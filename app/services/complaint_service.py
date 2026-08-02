"""客訴（RMA）與 8D。

追溯的「能力」原本就有了 —— 逆向、正向、族譜、die 級座標都查得到 ——
但沒有把手：客訴進來時沒有單可開、圈選出來的受影響範圍沒地方存、
8D 做到哪一步也沒人知道。

這個模組把客訴從收件到結案串成一條可稽核的流程，並在建單當下
**自動跑一次影響分析**：客戶申告的批號用了哪些晶圓、那些晶圓還做成哪些批、
那些批出給了誰。這是客訴進來的第一個小時最需要的東西。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import (
    T_COMPLAINT_EVENTS,
    T_COMPLAINTS,
    T_CUSTOMERS,
    T_LOTS,
    T_SHIPMENTS,
    fetch_all,
    fetch_one,
    next_sequence,
)
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import json_safe, to_local, utcnow
from app.services import audit_service, trace_service, wafermap_service

STATUS_OPEN = "OPEN"
STATUS_INVESTIGATING = "INVESTIGATING"
STATUS_ACTION = "ACTION"
STATUS_CLOSED = "CLOSED"
STATUS_REJECTED = "REJECTED"

#: 客訴狀態機
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATUS_OPEN: {STATUS_INVESTIGATING, STATUS_REJECTED},
    STATUS_INVESTIGATING: {STATUS_ACTION, STATUS_REJECTED},
    STATUS_ACTION: {STATUS_CLOSED, STATUS_INVESTIGATING},
    STATUS_CLOSED: set(),
    STATUS_REJECTED: set(),
}

#: 8D 各步驟
D8_STEPS = {
    "D1": "成立小組",
    "D2": "描述問題",
    "D3": "圍堵措施",
    "D4": "根本原因分析",
    "D5": "永久對策",
    "D6": "對策執行與驗證",
    "D7": "防止再發",
    "D8": "結案與表揚",
}

#: 結案前必須完成的步驟 —— 少了根本原因與對策的 8D 不算 8D
REQUIRED_FOR_CLOSE = ("D2", "D3", "D4", "D5", "D6")


async def _gen_complaint_no(db) -> str:
    ym = f"{to_local(utcnow()):%y%m}"
    return f"CM{ym}{await next_sequence(db, f'complaint:{ym}'):04d}"


async def _log_event(db, complaint_no: str, step: str, action: str, content: str, actor: str) -> None:
    await db.execute(
        f"""
        INSERT INTO {T_COMPLAINT_EVENTS} (complaint_no, step, action, content, actor, timestamp)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        complaint_no, step, action, content, actor, utcnow(),
    )


# ── 影響分析 ────────────────────────────────────────────────
async def analyse_impact(db, lot_ids: list[str], unit_seqs: list[int] | None = None) -> dict:
    """客訴進來的第一個動作：圈出受影響的範圍。

    申告批號 → 用到的晶圓 → 那些晶圓還做成哪些批 → 那些批出給了誰。
    有 die 綁定時再往下鑽到晶圓座標，看不良是不是聚在同一區。
    """
    wafers: set[str] = set()
    impacted: dict[str, dict] = {}
    for lot_id in lot_ids:
        lot = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1", lot_id)
        if lot is None:
            continue
        impacted[lot_id] = {
            "lot_id": lot_id, "device_id": lot["device_id"], "status": lot["status"],
            "relation": "REPORTED",
        }
        wafers.update(lot["wafer_ids"] or [])

    # 同一片晶圓做出來的其他批號同樣有風險
    for wafer_id in sorted(wafers):
        forward = await trace_service.forward_trace(db, wafer_id)
        for lot in forward["impacted_lots"]:
            impacted.setdefault(
                lot["lot_id"],
                {"lot_id": lot["lot_id"], "device_id": lot["device_id"],
                 "status": lot["status"], "relation": "SAME_WAFER"},
            )

    lot_id_list = sorted(impacted)
    shipments = await fetch_all(
        db,
        f"SELECT * FROM {T_SHIPMENTS} WHERE lot_ids && $1::text[] ORDER BY shipped_at DESC",
        lot_id_list,
    ) if lot_id_list else []

    dies: list[dict] = []
    for lot_id in lot_ids:
        for seq in (unit_seqs or []):
            try:
                found = await wafermap_service.die_of_unit(db, lot_id, int(seq))
            except NotFoundError:
                continue
            dies.append(
                {
                    "lot_id": lot_id, "unit_seq": int(seq),
                    "wafer_id": found["die"]["wafer_id"],
                    "die_x": found["die"]["die_x"], "die_y": found["die"]["die_y"],
                    "cp_bin": found["die"]["cp_bin"], "ft_bin": found["die"]["ft_bin"],
                }
            )

    return {
        "reported_lots": lot_ids,
        "source_wafers": sorted(wafers),
        "impacted_lots": [impacted[k] for k in lot_id_list],
        "impacted_lot_count": len(lot_id_list),
        "shipments": [
            {"shipment_no": s["shipment_no"], "customer_code": s["customer_code"],
             "total_qty": s["total_qty"], "shipped_at": s["shipped_at"]}
            for s in shipments
        ],
        "affected_customers": sorted({s["customer_code"] for s in shipments}),
        "returned_dies": dies,
    }


# ── 客訴單 ──────────────────────────────────────────────────
async def create_complaint(db, payload: dict, actor: str) -> dict:
    async with db.transaction():
        customer = await fetch_one(
            db, f"SELECT code FROM {T_CUSTOMERS} WHERE code = $1", payload["customer_code"]
        )
        if customer is None:
            raise ValidationError(f"客戶不存在：{payload['customer_code']}")

        lot_ids = list(dict.fromkeys(payload.get("lot_ids") or []))
        missing = []
        for lot_id in lot_ids:
            if await fetch_one(db, f"SELECT lot_id FROM {T_LOTS} WHERE lot_id = $1", lot_id) is None:
                missing.append(lot_id)
        if missing:
            raise ValidationError(f"批號不存在：{', '.join(missing)}")

        unit_seqs = [int(s) for s in (payload.get("unit_seqs") or [])]
        impact = await analyse_impact(db, lot_ids, unit_seqs)

        device_id = payload.get("device_id", "")
        if not device_id and impact["impacted_lots"]:
            device_id = impact["impacted_lots"][0]["device_id"]

        now = utcnow()
        complaint_no = await _gen_complaint_no(db)
        row = await fetch_one(
            db,
            f"""
            INSERT INTO {T_COMPLAINTS}
                (complaint_no, customer_code, customer_ref, device_id, lot_ids, unit_seqs,
                 qty, severity, category, description, status, owner, due_date,
                 impact, received_at, created_at, created_by, updated_at, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $16, $17)
            RETURNING *
            """,
            complaint_no, payload["customer_code"], payload.get("customer_ref", ""),
            device_id, lot_ids, unit_seqs, int(payload.get("qty", 0)),
            payload.get("severity", "MINOR"), payload.get("category", ""),
            payload.get("description", ""), STATUS_OPEN, payload.get("owner", ""),
            payload.get("due_date"), json_safe(impact),
            payload.get("received_at") or now, now, actor,
        )
        await _log_event(
            db, complaint_no, "", "CREATE",
            f"收件；影響分析圈出 {impact['impacted_lot_count']} 個批號、"
            f"{len(impact['affected_customers'])} 位客戶",
            actor,
        )
        await audit_service.record_change(
            db, actor, "CREATE", T_COMPLAINTS, complaint_no, payload
        )
        return row


async def get_complaint(db, complaint_no: str) -> dict:
    row = await fetch_one(
        db, f"SELECT * FROM {T_COMPLAINTS} WHERE complaint_no = $1", complaint_no
    )
    if row is None:
        raise NotFoundError(f"找不到客訴單：{complaint_no}")
    events = await fetch_all(
        db,
        f"SELECT * FROM {T_COMPLAINT_EVENTS} WHERE complaint_no = $1 ORDER BY timestamp, id",
        complaint_no,
    )
    return {**row, "events": events, "d8_steps": D8_STEPS}


async def list_complaints(
    db, status: str | None = None, customer_code: str | None = None,
    severity: str | None = None, limit: int = 200,
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT complaint_no, customer_code, customer_ref, device_id, lot_ids, qty,
               severity, category, status, owner, due_date, received_at, closed_at,
               (impact ->> 'impacted_lot_count')::int AS impacted_lot_count
        FROM {T_COMPLAINTS}
        WHERE ($1::text IS NULL OR status = $1)
          AND ($2::text IS NULL OR customer_code = $2)
          AND ($3::text IS NULL OR severity = $3)
        ORDER BY received_at DESC LIMIT $4
        """,
        status, customer_code, severity, limit,
    )


async def refresh_impact(db, complaint_no: str, actor: str) -> dict:
    """重跑影響分析 —— 客戶追加申告批號、或後續又有出貨時使用。"""
    complaint = await fetch_one(
        db, f"SELECT * FROM {T_COMPLAINTS} WHERE complaint_no = $1", complaint_no
    )
    if complaint is None:
        raise NotFoundError(f"找不到客訴單：{complaint_no}")
    impact = await analyse_impact(
        db, list(complaint["lot_ids"] or []), list(complaint["unit_seqs"] or [])
    )
    row = await fetch_one(
        db,
        f"""
        UPDATE {T_COMPLAINTS} SET impact = $1, updated_at = $2, updated_by = $3
        WHERE complaint_no = $4 RETURNING *
        """,
        json_safe(impact), utcnow(), actor, complaint_no,
    )
    await _log_event(
        db, complaint_no, "", "REFRESH_IMPACT",
        f"重新圈選：{impact['impacted_lot_count']} 個批號", actor,
    )
    return row


async def update_d8(db, complaint_no: str, step: str, payload: dict, actor: str) -> dict:
    """填寫某一個 8D 步驟。"""
    step = step.upper()
    if step not in D8_STEPS:
        raise ValidationError(f"不支援的 8D 步驟：{step}（可用 {', '.join(D8_STEPS)}）")

    async with db.transaction():
        complaint = await fetch_one(
            db, f"SELECT * FROM {T_COMPLAINTS} WHERE complaint_no = $1 FOR UPDATE", complaint_no
        )
        if complaint is None:
            raise NotFoundError(f"找不到客訴單：{complaint_no}")
        if complaint["status"] in {STATUS_CLOSED, STATUS_REJECTED}:
            raise StateError(f"客訴單 {complaint_no} 已{complaint['status']}，不可再修改")

        now = utcnow()
        d8 = dict(complaint["d8"] or {})
        d8[step] = {
            "title": D8_STEPS[step],
            "content": payload.get("content", ""),
            "owner": payload.get("owner", "") or actor,
            "completed": bool(payload.get("completed", True)),
            "updated_at": now.isoformat(),
            "updated_by": actor,
        }

        fields: dict[str, Any] = {"d8": json_safe(d8), "updated_at": now, "updated_by": actor}
        # D4 / D5 的內容同時映射到單頭，報表與 ERP 才不用挖 JSON
        if step == "D4":
            fields["root_cause"] = payload.get("content", "")
        if step == "D5":
            fields["corrective_action"] = payload.get("content", "")
        if complaint["status"] == STATUS_OPEN:
            fields["status"] = STATUS_INVESTIGATING

        assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(fields, start=1))
        row = await fetch_one(
            db,
            f"UPDATE {T_COMPLAINTS} SET {assignments} "
            f"WHERE complaint_no = ${len(fields) + 1} RETURNING *",
            *fields.values(), complaint_no,
        )
        await _log_event(
            db, complaint_no, step, "UPDATE",
            f"{D8_STEPS[step]}：{payload.get('content', '')[:200]}", actor,
        )
        return row


async def change_status(db, complaint_no: str, target: str, payload: dict, actor: str) -> dict:
    async with db.transaction():
        complaint = await fetch_one(
            db, f"SELECT * FROM {T_COMPLAINTS} WHERE complaint_no = $1 FOR UPDATE", complaint_no
        )
        if complaint is None:
            raise NotFoundError(f"找不到客訴單：{complaint_no}")

        current = complaint["status"]
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise StateError(f"客訴狀態不可由 {current} 轉為 {target}")

        if target == STATUS_CLOSED:
            d8 = complaint["d8"] or {}
            missing = [
                f"{s}（{D8_STEPS[s]}）" for s in REQUIRED_FOR_CLOSE
                if not (d8.get(s) or {}).get("completed")
            ]
            if missing:
                raise StateError(f"以下 8D 步驟尚未完成，不可結案：{', '.join(missing)}")

        now = utcnow()
        fields: dict[str, Any] = {"status": target, "updated_at": now, "updated_by": actor}
        if target in {STATUS_CLOSED, STATUS_REJECTED}:
            fields["closed_at"] = now
            fields["closed_by"] = actor

        assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(fields, start=1))
        row = await fetch_one(
            db,
            f"UPDATE {T_COMPLAINTS} SET {assignments} "
            f"WHERE complaint_no = ${len(fields) + 1} RETURNING *",
            *fields.values(), complaint_no,
        )
        await _log_event(
            db, complaint_no, "", "STATUS",
            f"{current} → {target}；{payload.get('remark', '')}", actor,
        )
        await audit_service.record_change(
            db, actor, "STATUS", T_COMPLAINTS, complaint_no, {"status": target, **payload}
        )
        return row


async def add_note(db, complaint_no: str, content: str, actor: str) -> dict:
    await get_complaint(db, complaint_no)
    await _log_event(db, complaint_no, "", "NOTE", content, actor)
    return {"complaint_no": complaint_no, "added": True}


async def summary(db, start: datetime, end: datetime) -> dict:
    """客訴看板：件數、嚴重度分布、逾期未結案、平均結案天數。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT status, severity, count(*) AS qty,
               avg(EXTRACT(EPOCH FROM (closed_at - received_at)) / 86400.0) AS avg_days
        FROM {T_COMPLAINTS}
        WHERE received_at >= $1 AND received_at < $2
        GROUP BY status, severity
        """,
        start, end,
    )
    overdue = await fetch_all(
        db,
        f"""
        SELECT complaint_no, customer_code, device_id, severity, owner, due_date, received_at
        FROM {T_COMPLAINTS}
        WHERE status NOT IN ($1, $2) AND due_date IS NOT NULL AND due_date < $3
        ORDER BY due_date
        """,
        STATUS_CLOSED, STATUS_REJECTED, utcnow(),
    )

    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    closed_days: list[float] = []
    for row in rows:
        qty = int(row["qty"])
        by_status[row["status"]] = by_status.get(row["status"], 0) + qty
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + qty
        if row["status"] == STATUS_CLOSED and row["avg_days"] is not None:
            closed_days.extend([float(row["avg_days"])] * qty)

    total = sum(by_status.values())
    return {
        "start": start, "end": end,
        "total": total,
        "by_status": by_status,
        "by_severity": by_severity,
        "open": total - by_status.get(STATUS_CLOSED, 0) - by_status.get(STATUS_REJECTED, 0),
        "overdue": overdue,
        "avg_close_days": round(sum(closed_days) / len(closed_days), 2) if closed_days else None,
    }
