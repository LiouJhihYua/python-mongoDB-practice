"""批號主流程：開批 → 進出站 → 單位換算 → 完工。"""

from datetime import timedelta

import pytest

from app.errors import StateError, ValidationError
from app.models.base import utcnow
from app.models.enums import EquipmentState, LotStatus, UnitType
from app.services import lot_service, workorder_service
from tests.conftest import GROSS_DIE
from tests.helpers import advance_to, make_lot, make_work_order, run_step


async def test_create_lot_from_work_order(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    lot = await make_lot(db, qty=5, wo_no=wo_no)

    assert lot["status"] == LotStatus.WAITING.value
    assert lot["current_seq"] == 10
    assert lot["current_op"] == "WFR_RCV"
    assert lot["qty"] == 5
    assert lot["unit_type"] == UnitType.WAFER.value
    assert len(lot["wafer_ids"]) == 5

    wo = await workorder_service.get_work_order(db, wo_no, raw=True)
    assert wo["released_qty"] == 5
    assert wo["status"] == "IN_PROGRESS"

    # 晶圓已被綁定，不可重複投入
    wafer = await db.fetchrow("SELECT * FROM wafers WHERE wafer_id = $1", lot["wafer_ids"][0])
    assert wafer["consumed"] is True
    assert wafer["assembly_lot_id"] == lot["lot_id"]


async def test_cannot_release_more_than_plan(factory):
    db = factory
    wo_no = await make_work_order(db, plan_qty=10)
    await make_lot(db, qty=8, wo_no=wo_no)
    with pytest.raises(ValidationError, match="超過工單剩餘可投料量"):
        await make_lot(db, qty=5, wo_no=wo_no)


async def test_cannot_open_lot_on_draft_work_order(factory):
    db = factory
    wo = await workorder_service.create_work_order(
        db,
        {"device_id": "TEST-QFN48", "plan_qty": 10, "unit_type": "WAFER",
         "due_date": utcnow() + timedelta(days=7), "priority": 5, "customer_po": "", "remark": ""},
        "planner01",
    )
    with pytest.raises(StateError, match="需先下達"):
        await make_lot(db, qty=2, wo_no=wo["wo_no"])


async def test_wafer_to_die_conversion(factory, users):
    """OSAT 關鍵：切割站把「片」換算成「顆」，良率必須以換算後數量為分母。"""
    db = factory
    lot = await make_lot(db, qty=5)
    await run_step(db, users, lot["lot_id"])  # WFR_RCV

    current = await lot_service.get_lot(db, lot["lot_id"], raw=True)
    assert current["current_op"] == "WFR_SAW"
    assert current["qty"] == 5 and current["unit_type"] == UnitType.WAFER.value

    result = await run_step(db, users, lot["lot_id"], "DS-01", reject=50, defect_code="SAW-CHIP")
    assert result["last_step"]["qty_in"] == 5
    assert result["last_step"]["qty_expected"] == 5 * GROSS_DIE
    assert result["last_step"]["qty_good"] == 5 * GROSS_DIE - 50
    assert result["last_step"]["step_yield"] == pytest.approx(4950 / 5000)
    assert result["qty"] == 4950
    assert result["unit_type"] == UnitType.DIE.value
    assert result["scrap_qty"] == 50


async def test_full_route_to_completion(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    lot_id = lot["lot_id"]

    await run_step(db, users, lot_id)                                        # WFR_RCV
    await run_step(db, users, lot_id, "DS-01", 20, "SAW-CHIP")               # WFR_SAW: 2000 → 1980
    await run_step(db, users, lot_id, "WB-01", 30, "WB-NSOP")                # WIRE_BOND: → 1950

    # FT 以 Bin 分佈出站
    await lot_service.track_in(db, {"lot_id": lot_id, "eq_id": "FT-01"}, users["op001"])
    final = await lot_service.track_out(
        db,
        {"lot_id": lot_id, "good_qty": None, "reject_qty": 0,
         "defects": [{"defect_code": "FT-OPEN", "qty": 45}],
         "bin_map": {"1": 1905, "2": 45}, "materials": [], "remark": ""},
        users["op001"],
    )

    assert final["status"] == LotStatus.COMPLETED.value
    assert final["qty"] == 1905
    assert final["completed_at"] is not None
    assert final["scrap_qty"] == 20 + 30 + 45

    history = await lot_service.lot_history(db, lot_id)
    actions = [h["action"] for h in history]
    assert actions.count("TRACK_IN") == 4
    assert actions.count("TRACK_OUT") == 4
    assert actions[0] == "CREATE"

    # 每筆出站都記錄了班別，供班別產出報表使用
    assert all(h["shift"] for h in history)


async def test_equipment_state_follows_lot(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])  # 進到 WFR_SAW

    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])
    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'DS-01'")
    assert eq["current_state"] == EquipmentState.PRODUCTIVE.value
    assert eq["current_lot_id"] == lot["lot_id"]

    await run_step_out(db, users, lot["lot_id"])
    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'DS-01'")
    assert eq["current_state"] == EquipmentState.STANDBY.value
    assert eq["current_lot_id"] is None


async def run_step_out(db, users, lot_id: str) -> dict:
    lot = await lot_service.get_lot(db, lot_id, raw=True)
    operation = await db.fetchrow("SELECT * FROM operations WHERE op_code = $1", lot["current_op"])
    device = await db.fetchrow("SELECT * FROM devices WHERE device_id = $1", lot["device_id"])
    expected, _ = lot_service.compute_expected_output(
        int(lot["qty"]), operation, device, lot["unit_type"]
    )
    return await lot_service.track_out(
        db,
        {"lot_id": lot_id, "good_qty": expected, "reject_qty": 0, "defects": [],
         "materials": [], "bin_map": None, "remark": ""},
        users["op001"],
    )


async def test_scrapped_when_no_good_units_left(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await run_step(db, users, lot["lot_id"])
    result = await run_step(db, users, lot["lot_id"], "DS-01", reject=GROSS_DIE, defect_code="SAW-CHIP")
    assert result["status"] == LotStatus.SCRAPPED.value
    assert result["qty"] == 0


async def test_material_consumption_recorded(factory, users):
    db = factory
    await db.execute(
        "INSERT INTO materials (material_id, name, material_type, uom, on_hand_qty, safety_stock) "
        "VALUES ('WIRE-AU-08', '金線', 'WIRE', 'M', 1000.0, 100.0)"
    )
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])
    current = await lot_service.get_lot(db, lot["lot_id"], raw=True)
    await lot_service.track_out(
        db,
        {"lot_id": lot["lot_id"], "good_qty": current["qty"], "reject_qty": 0, "defects": [],
         "materials": [{"material_id": "WIRE-AU-08", "material_lot": "AU-L01", "qty": 120.5}],
         "bin_map": None, "remark": ""},
        users["op001"],
    )
    material = await db.fetchrow("SELECT * FROM materials WHERE material_id = 'WIRE-AU-08'")
    assert material["on_hand_qty"] == pytest.approx(879.5)
    txn = await db.fetchrow("SELECT * FROM material_transactions WHERE lot_id = $1", lot["lot_id"])
    assert txn["qty"] == -120.5 and txn["material_lot"] == "AU-L01"

    history = await db.fetchrow(
        "SELECT * FROM lot_history WHERE lot_id = $1 AND op_code = 'WIRE_BOND' AND action = 'TRACK_OUT'",
        lot["lot_id"],
    )
    assert history["materials"][0]["material_id"] == "WIRE-AU-08"


async def test_material_shortage_blocks_track_out(factory, users):
    db = factory
    await db.execute(
        "INSERT INTO materials (material_id, name, material_type, uom, on_hand_qty, safety_stock) "
        "VALUES ('WIRE-AU-08', '金線', 'WIRE', 'M', 10.0, 0.0)"
    )
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])
    current = await lot_service.get_lot(db, lot["lot_id"], raw=True)
    with pytest.raises(ValidationError, match="庫存不足"):
        await lot_service.track_out(
            db,
            {"lot_id": lot["lot_id"], "good_qty": current["qty"], "reject_qty": 0, "defects": [],
             "materials": [{"material_id": "WIRE-AU-08", "material_lot": "AU-L01", "qty": 999.0}],
             "bin_map": None, "remark": ""},
            users["op001"],
        )
