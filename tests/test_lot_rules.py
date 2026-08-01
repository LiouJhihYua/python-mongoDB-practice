"""生產執行的管制規則：狀態、設備、人員資格、數量結平與 Q-Time。"""

import pytest

from app.errors import PermissionError_, StateError, ValidationError
from app.models.enums import EquipmentState, LotStatus
from app.services import equipment_service, lot_service
from tests.helpers import EQ_FOR_OP, advance_to, make_lot, run_step


# ── 扣留 ────────────────────────────────────────────────────
async def test_hold_blocks_track_in_and_release_restores(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    lot_id = lot["lot_id"]

    held = await lot_service.hold_lot(
        db, {"lot_id": lot_id, "reason": "QUALITY", "remark": "客訴複判"}, users["qc01"]
    )
    assert held["status"] == LotStatus.HOLD.value

    with pytest.raises(StateError, match="扣留中"):
        await lot_service.track_in(db, {"lot_id": lot_id, "eq_id": ""}, users["op001"])

    hold_doc = await db["holds"].find_one({"lot_id": lot_id, "status": "OPEN"})
    assert hold_doc["reason"] == "QUALITY" and hold_doc["held_by"] == "qc01"

    released = await lot_service.release_lot(
        db, {"lot_id": lot_id, "remark": "確認無異常"}, users["qc01"]
    )
    assert released["status"] == LotStatus.WAITING.value
    assert (await db["holds"].find_one({"lot_id": lot_id}))["status"] == "RELEASED"


async def test_double_hold_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await lot_service.hold_lot(db, {"lot_id": lot["lot_id"], "reason": "QUALITY", "remark": ""}, users["qc01"])
    with pytest.raises(StateError, match="狀態為 HOLD，不可扣留"):
        await lot_service.hold_lot(db, {"lot_id": lot["lot_id"], "reason": "QUALITY", "remark": ""}, users["qc01"])
    # 只會留下一筆未結案的扣留紀錄
    assert await db["holds"].count_documents({"lot_id": lot["lot_id"], "status": "OPEN"}) == 1


async def test_release_without_hold_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    with pytest.raises(StateError, match="沒有扣留紀錄"):
        await lot_service.release_lot(db, {"lot_id": lot["lot_id"], "remark": ""}, users["qc01"])


# ── 設備管制 ────────────────────────────────────────────────
async def test_equipment_required(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])  # 到 WFR_SAW
    with pytest.raises(ValidationError, match="必須指定設備"):
        await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": ""}, users["op001"])


async def test_equipment_capability_checked(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    with pytest.raises(ValidationError, match="不具備 WFR_SAW"):
        await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])


async def test_equipment_down_cannot_accept_lot(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await equipment_service.set_state(
        db, "DS-01", EquipmentState.UNSCHEDULED_DOWN, "eng01", "EQ-ALARM", "主軸異常"
    )
    with pytest.raises(StateError, match="不可投料"):
        await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])


async def test_equipment_occupied_by_other_lot(factory, users):
    db = factory
    first = await make_lot(db, qty=2)
    second = await make_lot(db, qty=2)
    await run_step(db, users, first["lot_id"])
    await run_step(db, users, second["lot_id"])
    await lot_service.track_in(db, {"lot_id": first["lot_id"], "eq_id": "DS-01"}, users["op001"])
    with pytest.raises(StateError, match="正在加工批號"):
        await lot_service.track_in(db, {"lot_id": second["lot_id"], "eq_id": "DS-01"}, users["op001"])


# ── 人員資格 ────────────────────────────────────────────────
async def test_uncertified_operator_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    with pytest.raises(PermissionError_, match="未取得 WFR_SAW 站別資格"):
        await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op002"])


async def test_engineer_treated_as_certified(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    result = await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["eng01"])
    assert result["status"] == LotStatus.RUNNING.value


# ── 狀態機 ──────────────────────────────────────────────────
async def test_track_out_requires_track_in(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    with pytest.raises(StateError, match="需先 Track-In"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": 2, "reject_qty": 0, "defects": [],
                 "materials": [], "bin_map": None, "remark": ""}, users["op001"]
        )


async def test_double_track_in_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": ""}, users["op001"])
    with pytest.raises(StateError, match="加工中"):
        await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": ""}, users["op001"])


# ── 數量結平 ────────────────────────────────────────────────
async def test_quantity_must_balance(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])
    with pytest.raises(ValidationError, match="應等於應產出量"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": 100, "reject_qty": 0, "defects": [],
                 "materials": [], "bin_map": None, "remark": ""}, users["op001"]
        )


async def test_defect_detail_must_match_reject_qty(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])
    with pytest.raises(ValidationError, match="不良明細合計"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": 1990, "reject_qty": 10,
                 "defects": [{"defect_code": "SAW-CHIP", "qty": 7}],
                 "materials": [], "bin_map": None, "remark": ""}, users["op001"]
        )


async def test_reject_requires_defect_detail(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])
    with pytest.raises(ValidationError, match="必須填寫不良代碼"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": 1990, "reject_qty": 10, "defects": [],
                 "materials": [], "bin_map": None, "remark": ""}, users["op001"]
        )


async def test_defect_code_must_apply_to_operation(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])
    with pytest.raises(ValidationError, match="不適用於站別"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": 1990, "reject_qty": 10,
                 "defects": [{"defect_code": "FT-OPEN", "qty": 10}],
                 "materials": [], "bin_map": None, "remark": ""}, users["op001"]
        )


async def test_unknown_defect_code_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])
    with pytest.raises(ValidationError, match="不良代碼不存在"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": 1990, "reject_qty": 10,
                 "defects": [{"defect_code": "NO-SUCH", "qty": 10}],
                 "materials": [], "bin_map": None, "remark": ""}, users["op001"]
        )


async def test_bin_map_only_on_test_operation(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])
    with pytest.raises(ValidationError, match="非測試站"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": None, "reject_qty": 0, "defects": [],
                 "materials": [], "bin_map": {"1": 2000}, "remark": ""}, users["op001"]
        )


async def test_bin_map_total_must_match(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "FT")
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "FT-01"}, users["op001"])
    with pytest.raises(ValidationError, match="Bin 合計"):
        await lot_service.track_out(
            db, {"lot_id": lot["lot_id"], "good_qty": None, "reject_qty": 0, "defects": [],
                 "materials": [], "bin_map": {"1": 5}, "remark": ""}, users["op001"]
        )


# ── Q-Time ──────────────────────────────────────────────────
async def test_qtime_violation_auto_holds_then_waived_after_release(factory, users, clock):
    """打線後 60 分鐘內須進封膠站；逾時自動扣留，品保放行後給予一次性特採。"""
    db = factory
    lot = await make_lot(db, qty=1)
    lot_id = lot["lot_id"]
    await advance_to(db, users, lot_id, "WIRE_BOND")

    clock.advance(hours=3)  # 遠超過 WIRE_BOND 的 60 分鐘 Q-Time 上限
    with pytest.raises(StateError, match="Q-Time 逾時"):
        await lot_service.track_in(db, {"lot_id": lot_id, "eq_id": "WB-01"}, users["op001"])

    held = await lot_service.get_lot(db, lot_id, raw=True)
    assert held["status"] == LotStatus.HOLD.value
    assert held["qtime_violations"] == 1
    hold_doc = await db["holds"].find_one({"lot_id": lot_id, "status": "OPEN"})
    assert hold_doc["reason"] == "QTIME"

    # 放行後可以進站，不會再度被自動扣留（否則會形成死循環）
    await lot_service.release_lot(db, {"lot_id": lot_id, "remark": "品保特採"}, users["qc01"])
    result = await lot_service.track_in(db, {"lot_id": lot_id, "eq_id": "WB-01"}, users["op001"])
    assert result["status"] == LotStatus.RUNNING.value
    assert result["qtime_waived_seq"] is None  # 特採用掉即失效

    history = await db["lot_history"].find_one(
        {"lot_id": lot_id, "op_code": "WIRE_BOND", "action": "TRACK_IN"}
    )
    assert history["qtime_violation"] is True
    assert history["queue_sec"] >= 3 * 3600


async def test_within_qtime_no_violation(factory, users, clock):
    db = factory
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    clock.advance(minutes=30)
    result = await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])
    assert result["status"] == LotStatus.RUNNING.value
    assert result["qtime_violations"] == 0


# ── 報廢與重工 ──────────────────────────────────────────────
async def test_partial_scrap(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await run_step(db, users, lot["lot_id"], "DS-01")  # 2000 顆
    result = await lot_service.scrap_lot(
        db, {"lot_id": lot["lot_id"], "qty": 500, "defect_code": "HND-DROP", "remark": "落料"}, users["qc01"]
    )
    assert result["qty"] == 1500
    assert result["scrap_qty"] == 500
    assert result["status"] == LotStatus.WAITING.value
    assert await db["defect_records"].count_documents({"lot_id": lot["lot_id"]}) == 1


async def test_scrap_more_than_on_hand_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    with pytest.raises(ValidationError, match="超過批號現有量"):
        await lot_service.scrap_lot(
            db, {"lot_id": lot["lot_id"], "qty": 99, "defect_code": "HND-DROP", "remark": ""}, users["qc01"]
        )


async def test_full_scrap_marks_lot_scrapped(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    result = await lot_service.scrap_lot(
        db, {"lot_id": lot["lot_id"], "qty": 2, "defect_code": "HND-DROP", "remark": ""}, users["qc01"]
    )
    assert result["status"] == LotStatus.SCRAPPED.value


async def test_rework_returns_lot_to_earlier_step(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "FT")
    result = await lot_service.rework_lot(
        db, {"lot_id": lot["lot_id"], "to_seq": 30, "reason": "外觀重工"}, users["qc01"]
    )
    assert result["current_seq"] == 30
    assert result["current_op"] == "WIRE_BOND"
    assert result["rework_count"] == 1
    assert result["status"] == LotStatus.WAITING.value


async def test_rework_forward_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await run_step(db, users, lot["lot_id"])
    with pytest.raises(ValidationError, match="必須小於目前站序"):
        await lot_service.rework_lot(
            db, {"lot_id": lot["lot_id"], "to_seq": 40, "reason": ""}, users["qc01"]
        )


# ── 出貨 ────────────────────────────────────────────────────
async def test_ship_requires_completed_lots(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    with pytest.raises(StateError, match="尚未完工"):
        await lot_service.ship_lots(
            db, {"customer_code": "MTK", "lot_ids": [lot["lot_id"]], "customer_po": "", "remark": ""},
            users["planner01"],
        )


async def test_ship_completed_lot(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    lot_id = lot["lot_id"]
    for op in ["WFR_RCV", "WFR_SAW", "WIRE_BOND", "FT"]:
        await run_step(db, users, lot_id, EQ_FOR_OP[op])
    assert (await lot_service.get_lot(db, lot_id, raw=True))["status"] == LotStatus.COMPLETED.value

    shipment = await lot_service.ship_lots(
        db, {"customer_code": "MTK", "lot_ids": [lot_id], "customer_po": "PO-1", "remark": ""},
        users["planner01"],
    )
    assert shipment["shipment_no"].startswith("SH")
    shipped = await lot_service.get_lot(db, lot_id, raw=True)
    assert shipped["status"] == LotStatus.SHIPPED.value
    assert shipped["shipment_no"] == shipment["shipment_no"]


async def test_ship_wrong_customer_rejected(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    lot_id = lot["lot_id"]
    for op in ["WFR_RCV", "WFR_SAW", "WIRE_BOND", "FT"]:
        await run_step(db, users, lot_id, EQ_FOR_OP[op])
    with pytest.raises(ValidationError, match="不屬於客戶"):
        await lot_service.ship_lots(
            db, {"customer_code": "QCM", "lot_ids": [lot_id], "customer_po": "", "remark": ""},
            users["planner01"],
        )
