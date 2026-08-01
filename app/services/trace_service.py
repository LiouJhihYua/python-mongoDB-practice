"""追溯服務：晶圓 ↔ 批號 ↔ 出貨的正逆向追蹤，以及批號族譜（Genealogy）。"""

from __future__ import annotations

from app.database import (
    T_DEFECT_RECORDS,
    T_HOLDS,
    T_LOT_HISTORY,
    T_LOTS,
    T_MATERIAL_TXNS,
    T_SHIPMENTS,
    T_WAFERS,
    fetch_all,
    fetch_one,
)
from app.errors import NotFoundError
from app.models.enums import LotAction

LOT_BRIEF = """
    lot_id, device_id, wo_no, status, qty, unit_type, current_op, current_seq,
    parent_lot_id, child_lot_ids, merged_from, merged_into, wafer_ids, created_at
"""


async def _brief(db, lot_id: str) -> dict | None:
    return await fetch_one(db, f"SELECT {LOT_BRIEF} FROM {T_LOTS} WHERE lot_id = $1", lot_id)


async def _ancestors(db, lot_id: str, seen: set[str]) -> list[dict]:
    """往上追：拆批母批 + 併批來源批。"""
    if lot_id in seen:
        return []
    seen.add(lot_id)
    lot = await _brief(db, lot_id)
    if lot is None:
        return []
    parents = [p for p in ([lot["parent_lot_id"]] + list(lot["merged_from"] or [])) if p]
    out: list[dict] = []
    for pid in parents:
        parent = await _brief(db, pid)
        if parent is None:
            continue
        relation = "SPLIT_FROM" if pid == lot["parent_lot_id"] else "MERGED_FROM"
        out.append({**parent, "relation": relation})
        out.extend(await _ancestors(db, pid, seen))
    return out


async def _descendants(db, lot_id: str, seen: set[str]) -> list[dict]:
    """往下追：拆出的子批 + 併入的新批。"""
    if lot_id in seen:
        return []
    seen.add(lot_id)
    lot = await _brief(db, lot_id)
    if lot is None:
        return []
    kids = [k for k in (list(lot["child_lot_ids"] or []) + [lot["merged_into"]]) if k]
    out: list[dict] = []
    for kid in kids:
        child = await _brief(db, kid)
        if child is None:
            continue
        relation = "SPLIT_TO" if kid in (lot["child_lot_ids"] or []) else "MERGED_INTO"
        out.append({**child, "relation": relation})
        out.extend(await _descendants(db, kid, seen))
    return out


async def genealogy(db, lot_id: str) -> dict:
    """批號族譜：上溯來源、下追去向。"""
    lot = await _brief(db, lot_id)
    if lot is None:
        raise NotFoundError(f"找不到批號：{lot_id}")
    return {
        "lot": lot,
        "ancestors": await _ancestors(db, lot_id, set()),
        "descendants": await _descendants(db, lot_id, set()),
    }


async def _root_wafer_ids(db, lot_id: str) -> list[str]:
    """本批實際用到的晶圓。

    拆批時已把晶圓歸屬分配給各子批（無法分割時才整份複製），
    因此以本批自身的紀錄為準；只有在自身沒有紀錄時才往上游追，
    避免客訴圈選範圍被不當放大。
    """
    lot = await _brief(db, lot_id)
    if lot is None:
        return []
    wafers = set(lot["wafer_ids"] or [])
    if wafers:
        return sorted(wafers)
    for anc in await _ancestors(db, lot_id, set()):
        wafers.update(anc["wafer_ids"] or [])
    return sorted(wafers)


async def backward_trace(db, lot_id: str) -> dict:
    """逆向追溯：這批貨是「用什麼、在哪台機、由誰、何時」做出來的。

    客戶客訴時的標準動作。
    """
    lot = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1", lot_id)
    if lot is None:
        raise NotFoundError(f"找不到批號：{lot_id}")

    wafer_ids = await _root_wafer_ids(db, lot_id)
    wafers = await fetch_all(
        db, f"SELECT * FROM {T_WAFERS} WHERE wafer_id = ANY($1::text[]) ORDER BY wafer_id", wafer_ids
    ) if wafer_ids else []

    ancestors = await _ancestors(db, lot_id, set())
    all_lot_ids = [lot_id] + [a["lot_id"] for a in ancestors]

    history = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_LOT_HISTORY}
        WHERE lot_id = ANY($1::text[]) AND action = ANY($2::text[])
        ORDER BY timestamp ASC, id ASC
        """,
        all_lot_ids, [LotAction.TRACK_OUT.value, LotAction.TRACK_IN.value],
    )
    equipments = sorted({h["eq_id"] for h in history if h["eq_id"]})
    operators = sorted({h["operator"] for h in history if h["operator"]})

    materials = await fetch_all(
        db, f"SELECT * FROM {T_MATERIAL_TXNS} WHERE lot_id = ANY($1::text[]) ORDER BY timestamp",
        all_lot_ids,
    )
    defects = await fetch_all(
        db, f"SELECT * FROM {T_DEFECT_RECORDS} WHERE lot_id = ANY($1::text[]) ORDER BY timestamp",
        all_lot_ids,
    )
    holds = await fetch_all(
        db, f"SELECT * FROM {T_HOLDS} WHERE lot_id = ANY($1::text[]) ORDER BY held_at", all_lot_ids
    )
    shipments = await fetch_all(
        db, f"SELECT * FROM {T_SHIPMENTS} WHERE $1 = ANY(lot_ids)", lot_id
    )

    return {
        "lot": lot,
        "source_wafers": wafers,
        "ancestors": ancestors,
        "process_history": history,
        "equipments_used": equipments,
        "operators_involved": operators,
        "materials_consumed": materials,
        "defects": defects,
        "holds": holds,
        "shipments": shipments,
    }


async def forward_trace(db, wafer_id: str) -> dict:
    """正向追溯：這片晶圓最後變成哪些批號、出貨給誰。

    晶圓廠通知某批 wafer 有異常時，用來圈出受影響範圍。
    """
    wafer = await fetch_one(db, f"SELECT * FROM {T_WAFERS} WHERE wafer_id = $1", wafer_id)
    if wafer is None:
        raise NotFoundError(f"找不到晶圓：{wafer_id}")

    seed_lots = await fetch_all(
        db, f"SELECT {LOT_BRIEF} FROM {T_LOTS} WHERE $1 = ANY(wafer_ids) ORDER BY lot_id", wafer_id
    )
    impacted: dict[str, dict] = {l["lot_id"]: l for l in seed_lots}
    for lot in seed_lots:
        for desc in await _descendants(db, lot["lot_id"], set()):
            impacted.setdefault(desc["lot_id"], desc)

    lot_ids = sorted(impacted)
    shipments = await fetch_all(
        db, f"SELECT * FROM {T_SHIPMENTS} WHERE lot_ids && $1::text[] ORDER BY shipped_at", lot_ids
    ) if lot_ids else []

    return {
        "wafer": wafer,
        "impacted_lots": [impacted[k] for k in lot_ids],
        "impacted_lot_count": len(lot_ids),
        "shipments": shipments,
        "shipped_customers": sorted({s["customer_code"] for s in shipments}),
    }


async def where_used(db, material_lot: str) -> dict:
    """材料批號被哪些生產批號用掉 —— 供應商材料異常時的圈選範圍。"""
    txns = await fetch_all(
        db,
        f"SELECT * FROM {T_MATERIAL_TXNS} WHERE material_lot = $1 AND txn_type = 'ISSUE' ORDER BY timestamp",
        material_lot,
    )
    lot_ids = sorted({t["lot_id"] for t in txns if t["lot_id"]})
    lots = await fetch_all(
        db, f"SELECT {LOT_BRIEF} FROM {T_LOTS} WHERE lot_id = ANY($1::text[]) ORDER BY lot_id", lot_ids
    ) if lot_ids else []
    return {
        "material_lot": material_lot,
        "transactions": txns,
        "impacted_lots": lots,
        "impacted_lot_count": len(lots),
    }
