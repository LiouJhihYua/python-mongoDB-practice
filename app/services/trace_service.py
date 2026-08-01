"""追溯服務：晶圓 ↔ 批號 ↔ 出貨的正逆向追蹤，以及批號族譜（Genealogy）。"""

from __future__ import annotations

from app.database import (
    COL_DEFECT_RECORDS,
    COL_HOLDS,
    COL_LOT_HISTORY,
    COL_LOTS,
    COL_MATERIAL_TXNS,
    COL_SHIPMENTS,
    COL_WAFERS,
)
from app.errors import NotFoundError
from app.models.base import clean, clean_all
from app.models.enums import LotAction

LOT_BRIEF = {
    "_id": 0,
    "lot_id": 1,
    "device_id": 1,
    "wo_no": 1,
    "status": 1,
    "qty": 1,
    "unit_type": 1,
    "current_op": 1,
    "current_seq": 1,
    "parent_lot_id": 1,
    "child_lot_ids": 1,
    "merged_from": 1,
    "merged_into": 1,
    "wafer_ids": 1,
    "created_at": 1,
}


async def _brief(db, lot_id: str) -> dict | None:
    return await db[COL_LOTS].find_one({"lot_id": lot_id}, LOT_BRIEF)


async def _ancestors(db, lot_id: str, seen: set[str]) -> list[dict]:
    """往上追：拆批母批 + 併批來源批。"""
    if lot_id in seen:
        return []
    seen.add(lot_id)
    lot = await _brief(db, lot_id)
    if lot is None:
        return []
    parents = [p for p in ([lot.get("parent_lot_id")] + list(lot.get("merged_from") or [])) if p]
    out: list[dict] = []
    for pid in parents:
        parent = await _brief(db, pid)
        if parent is None:
            continue
        out.append({**parent, "relation": "SPLIT_FROM" if pid == lot.get("parent_lot_id") else "MERGED_FROM"})
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
    kids = [k for k in (list(lot.get("child_lot_ids") or []) + [lot.get("merged_into")]) if k]
    out: list[dict] = []
    for kid in kids:
        child = await _brief(db, kid)
        if child is None:
            continue
        out.append({**child, "relation": "SPLIT_TO" if kid in (lot.get("child_lot_ids") or []) else "MERGED_INTO"})
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
    wafers = set(lot.get("wafer_ids") or [])
    if wafers:
        return sorted(wafers)
    for anc in await _ancestors(db, lot_id, set()):
        wafers.update(anc.get("wafer_ids") or [])
    return sorted(wafers)


async def backward_trace(db, lot_id: str) -> dict:
    """逆向追溯：這批貨是「用什麼、在哪台機、由誰、何時」做出來的。

    客戶客訴時的標準動作。
    """
    lot = await db[COL_LOTS].find_one({"lot_id": lot_id})
    if lot is None:
        raise NotFoundError(f"找不到批號：{lot_id}")

    wafer_ids = await _root_wafer_ids(db, lot_id)
    wafers = clean_all(
        await db[COL_WAFERS].find({"wafer_id": {"$in": wafer_ids}}).to_list(length=len(wafer_ids) or 1)
    ) if wafer_ids else []

    # 含上游批號的完整加工履歷
    ancestors = await _ancestors(db, lot_id, set())
    all_lot_ids = [lot_id] + [a["lot_id"] for a in ancestors]
    history = clean_all(
        await db[COL_LOT_HISTORY]
        .find({"lot_id": {"$in": all_lot_ids}, "action": {"$in": [LotAction.TRACK_OUT.value, LotAction.TRACK_IN.value]}})
        .sort([("timestamp", 1)])
        .to_list(length=None)
    )

    equipments = sorted({h["eq_id"] for h in history if h.get("eq_id")})
    operators = sorted({h["operator"] for h in history if h.get("operator")})
    materials = clean_all(
        await db[COL_MATERIAL_TXNS].find({"lot_id": {"$in": all_lot_ids}}).to_list(length=None)
    )
    defects = clean_all(
        await db[COL_DEFECT_RECORDS].find({"lot_id": {"$in": all_lot_ids}}).to_list(length=None)
    )
    holds = clean_all(await db[COL_HOLDS].find({"lot_id": {"$in": all_lot_ids}}).to_list(length=None))
    shipments = clean_all(await db[COL_SHIPMENTS].find({"lot_ids": lot_id}).to_list(length=None))

    return {
        "lot": clean(lot),
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
    wafer = await db[COL_WAFERS].find_one({"wafer_id": wafer_id})
    if wafer is None:
        raise NotFoundError(f"找不到晶圓：{wafer_id}")

    seed_lots = clean_all(await db[COL_LOTS].find({"wafer_ids": wafer_id}, LOT_BRIEF).to_list(length=None))
    impacted: dict[str, dict] = {l["lot_id"]: l for l in seed_lots}
    for lot in seed_lots:
        for desc in await _descendants(db, lot["lot_id"], set()):
            impacted.setdefault(desc["lot_id"], desc)

    lot_ids = sorted(impacted)
    shipments = clean_all(
        await db[COL_SHIPMENTS].find({"lot_ids": {"$in": lot_ids}}).to_list(length=None)
    ) if lot_ids else []

    return {
        "wafer": clean(wafer),
        "impacted_lots": [impacted[k] for k in lot_ids],
        "impacted_lot_count": len(lot_ids),
        "shipments": shipments,
        "shipped_customers": sorted({s["customer_code"] for s in shipments}),
    }


async def where_used(db, material_lot: str) -> dict:
    """材料批號被哪些生產批號用掉 —— 供應商材料異常時的圈選範圍。"""
    txns = clean_all(
        await db[COL_MATERIAL_TXNS].find({"material_lot": material_lot, "txn_type": "ISSUE"}).to_list(length=None)
    )
    lot_ids = sorted({t["lot_id"] for t in txns if t.get("lot_id")})
    lots = clean_all(
        await db[COL_LOTS].find({"lot_id": {"$in": lot_ids}}, LOT_BRIEF).to_list(length=None)
    ) if lot_ids else []
    return {
        "material_lot": material_lot,
        "transactions": txns,
        "impacted_lots": lots,
        "impacted_lot_count": len(lots),
    }
