"""ERP 介接。

MES 與 ERP 的分工在 OSAT 很清楚：ERP 管訂單、料帳與應收，MES 管現場。
兩邊只能透過單據往來，而且必須擋得住重送、失敗與補送 ——
所以這裡採「收發交易表 + 冪等鍵 + 重試」的作法，而不是直接互打資料庫。

* **下行（ERP → MES）** —— 單據先落地 ``erp_inbound``，再由 ``process_pending``
  逐筆套用到主檔／工單。``(doc_type, external_id)`` 唯一，重送不會重複建檔。
* **上行（MES → ERP）** —— 完工、領料、出貨、報廢整理成 ``erp_outbound``，
  由 ERP 端拉取或本系統推送，送出後回 ACK 才算完成。

兩個方向都支援 REST 與 CSV 檔案兩種通道，因為現場真的還有很多 ERP 只吃檔案。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable

from app.database import (
    T_CUSTOMERS,
    T_DEFECT_RECORDS,
    T_DEVICES,
    T_ERP_INBOUND,
    T_ERP_OUTBOUND,
    T_LOTS,
    T_MATERIAL_TXNS,
    T_MATERIALS,
    T_SHIPMENTS,
    fetch_all,
    fetch_one,
)
from app.errors import NotFoundError, ValidationError
from app.models.base import json_safe, plain_values, utcnow
from app.models.enums import (
    ERPDocType,
    ERPInboundStatus,
    ERPOutboundStatus,
    ERPOutboundType,
    LotStatus,
)
from app.services import audit_service, master_service, workorder_service

#: 失敗超過這個次數就不再自動重試，交由人工處理
MAX_ATTEMPTS = 5


# ── 下行：收單 ──────────────────────────────────────────────
async def receive(db, payload: dict, actor: str = "erp") -> dict:
    """收下一筆 ERP 單據。相同 ``(doc_type, external_id)`` 視為重送。"""
    doc_type = str(payload["doc_type"])
    external_id = str(payload["external_id"])
    row = await fetch_one(
        db,
        f"""
        INSERT INTO {T_ERP_INBOUND} (external_id, doc_type, payload, source, status, received_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (doc_type, external_id) DO NOTHING
        RETURNING *
        """,
        external_id, doc_type, json_safe(payload.get("payload") or {}),
        payload.get("source", "REST"), ERPInboundStatus.PENDING.value, utcnow(),
    )
    if row is not None:
        return {**row, "duplicate": False}
    existing = await fetch_one(
        db,
        f"SELECT * FROM {T_ERP_INBOUND} WHERE doc_type = $1 AND external_id = $2",
        doc_type, external_id,
    )
    return {**existing, "duplicate": True}


async def receive_batch(db, documents: list[dict], actor: str = "erp") -> dict:
    results = [await receive(db, doc, actor) for doc in documents]
    accepted = [r for r in results if not r["duplicate"]]
    return {
        "received": len(results),
        "accepted": len(accepted),
        "duplicates": len(results) - len(accepted),
        "ids": [r["id"] for r in results],
    }


def parse_csv(content: str, external_id_column: str = "external_id") -> list[dict]:
    """把 CSV 轉成單據串列；欄位值一律當字串，型別交給各 handler 判斷。"""
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ValidationError("CSV 內容為空或缺少表頭")
    if external_id_column not in reader.fieldnames:
        raise ValidationError(
            f"CSV 缺少冪等鍵欄位 {external_id_column}（目前欄位：{', '.join(reader.fieldnames)}）"
        )
    rows: list[dict] = []
    for lineno, raw in enumerate(reader, start=2):
        record = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
        external_id = record.get(external_id_column, "")
        if not external_id:
            raise ValidationError(f"第 {lineno} 行的 {external_id_column} 為空")
        rows.append({"external_id": external_id, "payload": record})
    if not rows:
        raise ValidationError("CSV 沒有資料列")
    return rows


async def import_csv(db, doc_type: str, content: str, external_id_column: str, actor: str) -> dict:
    documents = [
        {"doc_type": doc_type, "external_id": r["external_id"], "payload": r["payload"], "source": "FILE"}
        for r in parse_csv(content, external_id_column)
    ]
    result = await receive_batch(db, documents, actor)
    await audit_service.record_change(
        db, actor, "IMPORT_CSV", T_ERP_INBOUND, doc_type,
        {"rows": result["received"], "accepted": result["accepted"]},
    )
    return {**result, "doc_type": doc_type}


# ── 下行：套用到 MES ────────────────────────────────────────
def _require(payload: dict, *fields: str) -> None:
    missing = [f for f in fields if not str(payload.get(f, "")).strip()]
    if missing:
        raise ValidationError(f"單據缺少必要欄位：{', '.join(missing)}")


def _as_int(payload: dict, field: str, default: int | None = None) -> int:
    value = payload.get(field, default)
    if value is None or str(value).strip() == "":
        if default is None:
            raise ValidationError(f"單據缺少必要欄位：{field}")
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise ValidationError(f"欄位 {field} 必須是數字，收到 {value!r}")


def _as_datetime(payload: dict, field: str) -> datetime:
    value = payload.get(field)
    if isinstance(value, datetime):
        return value
    if not value:
        raise ValidationError(f"單據缺少必要欄位：{field}")
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"欄位 {field} 不是合法的日期時間：{value!r}")


async def _upsert(db, table, payload: dict, key: str, actor: str) -> dict:
    """主檔已存在就更新、否則建立 —— ERP 重發全量檔時的必要行為。"""
    existing = await table.get(db, str(payload[key]), required=False)
    if existing is None:
        return await table.create(db, payload, actor)
    patch = {k: v for k, v in payload.items() if k != key}
    return await table.update(db, str(payload[key]), patch, actor)


async def _handle_customer(db, payload: dict, actor: str) -> dict:
    _require(payload, "code", "name")
    doc = await _upsert(db, master_service.customers, payload, "code", actor)
    return {"action": "UPSERT", "table": T_CUSTOMERS, "key": doc["code"]}


async def _handle_device(db, payload: dict, actor: str) -> dict:
    _require(payload, "device_id", "customer_code", "package_code", "route_code")
    data = dict(payload)
    data["gross_die_per_wafer"] = _as_int(data, "gross_die_per_wafer", 1)
    data["units_per_strip"] = _as_int(data, "units_per_strip", 1)
    data["units_per_reel"] = _as_int(data, "units_per_reel", 1)
    existing = await master_service.devices.get(db, str(data["device_id"]), required=False)
    if existing is None:
        doc = await master_service.create_device(db, data, actor)
    else:
        doc = await master_service.devices.update(
            db, str(data["device_id"]), {k: v for k, v in data.items() if k != "device_id"}, actor
        )
    return {"action": "UPSERT", "table": T_DEVICES, "key": doc["device_id"]}


async def _handle_material(db, payload: dict, actor: str) -> dict:
    _require(payload, "material_id", "name", "material_type")
    doc = await _upsert(db, master_service.materials, payload, "material_id", actor)
    return {"action": "UPSERT", "table": T_MATERIALS, "key": doc["material_id"]}


async def _handle_work_order(db, payload: dict, actor: str) -> dict:
    _require(payload, "device_id")
    data = {
        "device_id": str(payload["device_id"]),
        "plan_qty": _as_int(payload, "plan_qty"),
        "unit_type": payload.get("unit_type") or "WAFER",
        "due_date": _as_datetime(payload, "due_date"),
        "priority": _as_int(payload, "priority", 5),
        "customer_po": str(payload.get("customer_po", "")),
        "remark": str(payload.get("remark", "")),
    }
    if data["plan_qty"] <= 0:
        raise ValidationError("plan_qty 必須大於 0")
    wo = await workorder_service.create_work_order(db, data, actor)
    return {"action": "CREATE", "table": "work_orders", "key": wo["wo_no"], "wo_no": wo["wo_no"]}


HANDLERS: dict[str, Callable[..., Awaitable[dict]]] = {
    ERPDocType.CUSTOMER.value: _handle_customer,
    ERPDocType.DEVICE.value: _handle_device,
    ERPDocType.MATERIAL.value: _handle_material,
    ERPDocType.WORK_ORDER.value: _handle_work_order,
}


async def process_pending(db, doc_type: str | None = None, limit: int = 50, actor: str = "erp") -> dict:
    """逐筆套用待處理單據。

    每一筆都跑在自己的交易裡：一筆失敗不會拖垮整批，
    失敗原因寫回該筆的 ``error`` 供現場查修，重試次數用完才需要人工介入。
    """
    pending = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_ERP_INBOUND}
        WHERE status = $1 AND ($2::text IS NULL OR doc_type = $2) AND attempts < $3
        ORDER BY received_at, id LIMIT $4
        """,
        ERPInboundStatus.PENDING.value, doc_type, MAX_ATTEMPTS, limit,
    )
    processed, failed, details = 0, 0, []
    for row in pending:
        handler = HANDLERS.get(row["doc_type"])
        now = utcnow()
        if handler is None:
            await db.execute(
                f"""
                UPDATE {T_ERP_INBOUND}
                SET status = $1, error = $2, attempts = attempts + 1, processed_at = $3
                WHERE id = $4
                """,
                ERPInboundStatus.FAILED.value, f"不支援的單據類型：{row['doc_type']}", now, row["id"],
            )
            failed += 1
            details.append({"id": row["id"], "ok": False, "error": f"不支援的單據類型：{row['doc_type']}"})
            continue

        try:
            async with db.transaction():
                result = await handler(db, dict(row["payload"] or {}), actor)
                await db.execute(
                    f"""
                    UPDATE {T_ERP_INBOUND}
                    SET status = $1, result = $2, error = NULL,
                        attempts = attempts + 1, processed_at = $3
                    WHERE id = $4
                    """,
                    ERPInboundStatus.PROCESSED.value, json_safe(result), now, row["id"],
                )
            processed += 1
            details.append({"id": row["id"], "ok": True, **result})
        except Exception as exc:
            message = getattr(exc, "message", None) or str(exc)
            await db.execute(
                f"""
                UPDATE {T_ERP_INBOUND}
                SET status = $1, error = $2, attempts = attempts + 1, processed_at = $3
                WHERE id = $4
                """,
                ERPInboundStatus.FAILED.value, message[:2000], now, row["id"],
            )
            failed += 1
            details.append({"id": row["id"], "ok": False, "error": message})
    return {"picked": len(pending), "processed": processed, "failed": failed, "details": details}


async def retry_inbound(db, doc_id: int, actor: str) -> dict:
    row = await fetch_one(db, f"SELECT * FROM {T_ERP_INBOUND} WHERE id = $1", doc_id)
    if row is None:
        raise NotFoundError(f"找不到下行單據：{doc_id}")
    if row["status"] == ERPInboundStatus.PROCESSED.value:
        raise ValidationError(f"單據 {doc_id} 已處理成功，不需重試")
    result = await fetch_one(
        db,
        f"""
        UPDATE {T_ERP_INBOUND} SET status = $1, error = NULL, attempts = 0 WHERE id = $2 RETURNING *
        """,
        ERPInboundStatus.PENDING.value, doc_id,
    )
    await audit_service.record_change(db, actor, "RETRY", T_ERP_INBOUND, str(doc_id), None)
    return result


async def list_inbound(
    db, status: str | None = None, doc_type: str | None = None, limit: int = 200
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_ERP_INBOUND}
        WHERE ($1::text IS NULL OR status = $1) AND ($2::text IS NULL OR doc_type = $2)
        ORDER BY received_at DESC, id DESC LIMIT $3
        """,
        status, doc_type, limit,
    )


# ── 上行：產生待送單據 ──────────────────────────────────────
async def queue_outbound(db, doc_type: str, reference: str, payload: dict) -> dict:
    """排入待送佇列；同一張單重算時只覆蓋還沒送出的內容。"""
    return await fetch_one(
        db,
        f"""
        INSERT INTO {T_ERP_OUTBOUND} (doc_type, reference, payload, status, created_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (doc_type, reference) DO UPDATE
            SET payload = EXCLUDED.payload
            WHERE {T_ERP_OUTBOUND}.status = $4
        RETURNING *
        """,
        doc_type, reference, json_safe(payload), ERPOutboundStatus.PENDING.value, utcnow(),
    )


async def _build_production_reports(db, start: datetime, end: datetime) -> int:
    """完工批號的生產回報 —— ERP 據此結轉在製與計價。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT lot_id, wo_no, device_id, customer_code, qty, initial_qty, unit_type,
               scrap_qty, completed_at, route_code
        FROM {T_LOTS}
        WHERE status = ANY($1::text[]) AND completed_at >= $2 AND completed_at < $3
        ORDER BY completed_at
        """,
        [LotStatus.COMPLETED.value, LotStatus.SHIPPED.value], start, end,
    )
    count = 0
    for row in rows:
        initial = int(row["initial_qty"] or 0)
        payload = {
            "lot_id": row["lot_id"],
            "wo_no": row["wo_no"],
            "device_id": row["device_id"],
            "customer_code": row["customer_code"],
            "route_code": row["route_code"],
            "good_qty": int(row["qty"] or 0),
            "scrap_qty": int(row["scrap_qty"] or 0),
            "input_qty": initial,
            "unit_type": row["unit_type"],
            "yield": round(int(row["qty"] or 0) / initial, 6) if initial else None,
            "completed_at": row["completed_at"],
        }
        if await queue_outbound(db, ERPOutboundType.PRODUCTION_REPORT.value, row["lot_id"], payload):
            count += 1
    return count


async def _build_material_issues(db, start: datetime, end: datetime) -> int:
    """材料領用彙總 —— ERP 據此扣庫存。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT lot_id, material_id, material_lot, op_code,
               sum(qty) AS qty, min(timestamp) AS first_at, max(timestamp) AS last_at
        FROM {T_MATERIAL_TXNS}
        WHERE txn_type = 'ISSUE' AND timestamp >= $1 AND timestamp < $2
        GROUP BY lot_id, material_id, material_lot, op_code
        ORDER BY min(timestamp)
        """,
        start, end,
    )
    count = 0
    for row in rows:
        reference = f"{row['lot_id']}|{row['material_id']}|{row['material_lot']}|{row['op_code']}"
        payload = {
            "lot_id": row["lot_id"],
            "material_id": row["material_id"],
            "material_lot": row["material_lot"],
            "op_code": row["op_code"],
            "qty": float(row["qty"] or 0),
            "issued_at": row["last_at"],
        }
        if await queue_outbound(db, ERPOutboundType.MATERIAL_ISSUE.value, reference, payload):
            count += 1
    return count


async def _build_shipments(db, start: datetime, end: datetime) -> int:
    """出貨單 —— ERP 據此開立發票。"""
    rows = await fetch_all(
        db,
        f"SELECT * FROM {T_SHIPMENTS} WHERE shipped_at >= $1 AND shipped_at < $2 ORDER BY shipped_at",
        start, end,
    )
    count = 0
    for row in rows:
        payload = {
            "shipment_no": row["shipment_no"],
            "customer_code": row["customer_code"],
            "customer_po": row["customer_po"],
            "lot_ids": list(row["lot_ids"] or []),
            "device_ids": list(row["device_ids"] or []),
            "total_qty": int(row["total_qty"] or 0),
            "shipped_at": row["shipped_at"],
        }
        if await queue_outbound(db, ERPOutboundType.SHIPMENT.value, row["shipment_no"], payload):
            count += 1
    return count


async def _build_scraps(db, start: datetime, end: datetime) -> int:
    """報廢彙總 —— ERP 據此沖銷在製成本。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT lot_id, op_code, device_id, defect_code, sum(qty) AS qty, max(timestamp) AS last_at
        FROM {T_DEFECT_RECORDS}
        WHERE disposition = 'SCRAP' AND timestamp >= $1 AND timestamp < $2
        GROUP BY lot_id, op_code, device_id, defect_code
        ORDER BY max(timestamp)
        """,
        start, end,
    )
    count = 0
    for row in rows:
        reference = f"{row['lot_id']}|{row['op_code']}|{row['defect_code']}"
        payload = {
            "lot_id": row["lot_id"],
            "device_id": row["device_id"],
            "op_code": row["op_code"],
            "defect_code": row["defect_code"],
            "qty": int(row["qty"] or 0),
            "scrapped_at": row["last_at"],
        }
        if await queue_outbound(db, ERPOutboundType.SCRAP.value, reference, payload):
            count += 1
    return count


BUILDERS: dict[str, Callable[..., Awaitable[int]]] = {
    ERPOutboundType.PRODUCTION_REPORT.value: _build_production_reports,
    ERPOutboundType.MATERIAL_ISSUE.value: _build_material_issues,
    ERPOutboundType.SHIPMENT.value: _build_shipments,
    ERPOutboundType.SCRAP.value: _build_scraps,
}


async def build_outbound(db, payload: dict, actor: str) -> dict:
    now = utcnow()
    end = payload.get("end") or now
    start = payload.get("start") or (end - timedelta(hours=int(payload.get("hours", 24))))
    wanted = [str(t) for t in (payload.get("doc_types") or list(BUILDERS))]
    unknown = [t for t in wanted if t not in BUILDERS]
    if unknown:
        raise ValidationError(f"不支援的上行單據類型：{', '.join(unknown)}")

    created: dict[str, int] = {}
    async with db.transaction():
        for doc_type in wanted:
            created[doc_type] = await BUILDERS[doc_type](db, start, end)
        await audit_service.record_change(
            db, actor, "BUILD", T_ERP_OUTBOUND, ",".join(wanted),
            {"start": start, "end": end, "created": created},
        )
    return {"start": start, "end": end, "created": created, "total": sum(created.values())}


async def list_outbound(
    db, status: str | None = None, doc_type: str | None = None, limit: int = 200
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_ERP_OUTBOUND}
        WHERE ($1::text IS NULL OR status = $1) AND ($2::text IS NULL OR doc_type = $2)
        ORDER BY created_at DESC, id DESC LIMIT $3
        """,
        status, doc_type, limit,
    )


async def fetch_outbound(db, doc_type: str | None = None, limit: int = 100,
                         mark_sent: bool = True) -> dict:
    """ERP 端拉單：取出待送單據，並視需要標記為已送出。"""
    async with db.transaction():
        rows = await fetch_all(
            db,
            f"""
            SELECT * FROM {T_ERP_OUTBOUND}
            WHERE status = $1 AND ($2::text IS NULL OR doc_type = $2)
            ORDER BY created_at, id LIMIT $3
            FOR UPDATE SKIP LOCKED
            """,
            ERPOutboundStatus.PENDING.value, doc_type, limit,
        )
        if rows and mark_sent:
            await db.execute(
                f"""
                UPDATE {T_ERP_OUTBOUND}
                SET status = $1, sent_at = $2, attempts = attempts + 1
                WHERE id = ANY($3::bigint[])
                """,
                ERPOutboundStatus.SENT.value, utcnow(), [r["id"] for r in rows],
            )
    return {"count": len(rows), "documents": rows, "marked_sent": bool(rows and mark_sent)}


async def ack_outbound(db, ids: list[int], actor: str, remark: str = "") -> dict:
    rows = await fetch_all(
        db,
        f"""
        UPDATE {T_ERP_OUTBOUND} SET status = $1, acked_at = $2, error = NULL
        WHERE id = ANY($3::bigint[]) AND status <> $1
        RETURNING id, doc_type, reference
        """,
        ERPOutboundStatus.ACKED.value, utcnow(), ids,
    )
    await audit_service.record_change(
        db, actor, "ACK", T_ERP_OUTBOUND, ",".join(str(i) for i in ids[:20]),
        {"count": len(rows), "remark": remark},
    )
    return {"acked": len(rows), "documents": rows}


async def fail_outbound(db, ids: list[int], error: str, actor: str) -> dict:
    rows = await fetch_all(
        db,
        f"""
        UPDATE {T_ERP_OUTBOUND} SET status = $1, error = $2 WHERE id = ANY($3::bigint[])
        RETURNING id, doc_type, reference, attempts
        """,
        ERPOutboundStatus.FAILED.value, error[:2000], ids,
    )
    return {"failed": len(rows), "documents": rows}


async def requeue_outbound(db, ids: list[int], actor: str) -> dict:
    rows = await fetch_all(
        db,
        f"""
        UPDATE {T_ERP_OUTBOUND} SET status = $1, error = NULL, sent_at = NULL
        WHERE id = ANY($2::bigint[]) AND status <> $3
        RETURNING id, doc_type, reference
        """,
        ERPOutboundStatus.PENDING.value, ids, ERPOutboundStatus.ACKED.value,
    )
    return {"requeued": len(rows), "documents": rows}


#: 各單據類型輸出成 CSV 時的欄位順序
CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    ERPOutboundType.PRODUCTION_REPORT.value: (
        "lot_id", "wo_no", "device_id", "customer_code", "route_code",
        "input_qty", "good_qty", "scrap_qty", "unit_type", "yield", "completed_at",
    ),
    ERPOutboundType.MATERIAL_ISSUE.value: (
        "lot_id", "material_id", "material_lot", "op_code", "qty", "issued_at",
    ),
    ERPOutboundType.SHIPMENT.value: (
        "shipment_no", "customer_code", "customer_po", "lot_ids", "device_ids",
        "total_qty", "shipped_at",
    ),
    ERPOutboundType.SCRAP.value: (
        "lot_id", "device_id", "op_code", "defect_code", "qty", "scrapped_at",
    ),
}


async def export_csv(db, doc_type: str, status: str | None = None, limit: int = 1000) -> str:
    """把待送單據輸出成 CSV，供只吃檔案的 ERP 匯入。"""
    columns = CSV_COLUMNS.get(doc_type)
    if columns is None:
        raise ValidationError(f"不支援的上行單據類型：{doc_type}")
    rows = await list_outbound(db, status or ERPOutboundStatus.PENDING.value, doc_type, limit)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(("erp_doc_id", *columns))
    for row in rows:
        payload = row["payload"] or {}
        values = []
        for column in columns:
            value = payload.get(column, "")
            if isinstance(value, list):
                value = ";".join(str(v) for v in value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            values.append("" if value is None else value)
        writer.writerow((row["id"], *values))
    return buffer.getvalue()


async def summary(db) -> dict:
    """介接看板：兩個方向各卡了幾張單。"""
    inbound = await fetch_all(
        db,
        f"SELECT doc_type, status, count(*) AS qty FROM {T_ERP_INBOUND} GROUP BY doc_type, status",
    )
    outbound = await fetch_all(
        db,
        f"SELECT doc_type, status, count(*) AS qty FROM {T_ERP_OUTBOUND} GROUP BY doc_type, status",
    )
    stuck = await fetch_all(
        db,
        f"""
        SELECT id, doc_type, external_id, attempts, error, received_at
        FROM {T_ERP_INBOUND} WHERE status = $1 ORDER BY received_at DESC LIMIT 20
        """,
        ERPInboundStatus.FAILED.value,
    )

    def _fold(rows: list[dict]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for row in rows:
            entry = out.setdefault(row["doc_type"], {})
            entry[row["status"]] = int(row["qty"])
        return out

    return {
        "inbound": _fold(inbound),
        "outbound": _fold(outbound),
        "inbound_pending": sum(
            int(r["qty"]) for r in inbound if r["status"] == ERPInboundStatus.PENDING.value
        ),
        "inbound_failed": sum(
            int(r["qty"]) for r in inbound if r["status"] == ERPInboundStatus.FAILED.value
        ),
        "outbound_pending": sum(
            int(r["qty"]) for r in outbound if r["status"] == ERPOutboundStatus.PENDING.value
        ),
        "failed_documents": stuck,
    }
