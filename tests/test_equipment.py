"""設備狀態機、狀態履歷與 OEE 計算。"""

from datetime import timedelta

import pytest

from app.errors import ValidationError
from app.models.base import ensure_aware
from app.models.enums import EquipmentState, PMStatus
from app.services import equipment_service


async def test_state_transition_closes_previous_log(factory, clock):
    db = factory
    start = clock.now
    await equipment_service.set_state(db, "WB-01", EquipmentState.PRODUCTIVE, "op001", "TRACK_IN")
    clock.advance(hours=2)
    await equipment_service.set_state(db, "WB-01", EquipmentState.UNSCHEDULED_DOWN, "eng01", "EQ-ALARM")

    logs = await db.fetch(
        "SELECT * FROM equipment_state_logs WHERE eq_id = 'WB-01' ORDER BY start_time, id"
    )
    productive = [log for log in logs if log["state"] == EquipmentState.PRODUCTIVE.value][-1]
    assert productive["end_time"] is not None
    assert productive["duration_sec"] == pytest.approx(7200, abs=1)

    open_log = [log for log in logs if log["end_time"] is None]
    assert len(open_log) == 1 and open_log[0]["state"] == EquipmentState.UNSCHEDULED_DOWN.value

    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'WB-01'")
    assert eq["current_state"] == EquipmentState.UNSCHEDULED_DOWN.value
    assert ensure_aware(eq["state_since"]) >= start


async def test_oee_math(factory, clock):
    """OEE = 稼動率 × 效能 × 良率，時間依 SEMI E10 分類。"""
    db = factory
    start = clock.now

    await equipment_service.set_state(db, "FT-01", EquipmentState.PRODUCTIVE, "op001")
    clock.advance(hours=2)
    await equipment_service.set_state(db, "FT-01", EquipmentState.STANDBY, "op001")
    clock.advance(hours=1)
    await equipment_service.set_state(db, "FT-01", EquipmentState.UNSCHEDULED_DOWN, "eng01", "EQ-ALARM")
    clock.advance(hours=1)
    end = clock.now

    # 該設備在區間內出站 6,200 顆（良品 6,000）
    await db.execute(
        "INSERT INTO lot_history (lot_id, eq_id, action, timestamp, qty_good, qty_reject, process_sec) "
        "VALUES ('LX', 'FT-01', 'TRACK_OUT', $1, 6000, 200, 3600.0)",
        start + timedelta(hours=1),
    )

    oee = await equipment_service.calc_oee(db, "FT-01", start, end)
    breakdown = oee["time_breakdown_sec"]
    assert breakdown["PRODUCTIVE"] == pytest.approx(7200, abs=1)
    assert breakdown["STANDBY"] == pytest.approx(3600, abs=1)
    assert breakdown["UNSCHEDULED_DOWN"] == pytest.approx(3600, abs=1)

    assert oee["scheduled_sec"] == pytest.approx(14400, abs=1)
    assert oee["availability"] == pytest.approx(0.5, abs=0.01)          # 7200 / 14400
    assert oee["quality"] == pytest.approx(6000 / 6200, abs=0.001)
    assert oee["performance"] == pytest.approx(0.1 * 6200 / 7200, abs=0.001)
    assert oee["oee"] == pytest.approx(
        oee["availability"] * oee["performance"] * oee["quality"], abs=0.001
    )
    assert oee["units_processed"] == 6200
    assert oee["uptime_ratio"] == pytest.approx((7200 + 3600) / 14400, abs=0.01)


async def test_oee_excludes_non_scheduled_time(factory, clock):
    """非排程時間不計入稼動率分母，停工不該被算成設備不良。"""
    db = factory
    start = clock.now
    await equipment_service.set_state(db, "FT-01", EquipmentState.NON_SCHEDULED, "eng01", "無班")
    clock.advance(hours=2)
    await equipment_service.set_state(db, "FT-01", EquipmentState.PRODUCTIVE, "op001")
    clock.advance(hours=2)
    end = clock.now

    await db.execute(
        "INSERT INTO lot_history (lot_id, eq_id, action, timestamp, qty_good, qty_reject, process_sec) "
        "VALUES ('LX', 'FT-01', 'TRACK_OUT', $1, 1000, 0, 3600.0)",
        start + timedelta(hours=3),
    )
    oee = await equipment_service.calc_oee(db, "FT-01", start, end)
    assert oee["scheduled_sec"] == pytest.approx(7200, abs=1)
    assert oee["availability"] == pytest.approx(1.0, abs=0.01)
    assert oee["utilization"] == pytest.approx(0.5, abs=0.01)  # 以總時間為分母


async def test_oee_window_must_be_valid(factory, clock):
    db = factory
    with pytest.raises(ValidationError, match="結束時間"):
        await equipment_service.calc_oee(db, "FT-01", clock.now, clock.now)


async def test_state_summary(factory):
    db = factory
    await equipment_service.set_state(db, "DS-01", EquipmentState.PRODUCTIVE, "op001")
    await equipment_service.set_state(db, "WB-01", EquipmentState.UNSCHEDULED_DOWN, "eng01", "EQ-ALARM")
    summary = await equipment_service.state_summary(db)
    assert summary["total"] == 3
    assert summary["by_state"]["PRODUCTIVE"]["count"] == 1
    assert summary["by_state"]["UNSCHEDULED_DOWN"]["equipments"] == ["WB-01"]
    assert summary["uptime_ratio"] == pytest.approx(2 / 3, abs=0.01)


async def test_pm_completion_schedules_next(factory, clock):
    db = factory
    pm = await equipment_service.create_pm(
        db, "WB-01", "季保養", clock.now + timedelta(days=1), "eng01", "例行"
    )
    assert pm["status"] == PMStatus.PLANNED.value

    done = await equipment_service.complete_pm(db, pm["id"], "op001", "已更換耗材")
    assert done["status"] == PMStatus.DONE.value
    assert done["performed_by"] == "op001"

    # 依保養週期自動排下一次（設備 pm_interval_days = 90）
    tasks = await equipment_service.list_pm(db, eq_id="WB-01", status=PMStatus.PLANNED.value)
    assert len(tasks) == 1
    assert ensure_aware(tasks[0]["due_date"]) > clock.now + timedelta(days=80)


async def test_overdue_pm_flagged(factory, clock):
    db = factory
    await equipment_service.create_pm(db, "WB-01", "季保養", clock.now - timedelta(days=2), "eng01")
    tasks = await equipment_service.list_pm(db, eq_id="WB-01")
    assert tasks[0]["status"] == PMStatus.OVERDUE.value
