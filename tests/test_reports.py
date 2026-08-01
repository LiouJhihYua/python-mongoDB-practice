"""報表：WIP、良率、不良柏拉圖、Bin 分佈、週期時間與看板彙總。"""

from datetime import timedelta

import pytest

from app.models.base import utcnow
from app.services import lot_service, quality_service, report_service
from tests.helpers import EQ_FOR_OP, advance_to, make_lot, make_work_order, run_step


async def _window():
    now = utcnow()
    return now - timedelta(hours=24), now + timedelta(minutes=1)


async def test_wip_by_operation(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    stay = await make_lot(db, qty=3, wo_no=wo_no)          # 停在第一站
    moved = await make_lot(db, qty=4, wo_no=wo_no)
    await run_step(db, users, moved["lot_id"])             # 前進到 WFR_SAW

    report = await report_service.wip_by_operation(db)
    by_op = {row["op_code"]: row for row in report["items"]}
    assert by_op["WFR_RCV"]["lots"] == 1
    assert by_op["WFR_RCV"]["qty"] == 3
    assert by_op["WFR_SAW"]["lots"] == 1
    assert by_op["WFR_SAW"]["op_name"] == "晶圓切割"
    assert report["total_lots"] == 2
    assert stay["lot_id"] and moved["lot_id"]


async def test_wip_counts_running_and_hold_separately(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    running = await make_lot(db, qty=2, wo_no=wo_no)
    held = await make_lot(db, qty=2, wo_no=wo_no)
    await lot_service.track_in(db, {"lot_id": running["lot_id"], "eq_id": ""}, users["op001"])
    await lot_service.hold_lot(db, {"lot_id": held["lot_id"], "reason": "QUALITY", "remark": ""}, users["qc01"])

    report = await report_service.wip_by_operation(db)
    row = next(r for r in report["items"] if r["op_code"] == "WFR_RCV")
    assert row["running"] == 1 and row["hold"] == 1 and row["waiting"] == 0


async def test_yield_by_operation_cumulative(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    lot_id = lot["lot_id"]
    await run_step(db, users, lot_id)                                # 2 片 → 2 片
    await run_step(db, users, lot_id, "DS-01", 100, "SAW-CHIP")      # 2000 → 1900
    await run_step(db, users, lot_id, "WB-01", 190, "WB-NSOP")       # 1900 → 1710

    start, end = await _window()
    report = await report_service.yield_by_operation(db, start, end)
    by_op = {row["op_code"]: row for row in report["items"]}
    assert by_op["WFR_SAW"]["step_yield"] == pytest.approx(0.95)
    assert by_op["WIRE_BOND"]["step_yield"] == pytest.approx(0.9)
    assert by_op["WIRE_BOND"]["cumulative_yield"] == pytest.approx(0.95 * 0.9)
    assert report["final_yield"] == pytest.approx(0.855, abs=1e-6)


async def test_cumulative_yield_suppressed_when_routes_mixed(factory, users):
    """兩條流程站別代碼會重疊，硬串成一條累計良率沒有物理意義。"""
    db = factory
    from app.services import master_service

    # 另一條流程（少一站）與對應料號
    await master_service.create_route(
        db,
        {"route_code": "RT-ALT", "version": 1, "description": "替代流程", "package_family": "QFN",
         "steps": [{"seq": 10, "op_code": "WFR_RCV", "standard_yield": 1.0, "note": ""},
                   {"seq": 20, "op_code": "WFR_SAW", "standard_yield": 1.0, "note": ""}],
         "active": True},
        "test",
    )
    await master_service.create_device(
        db,
        {"device_id": "ALT-QFN48", "description": "", "customer_code": "MTK", "customer_device": "",
         "package_code": "QFN48", "route_code": "RT-ALT", "wafer_size_inch": 12,
         "gross_die_per_wafer": 1000, "units_per_strip": 50, "units_per_reel": 4000,
         "die_size_mm": "", "wire_per_unit": 0, "target_yield": 0.98, "active": True},
        "test",
    )

    main_lot = await make_lot(db, qty=2)
    await run_step(db, users, main_lot["lot_id"])
    await run_step(db, users, main_lot["lot_id"], "DS-01", 100, "SAW-CHIP")

    alt_wo = await make_work_order(db, plan_qty=10, device_id="ALT-QFN48")
    alt_lot = await make_lot(db, qty=2, wo_no=alt_wo, with_wafers=False)
    await run_step(db, users, alt_lot["lot_id"])
    await run_step(db, users, alt_lot["lot_id"], "DS-01", 200, "SAW-CHIP")

    start, end = await _window()
    mixed = await report_service.yield_by_operation(db, start, end)
    assert sorted(mixed["routes"]) == ["RT-ALT", "RT-TEST"]
    assert mixed["final_yield"] is None
    assert all(row["cumulative_yield"] is None for row in mixed["items"])
    assert all(row["step_yield"] > 0 for row in mixed["items"])  # 單站良率仍有效

    # 限定單一流程時可以正常串接
    single = await report_service.yield_by_operation(db, start, end, route_code="RT-TEST")
    assert single["final_yield"] == pytest.approx(0.95)
    assert single["items"][-1]["cumulative_yield"] == pytest.approx(0.95)

    # 混流時改看各流程各自的累計良率
    by_route = await report_service.final_yield_by_route(db, start, end)
    per_route = {row["route_code"]: row for row in by_route}
    assert per_route["RT-TEST"]["final_yield"] == pytest.approx(0.95)
    assert per_route["RT-ALT"]["final_yield"] == pytest.approx(0.90)
    assert by_route[0]["route_code"] == "RT-ALT"  # 由差到好排序


async def test_defect_pareto(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    for reject in (300, 100):
        lot = await make_lot(db, qty=2, wo_no=wo_no)
        await run_step(db, users, lot["lot_id"])
        await run_step(db, users, lot["lot_id"], "DS-01", reject, "SAW-CHIP")

    start, end = await _window()
    pareto = await quality_service.defect_pareto(db, start, end)
    assert pareto["total_defect_qty"] == 400
    top = pareto["items"][0]
    assert top["defect_code"] == "SAW-CHIP"
    assert top["qty"] == 400
    assert top["occurrences"] == 2
    assert top["cumulative_ratio"] == pytest.approx(1.0)


async def test_bin_summary(factory, users):
    db = factory
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "FT")
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "FT-01"}, users["op001"])
    current = await lot_service.get_lot(db, lot["lot_id"], raw=True)
    fails = 40
    await lot_service.track_out(
        db,
        {"lot_id": lot["lot_id"], "good_qty": None, "reject_qty": 0,
         "defects": [{"defect_code": "FT-OPEN", "qty": fails}],
         "bin_map": {"1": current["qty"] - fails, "2": fails}, "materials": [], "remark": ""},
        users["op001"],
    )
    start, end = await _window()
    summary = await quality_service.bin_summary(db, start, end)
    bins = {row["bin"]: row for row in summary["bins"]}
    assert summary["total_units"] == current["qty"]
    assert bins["2"]["qty"] == fails
    assert bins["1"]["ratio"] == pytest.approx((current["qty"] - fails) / current["qty"], abs=1e-4)


async def test_throughput_by_shift(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    await run_step(db, users, lot["lot_id"])
    await run_step(db, users, lot["lot_id"], "DS-01", 20, "SAW-CHIP")

    start, end = await _window()
    rows = await report_service.throughput(db, start, end, group_by="operation")
    by_op = {row["key"]: row for row in rows}
    assert by_op["WFR_SAW"]["qty_good"] == 1980
    assert by_op["WFR_SAW"]["qty_reject"] == 20
    assert by_op["WFR_SAW"]["yield"] == pytest.approx(0.99)


async def test_cycle_time_and_queue(factory, users, clock):
    db = factory
    lot = await make_lot(db, qty=1)
    lot_id = lot["lot_id"]
    await run_step(db, users, lot_id)  # WFR_RCV，瞬間完成

    clock.advance(minutes=25)  # 在切割站前排隊 25 分鐘
    await lot_service.track_in(db, {"lot_id": lot_id, "eq_id": "DS-01"}, users["op001"])
    clock.advance(minutes=40)  # 加工 40 分鐘
    current = await lot_service.get_lot(db, lot_id, raw=True)
    device = await db["devices"].find_one({"device_id": current["device_id"]})
    operation = await db["operations"].find_one({"op_code": "WFR_SAW"})
    expected, _ = lot_service.compute_expected_output(
        int(current["qty"]), operation, device, current["unit_type"]
    )
    await lot_service.track_out(
        db, {"lot_id": lot_id, "good_qty": expected, "reject_qty": 0, "defects": [],
             "materials": [], "bin_map": None, "remark": ""}, users["op001"]
    )

    start, end = clock.now - timedelta(hours=2), clock.now + timedelta(minutes=1)
    rows = await report_service.cycle_time_by_operation(db, start, end)
    saw = next(r for r in rows if r["op_code"] == "WFR_SAW")
    assert saw["avg_process_sec"] == pytest.approx(2400, abs=2)   # 40 分鐘
    assert saw["avg_queue_sec"] == pytest.approx(1500, abs=2)     # 25 分鐘
    assert saw["standard_cycle_time_sec"] == 1800
    assert saw["vs_standard"] == pytest.approx(2400 / 1800, abs=0.01)


async def test_wip_aging_buckets(factory, users, clock):
    db = factory
    await make_lot(db, qty=2)
    clock.advance(hours=100)
    aging = await report_service.wip_aging(db)
    assert aging["total_wip_lots"] == 1
    assert aging["distribution"]["72-168h"] == 1
    assert aging["aged_lots"] == []  # 尚未超過最大級距


async def test_qtime_violation_report(factory, users, clock):
    db = factory
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    clock.advance(hours=5)
    with pytest.raises(Exception):
        await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])

    start, end = clock.now - timedelta(hours=24), clock.now + timedelta(minutes=1)
    rows = await report_service.qtime_violations(db, start, end)
    assert len(rows) == 1
    assert rows[0]["lot_id"] == lot["lot_id"]
    assert rows[0]["qtime_limit_min"] == 60


async def test_dashboard_bundles_everything(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    finished = await make_lot(db, qty=1, wo_no=wo_no)
    for op in ["WFR_RCV", "WFR_SAW", "WIRE_BOND", "FT"]:
        await run_step(db, users, finished["lot_id"], EQ_FOR_OP[op])
    in_line = await make_lot(db, qty=2, wo_no=wo_no)
    await lot_service.hold_lot(db, {"lot_id": in_line["lot_id"], "reason": "QUALITY", "remark": ""}, users["qc01"])

    data = await report_service.dashboard(db, hours=24)
    kpi = data["kpi"]
    assert kpi["wip_lots"] == 1
    assert kpi["lots_on_hold"] == 1
    assert kpi["moves"] == 4
    assert kpi["open_work_orders"] == 1
    assert 0 < kpi["overall_yield"] <= 1
    assert {"wip_by_operation", "yield_by_operation", "equipment_states",
            "defect_pareto", "hold_summary", "wip_aging"} <= set(data)
    assert data["hold_summary"][0]["reason"] == "QUALITY"


async def test_work_order_progress(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=20)
    lot = await make_lot(db, qty=8, wo_no=wo_no)
    await run_step(db, users, lot["lot_id"])

    from app.services import workorder_service

    result = await workorder_service.work_order_progress(db, wo_no)
    assert result["plan_qty"] == 20
    assert result["released_qty"] == 8
    assert result["wip_qty"] == 8
    assert result["release_rate"] == pytest.approx(0.4)
