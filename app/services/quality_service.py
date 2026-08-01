"""品質服務：扣留清單、不良分析（Pareto）、測試 Bin 統計、不良判定。"""

from __future__ import annotations

from datetime import datetime

from app.database import T_DEFECT_RECORDS, T_HOLDS, T_LOT_HISTORY, fetch_all, fetch_one
from app.errors import NotFoundError, ValidationError
from app.models.base import utcnow
from app.models.enums import HoldStatus, LotAction


async def list_holds(db, status: str | None = None, lot_id: str | None = None, limit: int = 200) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT *,
               round(EXTRACT(EPOCH FROM (COALESCE(released_at, $3) - held_at))::numeric / 3600, 2)
                   AS hold_hours
        FROM {T_HOLDS}
        WHERE ($1::text IS NULL OR status = $1) AND ($2::text IS NULL OR lot_id = $2)
        ORDER BY held_at DESC, id DESC LIMIT $4
        """,
        status, lot_id, utcnow(), limit,
    )


async def open_hold_summary(db) -> dict:
    """目前扣留中的批號統計，看板用。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT reason, count(*) AS count, array_agg(lot_id ORDER BY lot_id) AS lots
        FROM {T_HOLDS} WHERE status = $1
        GROUP BY reason ORDER BY count DESC
        """,
        HoldStatus.OPEN.value,
    )
    return {
        "total": sum(r["count"] for r in rows),
        "by_reason": [{"reason": r["reason"], "count": r["count"], "lots": r["lots"]} for r in rows],
    }


async def list_defect_records(
    db,
    lot_id: str | None = None,
    op_code: str | None = None,
    defect_code: str | None = None,
    limit: int = 200,
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_DEFECT_RECORDS}
        WHERE ($1::text IS NULL OR lot_id = $1)
          AND ($2::text IS NULL OR op_code = $2)
          AND ($3::text IS NULL OR defect_code = $3)
        ORDER BY timestamp DESC, id DESC LIMIT $4
        """,
        lot_id, op_code, defect_code, limit,
    )


async def defect_pareto(
    db,
    start: datetime,
    end: datetime,
    op_code: str | None = None,
    device_id: str | None = None,
    top_n: int = 20,
) -> dict:
    """不良柏拉圖：依不良數排序並算累計佔比，找出前幾大不良。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT defect_code, max(defect_name) AS defect_name, max(category) AS category,
               sum(qty) AS qty, count(*) AS occurrences
        FROM {T_DEFECT_RECORDS}
        WHERE timestamp >= $1 AND timestamp < $2
          AND ($3::text IS NULL OR op_code = $3)
          AND ($4::text IS NULL OR device_id = $4)
        GROUP BY defect_code
        ORDER BY qty DESC
        """,
        start, end, op_code, device_id,
    )

    total = sum(int(r["qty"]) for r in rows)
    items, cumulative = [], 0
    for row in rows[:top_n]:
        qty = int(row["qty"])
        cumulative += qty
        items.append({
            "defect_code": row["defect_code"],
            "defect_name": row["defect_name"],
            "category": row["category"],
            "qty": qty,
            "occurrences": row["occurrences"],
            "ratio": round(qty / total, 4) if total else 0.0,
            "cumulative_ratio": round(cumulative / total, 4) if total else 0.0,
        })
    return {"window": {"start": start, "end": end}, "total_defect_qty": total, "items": items}


async def bin_summary(
    db,
    start: datetime,
    end: datetime,
    op_code: str | None = None,
    device_id: str | None = None,
) -> dict:
    """測試站 Bin 分佈統計（Bin 代碼是 JSONB 的動態鍵，用 jsonb_each 展開）。"""
    where = """
        WHERE h.action = $1 AND h.timestamp >= $2 AND h.timestamp < $3 AND h.bin_map IS NOT NULL
          AND ($4::text IS NULL OR h.op_code = $4)
          AND ($5::text IS NULL OR h.device_id = $5)
    """
    args = (LotAction.TRACK_OUT.value, start, end, op_code, device_id)

    rows = await fetch_all(
        db,
        f"""
        SELECT b.key AS bin, sum(b.value::bigint) AS qty
        FROM {T_LOT_HISTORY} h, jsonb_each_text(h.bin_map) b
        {where}
        GROUP BY b.key
        ORDER BY qty DESC
        """,
        *args,
    )
    lots = await db.fetchval(f"SELECT count(*) FROM {T_LOT_HISTORY} h {where}", *args)

    grand = sum(int(r["qty"]) for r in rows)
    items = [
        {"bin": r["bin"], "qty": int(r["qty"]), "ratio": round(int(r["qty"]) / grand, 4) if grand else 0.0}
        for r in rows
    ]
    return {"window": {"start": start, "end": end}, "lots": lots, "total_units": grand, "bins": items}


async def disposition_defect(db, record_id: int, disposition: str, actor: str, remark: str = "") -> dict:
    """品保對不良紀錄下判定（報廢／重工／特採／重測）。"""
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        raise ValidationError(f"不良紀錄 ID 格式錯誤：{record_id}")

    row = await fetch_one(
        db,
        f"""
        UPDATE {T_DEFECT_RECORDS}
        SET disposition = $1, dispositioned_by = $2, dispositioned_at = $3, disposition_remark = $4
        WHERE id = $5 RETURNING *
        """,
        disposition, actor, utcnow(), remark, record_id,
    )
    if row is None:
        raise NotFoundError(f"找不到不良紀錄：{record_id}")
    return row
