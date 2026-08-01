"""品質服務：扣留清單、不良分析（Pareto）、測試 Bin 統計、不良判定。"""

from __future__ import annotations

from datetime import datetime

from bson import ObjectId

from app.database import COL_DEFECT_RECORDS, COL_HOLDS, COL_LOT_HISTORY
from app.errors import NotFoundError, ValidationError
from app.models.base import clean, clean_all, ensure_aware, utcnow
from app.models.enums import HoldStatus, LotAction


async def list_holds(db, status: str | None = None, lot_id: str | None = None, limit: int = 200) -> list[dict]:
    filt = {k: v for k, v in {"status": status, "lot_id": lot_id}.items() if v}
    cursor = db[COL_HOLDS].find(filt).sort([("held_at", -1)]).limit(limit)
    rows = clean_all(await cursor.to_list(length=limit))
    now = utcnow()
    for row in rows:
        end = ensure_aware(row["released_at"]) if row.get("released_at") else now
        row["hold_hours"] = round((end - ensure_aware(row["held_at"])).total_seconds() / 3600, 2)
    return rows


async def open_hold_summary(db) -> dict:
    """目前扣留中的批號統計，看板用。"""
    rows = await db[COL_HOLDS].aggregate(
        [
            {"$match": {"status": HoldStatus.OPEN.value}},
            {"$group": {"_id": "$reason", "count": {"$sum": 1}, "lots": {"$push": "$lot_id"}}},
            {"$sort": {"count": -1}},
        ]
    ).to_list(length=None)
    return {
        "total": sum(r["count"] for r in rows),
        "by_reason": [
            {"reason": r["_id"], "count": r["count"], "lots": sorted(r["lots"])} for r in rows
        ],
    }


async def list_defect_records(
    db,
    lot_id: str | None = None,
    op_code: str | None = None,
    defect_code: str | None = None,
    limit: int = 200,
) -> list[dict]:
    filt = {
        k: v
        for k, v in {"lot_id": lot_id, "op_code": op_code, "defect_code": defect_code}.items()
        if v
    }
    cursor = db[COL_DEFECT_RECORDS].find(filt).sort([("timestamp", -1)]).limit(limit)
    return clean_all(await cursor.to_list(length=limit))


async def defect_pareto(
    db,
    start: datetime,
    end: datetime,
    op_code: str | None = None,
    device_id: str | None = None,
    top_n: int = 20,
) -> dict:
    """不良柏拉圖：依不良數排序並算累計佔比，找出前幾大不良。"""
    match: dict = {"timestamp": {"$gte": start, "$lt": end}}
    if op_code:
        match["op_code"] = op_code
    if device_id:
        match["device_id"] = device_id

    rows = await db[COL_DEFECT_RECORDS].aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": {"code": "$defect_code", "name": "$defect_name", "category": "$category"},
                    "qty": {"$sum": "$qty"},
                    "occurrences": {"$sum": 1},
                }
            },
            {"$sort": {"qty": -1}},
        ]
    ).to_list(length=None)

    total = sum(r["qty"] for r in rows)
    items, cumulative = [], 0
    for row in rows[:top_n]:
        cumulative += row["qty"]
        items.append(
            {
                "defect_code": row["_id"]["code"],
                "defect_name": row["_id"]["name"],
                "category": row["_id"]["category"],
                "qty": row["qty"],
                "occurrences": row["occurrences"],
                "ratio": round(row["qty"] / total, 4) if total else 0.0,
                "cumulative_ratio": round(cumulative / total, 4) if total else 0.0,
            }
        )
    return {"window": {"start": start, "end": end}, "total_defect_qty": total, "items": items}


async def bin_summary(
    db,
    start: datetime,
    end: datetime,
    op_code: str | None = None,
    device_id: str | None = None,
) -> dict:
    """測試站 Bin 分佈統計（Bin 代碼為動態鍵，於應用層彙總）。"""
    match: dict = {
        "action": LotAction.TRACK_OUT.value,
        "timestamp": {"$gte": start, "$lt": end},
        "bin_map": {"$ne": None},
    }
    if op_code:
        match["op_code"] = op_code
    if device_id:
        match["device_id"] = device_id

    totals: dict[str, int] = {}
    grand = 0
    lots = 0
    async for doc in db[COL_LOT_HISTORY].find(match):
        bins = doc.get("bin_map") or {}
        if not bins:
            continue
        lots += 1
        for code, qty in bins.items():
            totals[str(code)] = totals.get(str(code), 0) + int(qty)
            grand += int(qty)

    items = [
        {"bin": code, "qty": qty, "ratio": round(qty / grand, 4) if grand else 0.0}
        for code, qty in sorted(totals.items(), key=lambda kv: -kv[1])
    ]
    return {"window": {"start": start, "end": end}, "lots": lots, "total_units": grand, "bins": items}


async def disposition_defect(db, record_id: str, disposition: str, actor: str, remark: str = "") -> dict:
    """品保對不良紀錄下判定（報廢／重工／特採／重測）。"""
    try:
        oid = ObjectId(record_id)
    except Exception:
        raise ValidationError(f"不良紀錄 ID 格式錯誤：{record_id}")
    record = await db[COL_DEFECT_RECORDS].find_one({"_id": oid})
    if record is None:
        raise NotFoundError(f"找不到不良紀錄：{record_id}")
    await db[COL_DEFECT_RECORDS].update_one(
        {"_id": oid},
        {
            "$set": {
                "disposition": disposition,
                "dispositioned_by": actor,
                "dispositioned_at": utcnow(),
                "disposition_remark": remark,
            }
        },
    )
    return clean(await db[COL_DEFECT_RECORDS].find_one({"_id": oid}))
