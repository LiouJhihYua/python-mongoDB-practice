"""SPC：量測收集、判異規則、管制界限與製程能力。"""

from datetime import timedelta

import pytest

from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import utcnow
from app.models.enums import LotStatus, SPCRule
from app.services import lot_service, spc_service
from tests.helpers import advance_to, make_lot, run_step


async def _lot_at_wire_bond(db, users):
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])
    return lot["lot_id"]


async def _measure(db, users, lot_id, values, item="WB-PULL"):
    return await spc_service.record_measurement(
        db, {"item_code": item, "lot_id": lot_id, "values": values, "eq_id": "WB-01", "remark": ""},
        users["qc01"],
    )


# ── 量測收集 ────────────────────────────────────────────────
async def test_record_measurement(factory, users):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    result = await _measure(db, users, lot_id, [7.0, 7.2, 6.8, 7.1, 6.9])

    assert result["mean"] == pytest.approx(7.0)
    assert result["range"] == pytest.approx(0.4)
    assert result["out_of_spec"] is False
    assert result["violations"] == []
    assert result["lot_held"] is False
    assert result["op_code"] == "WIRE_BOND"


async def test_out_of_spec_flags_and_holds_lot(factory, users):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    result = await _measure(db, users, lot_id, [7.0, 7.1, 2.4, 6.9, 7.0])  # 2.4 低於 LSL 3.0

    assert result["out_of_spec"] is True
    assert result["out_of_spec_values"] == [2.4]
    assert SPCRule.OUT_OF_SPEC.value in result["violations"]
    assert result["lot_held"] is True

    lot = await lot_service.get_lot(db, lot_id, raw=True)
    assert lot["status"] == LotStatus.HOLD.value
    hold = await db.fetchrow("SELECT * FROM holds WHERE lot_id = $1 AND status = 'OPEN'", lot_id)
    assert "SPC 異常" in hold["remark"]


async def test_auto_hold_can_be_disabled_per_item(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await run_step(db, users, lot["lot_id"])  # 到 WFR_SAW
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "DS-01"}, users["op001"])

    result = await spc_service.record_measurement(
        db,
        {"item_code": "SAW-KERF", "lot_id": lot["lot_id"], "values": [30, 31, 99, 29, 30],
         "eq_id": "DS-01", "remark": ""},
        users["qc01"],
    )
    assert result["out_of_spec"] is True
    assert result["lot_held"] is False  # SAW-KERF 設定為不自動扣留
    assert (await lot_service.get_lot(db, lot["lot_id"], raw=True))["status"] == LotStatus.RUNNING.value


async def test_held_lot_can_still_track_out(factory, users):
    """加工中被 SPC 扣留的批號仍須能出站，否則料會卡死在機台上。"""
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    await _measure(db, users, lot_id, [7.0, 7.1, 2.0, 6.9, 7.0])
    current = await lot_service.get_lot(db, lot_id, raw=True)
    assert current["status"] == LotStatus.HOLD.value

    result = await lot_service.track_out(
        db,
        {"lot_id": lot_id, "good_qty": current["qty"], "reject_qty": 0, "defects": [],
         "materials": [], "bin_map": None, "remark": ""},
        users["op001"],
    )
    # 料離開機台，但扣留狀態保留到下一站
    assert result["status"] == LotStatus.HOLD.value
    assert result["current_op"] == "FT"
    assert result["eq_id"] is None
    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'WB-01'")
    assert eq["current_lot_id"] is None

    # 放行後回到待進站
    released = await lot_service.release_lot(db, {"lot_id": lot_id, "remark": "特採"}, users["qc01"])
    assert released["status"] == LotStatus.WAITING.value


async def test_measurement_rejects_wrong_station(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)  # 還在 WFR_RCV
    with pytest.raises(ValidationError, match="與量測項目所屬站別"):
        await _measure(db, users, lot["lot_id"], [7.0, 7.0, 7.0, 7.0, 7.0])


async def test_measurement_rejects_wrong_sample_size(factory, users):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    with pytest.raises(ValidationError, match="子群大小"):
        await _measure(db, users, lot_id, [7.0, 7.0, 7.0])


async def test_measurement_unknown_item(factory, users):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    with pytest.raises(NotFoundError, match="找不到量測項目"):
        await _measure(db, users, lot_id, [7.0] * 5, item="NO-SUCH")


# ── 判異規則（純函式，直接驗數學）────────────────────────────
def test_rule_beyond_3sigma():
    means = [10.0] * 8 + [14.0]
    assert SPCRule.BEYOND_3SIGMA.value in spc_service.check_rules(means, center=10.0, sigma=1.0)


def test_rule_run_9_same_side():
    means = [10.5] * 9
    rules = spc_service.check_rules(means, center=10.0, sigma=1.0)
    assert SPCRule.RUN_9_SAME_SIDE.value in rules
    assert SPCRule.BEYOND_3SIGMA.value not in rules


def test_rule_trend_6():
    means = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]
    assert SPCRule.TREND_6.value in spc_service.check_rules(means, center=10.3, sigma=1.0)


def test_rule_two_of_three_2sigma():
    means = [10.0, 12.5, 10.0, 12.6]
    assert SPCRule.TWO_OF_THREE_2SIGMA.value in spc_service.check_rules(means, center=10.0, sigma=1.0)


def test_no_rules_when_process_stable():
    means = [10.0, 10.2, 9.8, 10.1, 9.9, 10.0, 10.1, 9.9, 10.0]
    assert spc_service.check_rules(means, center=10.0, sigma=1.0) == []


def test_rules_need_variation():
    assert spc_service.check_rules([10.0, 10.0], center=10.0, sigma=0.0) == []


# ── 管制界限 ────────────────────────────────────────────────
def test_limits_from_subgroups_math():
    """UCL = X̿ + A2·R̄，且判異用的 σ 必須是 A2·R̄/3。"""
    subgroups = [{"mean": 10.0, "range": 2.0} for _ in range(20)]
    limits = spc_service.limits_from_subgroups(subgroups, sample_size=5)
    a2 = spc_service.CHART_CONSTANTS[5][0]
    assert limits["x_bar_bar"] == pytest.approx(10.0)
    assert limits["r_bar"] == pytest.approx(2.0)
    assert limits["x_ucl"] == pytest.approx(10.0 + a2 * 2.0)
    assert limits["x_lcl"] == pytest.approx(10.0 - a2 * 2.0)
    assert limits["sigma_xbar"] == pytest.approx(a2 * 2.0 / 3)
    assert limits["r_ucl"] == pytest.approx(spc_service.CHART_CONSTANTS[5][2] * 2.0)


async def test_establish_limits_requires_baseline(factory, users):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    await _measure(db, users, lot_id, [7.0, 7.1, 6.9, 7.0, 7.0])
    with pytest.raises(ValidationError, match="需至少"):
        await spc_service.establish_limits(db, "WB-PULL")


async def test_limits_are_frozen_after_baseline(factory, users, clock):
    """界限一旦建立就固定，之後製程漂移才測得出來。"""
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    for _ in range(spc_service.BASELINE_SUBGROUPS):
        clock.advance(minutes=1)
        await _measure(db, users, lot_id, [7.0, 7.2, 6.8, 7.1, 6.9])

    item = await spc_service.get_item(db, "WB-PULL")
    limits = item["control_limits"]
    assert limits["x_bar_bar"] == pytest.approx(7.0, abs=0.01)
    assert limits["subgroups"] == spc_service.BASELINE_SUBGROUPS
    assert limits["established_by"] == "auto"

    # 製程整體上移，超出固定界限即判異
    clock.advance(minutes=1)
    result = await _measure(db, users, lot_id, [9.0, 9.2, 8.8, 9.1, 8.9])
    assert SPCRule.BEYOND_3SIGMA.value in result["violations"]

    # 界限不會被新資料改寫
    item = await spc_service.get_item(db, "WB-PULL")
    assert item["control_limits"]["x_bar_bar"] == pytest.approx(limits["x_bar_bar"])


async def test_control_chart_uses_established_limits(factory, users, clock):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    for _ in range(spc_service.BASELINE_SUBGROUPS):
        clock.advance(minutes=1)
        await _measure(db, users, lot_id, [7.0, 7.2, 6.8, 7.1, 6.9])

    start, end = clock.now - timedelta(hours=1), clock.now + timedelta(minutes=1)
    chart = await spc_service.control_chart(db, "WB-PULL", start, end)
    assert chart["limits_source"] == "已建立的基準期"
    assert len(chart["points"]) == spc_service.BASELINE_SUBGROUPS
    assert chart["item"]["usl"] == 12.0
    assert chart["control_limits"]["x_ucl"] > chart["control_limits"]["x_bar_bar"]


async def test_control_chart_without_enough_data(factory, users):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    await _measure(db, users, lot_id, [7.0] * 5)
    now = utcnow()
    chart = await spc_service.control_chart(db, "WB-PULL", now - timedelta(hours=1), now + timedelta(minutes=1))
    assert chart["control_limits"] is None
    assert "尚無法建立管制界限" in chart["note"]


# ── 製程能力 ────────────────────────────────────────────────
async def test_capability_two_sided(factory, users, clock):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    for _ in range(6):
        clock.advance(minutes=1)
        await _measure(db, users, lot_id, [7.0, 7.5, 6.5, 7.2, 6.8])

    start, end = clock.now - timedelta(hours=1), clock.now + timedelta(minutes=1)
    result = await spc_service.capability(db, "WB-PULL", start, end)
    assert result["sample_count"] == 30
    assert result["mean"] == pytest.approx(7.0, abs=0.01)
    # 規格 3~12，σ 約 0.36 → Cp 遠大於 1.33
    assert result["cp"] > 1.33
    assert result["cpk"] <= result["cp"]
    assert result["ca"] == pytest.approx(0.0, abs=0.01)  # 對準目標值 7.0
    assert result["grade"].startswith("A")


async def test_capability_one_sided_spec(factory, users, clock):
    """只有單邊規格時 Cp 無意義，但 Cpk 仍可算。"""
    db = factory
    await db.execute("UPDATE measurement_items SET usl = NULL, target = NULL WHERE item_code = 'WB-PULL'")
    lot_id = await _lot_at_wire_bond(db, users)
    for _ in range(6):
        clock.advance(minutes=1)
        await _measure(db, users, lot_id, [7.0, 7.5, 6.5, 7.2, 6.8])

    result = await spc_service.capability(db, "WB-PULL", clock.now - timedelta(hours=1), clock.now + timedelta(minutes=1))
    assert result["cp"] is None
    assert result["cpk"] is not None and result["cpk"] > 0
    assert result["ca"] is None


async def test_capability_insufficient_data(factory):
    db = factory
    now = utcnow()
    result = await spc_service.capability(db, "WB-PULL", now - timedelta(hours=1), now)
    assert result["cpk"] is None
    assert "無法計算" in result["note"]


async def test_violation_summary(factory, users, clock):
    db = factory
    lot_id = await _lot_at_wire_bond(db, users)
    for _ in range(spc_service.BASELINE_SUBGROUPS):
        clock.advance(minutes=1)
        await _measure(db, users, lot_id, [7.0, 7.2, 6.8, 7.1, 6.9])
    clock.advance(minutes=1)
    await _measure(db, users, lot_id, [1.0, 1.2, 0.8, 1.1, 0.9])  # 超規且超出界限

    rows = await spc_service.violation_summary(db, clock.now - timedelta(hours=1), clock.now + timedelta(minutes=1))
    assert rows[0]["item_code"] == "WB-PULL"
    assert rows[0]["violations"] >= 1
    assert lot_id in rows[0]["affected_lots"]


async def test_item_spec_validation(factory):
    db = factory
    with pytest.raises(ValidationError, match="至少要設定一個"):
        await spc_service.create_item(
            db, {"item_code": "X1", "name": "X", "op_code": "FT", "usl": None, "lsl": None,
                 "sample_size": 5}, "test"
        )
    with pytest.raises(ValidationError, match="必須大於下限"):
        await spc_service.create_item(
            db, {"item_code": "X2", "name": "X", "op_code": "FT", "usl": 1.0, "lsl": 5.0,
                 "sample_size": 5}, "test"
        )
    with pytest.raises(ValidationError, match="站別不存在"):
        await spc_service.create_item(
            db, {"item_code": "X3", "name": "X", "op_code": "GHOST", "usl": 5.0, "lsl": 1.0,
                 "sample_size": 5}, "test"
        )
    created = await spc_service.create_item(
        db, {"item_code": "X4", "name": "X", "op_code": "FT", "usl": 10.0, "lsl": 2.0,
             "sample_size": 5}, "test"
    )
    assert created["target"] == pytest.approx(6.0)  # 未指定時取規格中心


async def test_inactive_item_rejected(factory, users):
    db = factory
    await db.execute("UPDATE measurement_items SET active = FALSE WHERE item_code = 'WB-PULL'")
    lot_id = await _lot_at_wire_bond(db, users)
    with pytest.raises(StateError, match="已停用"):
        await _measure(db, users, lot_id, [7.0] * 5)
