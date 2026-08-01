"""測試共用流程輔助。"""

from __future__ import annotations

from datetime import timedelta

from app.models.base import utcnow
from app.models.enums import WorkOrderStatus
from app.services import lot_service, workorder_service


async def make_work_order(db, plan_qty: int = 50, device_id: str = "TEST-QFN48") -> str:
    wo = await workorder_service.create_work_order(
        db,
        {
            "device_id": device_id,
            "plan_qty": plan_qty,
            "unit_type": "WAFER",
            "due_date": utcnow() + timedelta(days=7),
            "priority": 3,
            "customer_po": "PO-TEST",
            "remark": "",
        },
        "planner01",
    )
    await workorder_service.change_status(db, wo["wo_no"], WorkOrderStatus.RELEASED, "planner01")
    return wo["wo_no"]


async def make_lot(db, qty: int = 5, wo_no: str | None = None, with_wafers: bool = True) -> dict:
    wo_no = wo_no or await make_work_order(db)
    wafer_ids = []
    if with_wafers:
        wafers = await db["wafers"].find({"consumed": {"$ne": True}}).limit(qty).to_list(length=qty)
        wafer_ids = [w["wafer_id"] for w in wafers]
    return await lot_service.create_lot(
        db,
        {"wo_no": wo_no, "qty": qty, "wafer_ids": wafer_ids, "carrier_id": "MAG001", "remark": ""},
        "planner01",
    )


async def run_step(db, users, lot_id: str, eq_id: str = "", reject: int = 0, defect_code: str = "") -> dict:
    """跑完一站：進站 → 出站（依現況自動算出應產出量）。"""
    operator = users["op001"]
    await lot_service.track_in(db, {"lot_id": lot_id, "eq_id": eq_id, "remark": ""}, operator)
    lot = await lot_service.get_lot(db, lot_id, raw=True)
    from app.services.master_service import operations

    operation = await operations.raw(db, lot["current_op"])
    device = await db["devices"].find_one({"device_id": lot["device_id"]})
    expected, _ = lot_service.compute_expected_output(
        int(lot["qty"]), operation, device, lot["unit_type"]
    )
    payload = {
        "lot_id": lot_id,
        "good_qty": expected - reject,
        "reject_qty": reject,
        "defects": [{"defect_code": defect_code, "qty": reject}] if reject and defect_code else [],
        "materials": [],
        "bin_map": None,
        "remark": "",
    }
    return await lot_service.track_out(db, payload, operator)


#: 各站使用的設備
EQ_FOR_OP = {"WFR_RCV": "", "WFR_SAW": "DS-01", "WIRE_BOND": "WB-01", "FT": "FT-01"}


async def advance_to(db, users, lot_id: str, target_op: str) -> dict:
    """把批號一路跑到指定站別的「待進站」狀態。"""
    lot = await lot_service.get_lot(db, lot_id, raw=True)
    guard = 0
    while lot["current_op"] != target_op:
        guard += 1
        assert guard < 20, f"流程未經過 {target_op}"
        await run_step(db, users, lot_id, EQ_FOR_OP[lot["current_op"]])
        lot = await lot_service.get_lot(db, lot_id, raw=True)
    return lot
