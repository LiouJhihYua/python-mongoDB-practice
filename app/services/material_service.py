"""材料服務：進料、耗用與庫存異動履歷。"""

from __future__ import annotations

from app.database import COL_MATERIAL_TXNS, COL_MATERIALS
from app.errors import NotFoundError, ValidationError
from app.models.base import clean, clean_all, shift_of, utcnow


async def receive(db, payload: dict, actor: str) -> dict:
    material = await db[COL_MATERIALS].find_one({"material_id": payload["material_id"]})
    if material is None:
        raise NotFoundError(f"找不到材料：{payload['material_id']}")
    now = utcnow()
    await db[COL_MATERIALS].update_one(
        {"material_id": payload["material_id"]},
        {"$inc": {"on_hand_qty": float(payload["qty"])}, "$set": {"updated_at": now}},
    )
    await db[COL_MATERIAL_TXNS].insert_one(
        {
            "material_id": payload["material_id"],
            "material_lot": payload.get("material_lot", ""),
            "txn_type": "RECEIPT",
            "qty": float(payload["qty"]),
            "lot_id": None,
            "op_code": None,
            "operator": actor,
            "remark": payload.get("remark", ""),
            "timestamp": now,
            "shift": shift_of(now),
        }
    )
    return clean(await db[COL_MATERIALS].find_one({"material_id": payload["material_id"]}))


async def consume(db, usages: list[dict], lot: dict, op_code: str, actor: str) -> list[dict]:
    """批號於某站耗用材料；庫存不足時擋下，確保帳料一致。"""
    recorded: list[dict] = []
    now = utcnow()
    for usage in usages:
        material = await db[COL_MATERIALS].find_one({"material_id": usage["material_id"]})
        if material is None:
            raise ValidationError(f"材料不存在：{usage['material_id']}")
        qty = float(usage["qty"])
        on_hand = float(material.get("on_hand_qty", 0))
        if qty > on_hand:
            raise ValidationError(
                f"材料 {material['material_id']} 庫存不足：現有 {on_hand} {material.get('uom', '')}，需求 {qty}"
            )
        await db[COL_MATERIALS].update_one(
            {"material_id": usage["material_id"]},
            {"$inc": {"on_hand_qty": -qty}, "$set": {"updated_at": now}},
        )
        txn = {
            "material_id": usage["material_id"],
            "material_lot": usage.get("material_lot", ""),
            "txn_type": "ISSUE",
            "qty": -qty,
            "lot_id": lot["lot_id"],
            "device_id": lot.get("device_id"),
            "op_code": op_code,
            "operator": actor,
            "remark": "",
            "timestamp": now,
            "shift": shift_of(now),
        }
        await db[COL_MATERIAL_TXNS].insert_one(dict(txn))
        recorded.append(
            {
                "material_id": usage["material_id"],
                "material_lot": usage.get("material_lot", ""),
                "qty": qty,
                "uom": material.get("uom", ""),
            }
        )
    return recorded


async def transactions(db, material_id: str | None = None, lot_id: str | None = None, limit: int = 100) -> list[dict]:
    filt = {k: v for k, v in {"material_id": material_id, "lot_id": lot_id}.items() if v}
    cursor = db[COL_MATERIAL_TXNS].find(filt).sort([("timestamp", -1)]).limit(limit)
    return clean_all(await cursor.to_list(length=limit))


async def below_safety_stock(db) -> list[dict]:
    """低於安全庫存的材料清單。"""
    rows = await db[COL_MATERIALS].aggregate(
        [
            {"$match": {"active": True}},
            {"$addFields": {"shortage": {"$subtract": ["$safety_stock", "$on_hand_qty"]}}},
            {"$match": {"shortage": {"$gt": 0}}},
            {"$sort": {"shortage": -1}},
        ]
    ).to_list(length=None)
    return clean_all(rows)
