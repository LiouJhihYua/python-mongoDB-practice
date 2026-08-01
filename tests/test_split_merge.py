"""拆批、併批與族譜追溯。"""

import pytest

from app.errors import StateError, ValidationError
from app.models.enums import LotStatus
from app.services import lot_service, trace_service
from tests.helpers import advance_to, make_lot, make_work_order, run_step


async def test_split_creates_children_and_retires_parent(factory, users):
    db = factory
    lot = await make_lot(db, qty=10)
    parent_id = lot["lot_id"]

    result = await lot_service.split_lot(
        db, {"lot_id": parent_id, "quantities": [4, 6], "reason": "急單分流"}, users["planner01"]
    )
    children = result["children"]
    assert [c["lot_id"] for c in children] == [f"{parent_id}.1", f"{parent_id}.2"]
    assert [c["qty"] for c in children] == [4, 6]
    assert all(c["parent_lot_id"] == parent_id for c in children)
    assert all(c["status"] == LotStatus.WAITING.value for c in children)

    parent = await lot_service.get_lot(db, parent_id, raw=True)
    assert parent["status"] == LotStatus.SPLIT.value
    assert parent["qty"] == 0
    assert parent["child_lot_ids"] == [f"{parent_id}.1", f"{parent_id}.2"]

    # 晶圓依實際分配切給子批，追溯才不會失真
    assert len(children[0]["wafer_ids"]) == 4
    assert len(children[1]["wafer_ids"]) == 6
    assert set(children[0]["wafer_ids"]).isdisjoint(children[1]["wafer_ids"])


async def test_split_quantity_must_balance(factory, users):
    db = factory
    lot = await make_lot(db, qty=10)
    with pytest.raises(ValidationError, match="與母批現有量"):
        await lot_service.split_lot(
            db, {"lot_id": lot["lot_id"], "quantities": [4, 4], "reason": ""}, users["planner01"]
        )


async def test_split_requires_waiting_status(factory, users):
    db = factory
    lot = await make_lot(db, qty=10)
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": ""}, users["op001"])
    with pytest.raises(StateError, match="僅待進站"):
        await lot_service.split_lot(
            db, {"lot_id": lot["lot_id"], "quantities": [4, 6], "reason": ""}, users["planner01"]
        )


async def test_children_can_continue_production(factory, users):
    db = factory
    lot = await make_lot(db, qty=10)
    result = await lot_service.split_lot(
        db, {"lot_id": lot["lot_id"], "quantities": [4, 6], "reason": ""}, users["planner01"]
    )
    child_id = result["children"][0]["lot_id"]
    await run_step(db, users, child_id)
    outcome = await run_step(db, users, child_id, "DS-01")
    assert outcome["qty"] == 4 * 1000  # 4 片 → 4000 顆


async def test_merge_lots(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    first = await make_lot(db, qty=5, wo_no=wo_no)
    second = await make_lot(db, qty=7, wo_no=wo_no)

    merged = await lot_service.merge_lots(
        db, {"lot_ids": [first["lot_id"], second["lot_id"]], "carrier_id": "MAG9", "reason": "湊滿載盤"},
        users["planner01"],
    )
    assert merged["qty"] == 12
    assert merged["merged_from"] == sorted([first["lot_id"], second["lot_id"]])
    assert merged["status"] == LotStatus.WAITING.value
    assert len(merged["wafer_ids"]) == 12

    for lot_id in (first["lot_id"], second["lot_id"]):
        source = await lot_service.get_lot(db, lot_id, raw=True)
        assert source["status"] == LotStatus.MERGED.value
        assert source["merged_into"] == merged["lot_id"]
        assert source["qty"] == 0


async def test_merge_requires_same_step(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    first = await make_lot(db, qty=5, wo_no=wo_no)
    second = await make_lot(db, qty=5, wo_no=wo_no)
    await run_step(db, users, first["lot_id"])  # 只有其中一批前進了

    with pytest.raises(ValidationError, match="相同產品料號、流程、站序"):
        await lot_service.merge_lots(
            db, {"lot_ids": [first["lot_id"], second["lot_id"]], "carrier_id": "", "reason": ""},
            users["planner01"],
        )


async def test_merge_requires_waiting_lots(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    first = await make_lot(db, qty=5, wo_no=wo_no)
    second = await make_lot(db, qty=5, wo_no=wo_no)
    await lot_service.hold_lot(db, {"lot_id": first["lot_id"], "reason": "QUALITY", "remark": ""}, users["qc01"])
    with pytest.raises(StateError, match="非待進站狀態"):
        await lot_service.merge_lots(
            db, {"lot_ids": [first["lot_id"], second["lot_id"]], "carrier_id": "", "reason": ""},
            users["planner01"],
        )


async def test_genealogy_walks_both_directions(factory, users):
    db = factory
    wo_no = await make_work_order(db, plan_qty=50)
    lot = await make_lot(db, qty=10, wo_no=wo_no)
    split = await lot_service.split_lot(
        db, {"lot_id": lot["lot_id"], "quantities": [4, 6], "reason": ""}, users["planner01"]
    )
    child_a, child_b = [c["lot_id"] for c in split["children"]]
    merged = await lot_service.merge_lots(
        db, {"lot_ids": [child_a, child_b], "carrier_id": "", "reason": ""}, users["planner01"]
    )

    # 由最終批往上追，應能追回子批與母批
    tree = await trace_service.genealogy(db, merged["lot_id"])
    ancestors = {a["lot_id"] for a in tree["ancestors"]}
    assert {child_a, child_b, lot["lot_id"]} <= ancestors

    # 由母批往下追，應能追到最終批
    tree = await trace_service.genealogy(db, lot["lot_id"])
    descendants = {d["lot_id"] for d in tree["descendants"]}
    assert {child_a, child_b, merged["lot_id"]} <= descendants


async def test_backward_trace_includes_upstream_history(factory, users):
    db = factory
    lot = await make_lot(db, qty=4)
    await run_step(db, users, lot["lot_id"])  # 母批已跑過一站
    split = await lot_service.split_lot(
        db, {"lot_id": lot["lot_id"], "quantities": [2, 2], "reason": ""}, users["planner01"]
    )
    child_id = split["children"][0]["lot_id"]
    await run_step(db, users, child_id, "DS-01")

    trace = await trace_service.backward_trace(db, child_id)
    assert len(trace["source_wafers"]) == 2
    assert "DS-01" in trace["equipments_used"]
    assert "op001" in trace["operators_involved"]
    # 母批在 WFR_RCV 的加工紀錄也要能追到
    assert {"WFR_RCV", "WFR_SAW"} <= {h["op_code"] for h in trace["process_history"]}


async def test_forward_trace_from_wafer(factory, users):
    db = factory
    lot = await make_lot(db, qty=4)
    wafer_id = lot["wafer_ids"][0]
    split = await lot_service.split_lot(
        db, {"lot_id": lot["lot_id"], "quantities": [2, 2], "reason": ""}, users["planner01"]
    )
    trace = await trace_service.forward_trace(db, wafer_id)
    impacted = {l["lot_id"] for l in trace["impacted_lots"]}
    assert lot["lot_id"] in impacted
    assert split["children"][0]["lot_id"] in impacted


async def test_where_used_for_material_lot(factory, users):
    db = factory
    await db["materials"].insert_one(
        {"material_id": "WIRE-AU-08", "name": "金線", "material_type": "WIRE",
         "uom": "M", "on_hand_qty": 5000.0, "safety_stock": 0.0, "active": True}
    )
    lot = await make_lot(db, qty=1)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])
    current = await lot_service.get_lot(db, lot["lot_id"], raw=True)
    await lot_service.track_out(
        db,
        {"lot_id": lot["lot_id"], "good_qty": current["qty"], "reject_qty": 0, "defects": [],
         "materials": [{"material_id": "WIRE-AU-08", "material_lot": "AU-BAD-01", "qty": 50.0}],
         "bin_map": None, "remark": ""},
        users["op001"],
    )
    result = await trace_service.where_used(db, "AU-BAD-01")
    assert result["impacted_lot_count"] == 1
    assert result["impacted_lots"][0]["lot_id"] == lot["lot_id"]
