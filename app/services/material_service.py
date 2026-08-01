"""材料服務：進料、耗用與庫存異動履歷。"""

from __future__ import annotations

from app.database import T_MATERIAL_TXNS, T_MATERIALS, fetch_all, fetch_one
from app.errors import NotFoundError, ValidationError
from app.models.base import shift_of, utcnow


async def receive(db, payload: dict, actor: str) -> dict:
    async with db.transaction():
        material = await fetch_one(
            db, f"SELECT * FROM {T_MATERIALS} WHERE material_id = $1 FOR UPDATE", payload["material_id"]
        )
        if material is None:
            raise NotFoundError(f"找不到材料：{payload['material_id']}")

        now = utcnow()
        qty = float(payload["qty"])
        updated = await fetch_one(
            db,
            f"UPDATE {T_MATERIALS} SET on_hand_qty = on_hand_qty + $1, updated_at = $2 "
            f"WHERE material_id = $3 RETURNING *",
            qty, now, payload["material_id"],
        )
        await db.execute(
            f"""
            INSERT INTO {T_MATERIAL_TXNS}
                (material_id, material_lot, txn_type, qty, operator, remark, timestamp, shift)
            VALUES ($1, $2, 'RECEIPT', $3, $4, $5, $6, $7)
            """,
            payload["material_id"], payload.get("material_lot", ""), qty,
            actor, payload.get("remark", ""), now, shift_of(now),
        )
        return updated


async def consume(db, usages: list[dict], lot: dict, op_code: str, actor: str) -> list[dict]:
    """批號於某站耗用材料；庫存不足時擋下，確保帳料一致。"""
    recorded: list[dict] = []
    now = utcnow()
    for usage in usages:
        material = await fetch_one(
            db, f"SELECT * FROM {T_MATERIALS} WHERE material_id = $1 FOR UPDATE", usage["material_id"]
        )
        if material is None:
            raise ValidationError(f"材料不存在：{usage['material_id']}")
        qty = float(usage["qty"])
        on_hand = float(material["on_hand_qty"])
        if qty > on_hand:
            raise ValidationError(
                f"材料 {material['material_id']} 庫存不足：現有 {on_hand} {material['uom']}，需求 {qty}"
            )
        await db.execute(
            f"UPDATE {T_MATERIALS} SET on_hand_qty = on_hand_qty - $1, updated_at = $2 "
            f"WHERE material_id = $3",
            qty, now, usage["material_id"],
        )
        await db.execute(
            f"""
            INSERT INTO {T_MATERIAL_TXNS}
                (material_id, material_lot, txn_type, qty, lot_id, device_id, op_code,
                 operator, timestamp, shift)
            VALUES ($1, $2, 'ISSUE', $3, $4, $5, $6, $7, $8, $9)
            """,
            usage["material_id"], usage.get("material_lot", ""), -qty,
            lot["lot_id"], lot.get("device_id"), op_code, actor, now, shift_of(now),
        )
        recorded.append({
            "material_id": usage["material_id"],
            "material_lot": usage.get("material_lot", ""),
            "qty": qty,
            "uom": material["uom"],
        })
    return recorded


async def transactions(db, material_id: str | None = None, lot_id: str | None = None, limit: int = 100) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_MATERIAL_TXNS}
        WHERE ($1::text IS NULL OR material_id = $1) AND ($2::text IS NULL OR lot_id = $2)
        ORDER BY timestamp DESC, id DESC LIMIT $3
        """,
        material_id, lot_id, limit,
    )


async def below_safety_stock(db) -> list[dict]:
    """低於安全庫存的材料清單。"""
    return await fetch_all(
        db,
        f"""
        SELECT *, safety_stock - on_hand_qty AS shortage
        FROM {T_MATERIALS}
        WHERE active AND safety_stock > on_hand_qty
        ORDER BY shortage DESC
        """,
    )
