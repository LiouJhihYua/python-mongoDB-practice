"""派工排序、Q-Time 預警、稽核軌跡與交接班報表。"""

from datetime import timedelta

import pytest

from app.models.base import shift_of, shift_window, utcnow
from app.services import audit_service, dispatch_service, equipment_service, lot_service, report_service
from app.models.enums import EquipmentState
from tests.helpers import advance_to, make_lot, make_work_order, run_step


# ── 派工 ────────────────────────────────────────────────────
async def test_dispatch_lists_only_waiting_lots(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    waiting = await make_lot(db, qty=2, wo_no=wo_no)
    running = await make_lot(db, qty=2, wo_no=wo_no)
    await lot_service.track_in(db, {"lot_id": running["lot_id"], "eq_id": ""}, users["op001"])

    result = await dispatch_service.dispatch_list(db)
    assert [r["lot_id"] for r in result["items"]] == [waiting["lot_id"]]
    assert result["items"][0]["op_code"] == "WFR_RCV"
    assert result["items"][0]["ready"] is True  # 進料站不需設備


async def test_dispatch_filters_by_operation(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    first = await make_lot(db, qty=2, wo_no=wo_no)
    second = await make_lot(db, qty=2, wo_no=wo_no)
    await run_step(db, users, first["lot_id"])  # 推進到 WFR_SAW

    saw = await dispatch_service.dispatch_list(db, op_code="WFR_SAW")
    assert [r["lot_id"] for r in saw["items"]] == [first["lot_id"]]
    rcv = await dispatch_service.dispatch_list(db, op_code="WFR_RCV")
    assert [r["lot_id"] for r in rcv["items"]] == [second["lot_id"]]


async def test_qtime_urgency_jumps_the_queue(factory, users, clock):
    """Q-Time 快到的批號要插到急單前面 —— 逾時就得扣留，成本更高。"""
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)

    rush = await make_lot(db, qty=1, wo_no=wo_no)
    await db.execute("UPDATE lots SET priority = 1 WHERE lot_id = $1", rush["lot_id"])

    qtime_lot = await make_lot(db, qty=1, wo_no=wo_no)
    await advance_to(db, users, qtime_lot["lot_id"], "WIRE_BOND")  # 該站 Q-Time 60 分
    clock.advance(minutes=50)

    result = await dispatch_service.dispatch_list(db)
    order = [r["lot_id"] for r in result["items"]]
    assert order[0] == qtime_lot["lot_id"], "Q-Time 剩 10 分應排在急單之前"

    top = result["items"][0]
    assert top["urgency"] == "URGENT"
    assert "Q-Time 剩" in top["reason"]
    assert top["qtime_remaining_min"] == pytest.approx(10, abs=0.2)

    rush_row = next(r for r in result["items"] if r["lot_id"] == rush["lot_id"])
    assert rush_row["urgency"] == "HIGH"
    assert "急單" in rush_row["reason"]


async def test_expired_qtime_marked_critical(factory, users, clock):
    db = factory
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    clock.advance(minutes=90)

    result = await dispatch_service.dispatch_list(db)
    row = result["items"][0]
    assert row["qtime_expired"] is True
    assert row["urgency"] == "CRITICAL"
    assert "進站將自動扣留" in row["reason"]


async def test_blocked_when_no_equipment_available(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await run_step(db, users, lot["lot_id"])  # 到 WFR_SAW
    await equipment_service.set_state(db, "DS-01", EquipmentState.UNSCHEDULED_DOWN, "eng01", "EQ-ALARM")

    result = await dispatch_service.dispatch_list(db)
    row = next(r for r in result["items"] if r["lot_id"] == lot["lot_id"])
    assert row["ready"] is False
    assert row["urgency"] == "BLOCKED"
    assert row["available_equipments"] == []


async def test_equipment_busy_is_not_available(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    first = await make_lot(db, qty=1, wo_no=wo_no)
    second = await make_lot(db, qty=1, wo_no=wo_no)
    await run_step(db, users, first["lot_id"])
    await run_step(db, users, second["lot_id"])
    await lot_service.track_in(db, {"lot_id": first["lot_id"], "eq_id": "DS-01"}, users["op001"])

    result = await dispatch_service.dispatch_list(db, op_code="WFR_SAW")
    row = next(r for r in result["items"] if r["lot_id"] == second["lot_id"])
    assert row["ready"] is False


async def test_qtime_watch(factory, users, clock):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    safe = await make_lot(db, qty=1, wo_no=wo_no)
    late = await make_lot(db, qty=1, wo_no=wo_no)
    await advance_to(db, users, late["lot_id"], "WIRE_BOND")
    clock.advance(minutes=120)

    watch = await dispatch_service.qtime_watch(db)
    assert watch["expired_count"] == 1
    assert watch["at_risk_count"] == 0
    assert [r["lot_id"] for r in watch["items"]] == [late["lot_id"]]
    assert safe["lot_id"] not in [r["lot_id"] for r in watch["items"]]  # 該站無 Q-Time 管制


# ── 稽核 ────────────────────────────────────────────────────
def test_redact_hides_secrets():
    payload = {"username": "op001", "password": "s3cret", "nested": {"old_password": "x"}, "list": [{"password": "y"}]}
    cleaned = audit_service.redact(payload)
    assert cleaned["username"] == "op001"
    assert cleaned["password"] == audit_service.REDACTED
    assert cleaned["nested"]["old_password"] == audit_service.REDACTED
    assert cleaned["list"][0]["password"] == audit_service.REDACTED


async def test_master_data_changes_are_audited(factory):
    db = factory
    from app.services import master_service

    await master_service.customers.update(db, "MTK", {"name": "聯發科技"}, "eng01")
    rows = await audit_service.list_audit(db, kind="DATA", actor="eng01")
    assert rows[0]["action"] == "UPDATE"
    assert rows[0]["table_name"] == "customers"
    assert rows[0]["key"] == "MTK"
    assert "name" in rows[0]["fields"]


async def test_api_calls_are_audited(client, token):
    planner = await token("planner01")
    await client.post("/api/work-orders", headers=planner, json={
        "device_id": "TEST-QFN48", "plan_qty": 5, "unit_type": "WAFER",
        "due_date": "2030-01-01T00:00:00Z",
    })
    rows = (await client.get("/api/audit?kind=API", headers=await token("admin"))).json()
    entry = next(r for r in rows if r["path"] == "/api/work-orders")
    assert entry["method"] == "POST"
    assert entry["actor"] == "planner01"
    assert entry["status_code"] == 201
    assert entry["success"] is True
    assert "password" not in str(entry)


async def test_login_is_not_audited(client, token):
    """登入請求帶密碼，不進稽核軌跡。"""
    await token("planner01")
    rows = (await client.get("/api/audit", headers=await token("admin"))).json()
    assert all(r.get("path") != "/api/auth/login" for r in rows)


async def test_audit_requires_privilege(client, token):
    assert (await client.get("/api/audit", headers=await token("op001"))).status_code == 403
    assert (await client.get("/api/audit", headers=await token("qc01"))).status_code == 200


# ── 交接班 ──────────────────────────────────────────────────
def test_shift_window_round_trip():
    now = utcnow()
    label = shift_of(now)
    start, end = shift_window(label)
    assert start <= now < end
    assert (end - start).total_seconds() == 12 * 3600  # 早班 08:00 / 夜班 20:00


def test_shift_window_rejects_bad_label():
    with pytest.raises(ValueError, match="無法辨識"):
        shift_window("20260801-Z")
    with pytest.raises(ValueError, match="日期格式"):
        shift_window("2026AUG01-D")


async def test_shift_handover_report(factory, users, clock):
    db = factory
    # 把時鐘挪到本班開頭，否則測試若剛好在換班前一小時執行，
    # clock.advance(hours=1) 會跨到下一班，統計自然是空的
    shift_start, _ = shift_window(shift_of(clock.now))
    clock.now = shift_start + timedelta(minutes=1)

    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await run_step(db, users, lot["lot_id"], "DS-01", 20, "SAW-CHIP")
    await lot_service.hold_lot(
        db, {"lot_id": lot["lot_id"], "reason": "QUALITY", "remark": "抽檢"}, users["qc01"]
    )
    await equipment_service.set_state(db, "FT-01", EquipmentState.UNSCHEDULED_DOWN, "eng01", "EQ-ALARM")
    clock.advance(hours=1)
    await equipment_service.set_state(db, "FT-01", EquipmentState.STANDBY, "eng01", "EQ-FIXED")

    report = await report_service.shift_handover(db)
    assert report["shift"] == shift_of(clock.now)
    assert report["output"]["moves"] == 2
    assert report["output"]["qty_reject"] == 20
    assert report["output"]["yield"] == pytest.approx(1980 / 2000)
    assert [h["lot_id"] for h in report["new_holds"]] == [lot["lot_id"]]

    downtime = next(d for d in report["equipment_downtime"] if d["eq_id"] == "FT-01")
    assert downtime["minutes"] == pytest.approx(60, abs=1)
    assert downtime["reasons"] == ["EQ-ALARM"]

    assert "next_up" in report and "tools_to_change" in report


async def test_shift_handover_accepts_explicit_shift(factory, users):
    db = factory
    label = shift_of(utcnow())
    report = await report_service.shift_handover(db, label)
    assert report["shift"] == label
    assert report["output"]["moves"] == 0


async def test_dashboard_includes_new_signals(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await run_step(db, users, lot["lot_id"])
    data = await report_service.dashboard(db, hours=24)
    for key in ("qtime_at_risk", "spc_violations", "tools_to_change"):
        assert key in data["kpi"]
    for key in ("qtime_watch", "spc_violations", "tools_to_change"):
        assert key in data
