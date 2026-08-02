"""客訴（RMA）與載具管理測試。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import utcnow
from app.services import carrier_service, complaint_service, lot_service, wafermap_service
from tests.helpers import advance_to, make_lot, run_step
from tests.test_wafermap import circular_grid

pytestmark = pytest.mark.asyncio


# ── 載具 ────────────────────────────────────────────────────
async def make_carrier(db, carrier_id="MAG-001", capacity=0, clean_interval=0) -> dict:
    return await carrier_service.create_carrier(
        db,
        {"carrier_id": carrier_id, "carrier_type": "MAGAZINE", "capacity": capacity,
         "location": "ASSY-1", "clean_interval": clean_interval, "remark": "", "active": True},
        "qc01",
    )


async def test_assign_and_release(factory):
    db = factory
    await make_carrier(db)
    lot = await make_lot(db, qty=2)

    assigned = await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")
    assert assigned["status"] == "IN_USE" and assigned["current_lot_id"] == lot["lot_id"]
    refreshed = await lot_service.get_lot(db, lot["lot_id"])
    assert refreshed["carrier_id"] == "MAG-001"

    released = await carrier_service.release(db, "MAG-001", "op001")
    assert released["status"] == "EMPTY" and released["released_lot_id"] == lot["lot_id"]


async def test_cannot_assign_carrier_twice(factory):
    db = factory
    await make_carrier(db)
    first = await make_lot(db, qty=2)
    second = await make_lot(db, qty=2)

    await carrier_service.assign(db, "MAG-001", first["lot_id"], "op001")
    with pytest.raises(StateError, match="目前掛著批號"):
        await carrier_service.assign(db, "MAG-001", second["lot_id"], "op001")


async def test_assign_same_lot_is_idempotent(factory):
    db = factory
    await make_carrier(db)
    lot = await make_lot(db, qty=2)
    await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")
    again = await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")
    assert again["current_lot_id"] == lot["lot_id"]
    assert int(again["use_count"]) == 1  # 不會重複累計使用次數


async def test_capacity_is_enforced(factory):
    db = factory
    await make_carrier(db, capacity=1)
    lot = await make_lot(db, qty=3)
    with pytest.raises(ValidationError, match="超過載具"):
        await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")


async def test_release_marks_dirty_at_clean_interval(factory):
    db = factory
    await make_carrier(db, clean_interval=1)
    lot = await make_lot(db, qty=2)
    await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")

    released = await carrier_service.release(db, "MAG-001", "op001")
    assert released["status"] == "DIRTY" and released["needs_cleaning"] is True

    # 待清洗的載具不可再指派
    other = await make_lot(db, qty=2)
    with pytest.raises(StateError, match="清洗"):
        await carrier_service.assign(db, "MAG-001", other["lot_id"], "op001")

    cleaned = await carrier_service.clean(db, "MAG-001", "op001")
    assert cleaned["status"] == "EMPTY" and cleaned["use_count"] == 0


async def test_clean_blocked_while_in_use(factory):
    db = factory
    await make_carrier(db)
    lot = await make_lot(db, qty=2)
    await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")
    with pytest.raises(StateError, match="仍掛著批號"):
        await carrier_service.clean(db, "MAG-001", "op001")


async def test_scrap_deactivates_carrier(factory):
    db = factory
    await make_carrier(db)
    row = await carrier_service.set_status(db, "MAG-001", "SCRAPPED", "qc01", "變形")
    assert row["status"] == "SCRAPPED" and row["active"] is False


async def test_set_status_rejects_unknown(factory):
    db = factory
    await make_carrier(db)
    with pytest.raises(ValidationError, match="不支援的載具狀態"):
        await carrier_service.set_status(db, "MAG-001", "FLYING", "qc01")


async def test_release_without_lot(factory):
    db = factory
    await make_carrier(db)
    with pytest.raises(StateError, match="沒有掛任何批號"):
        await carrier_service.release(db, "MAG-001", "op001")


async def test_open_lot_assigns_known_carrier(factory):
    db = factory
    await make_carrier(db)
    lot = await make_lot(db, qty=2)  # helpers 用的 carrier_id 是 MAG001，不會對到
    assert lot["carrier_id"] == "MAG001"

    carrier = await carrier_service.get_carrier(db, "MAG-001")
    assert carrier["current_lot_id"] is None  # 未建檔的編號不影響開批


async def test_carrier_released_when_lot_completes(factory, users):
    db = factory
    await make_carrier(db)
    lot = await make_lot(db, qty=2)
    await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")

    await advance_to(db, users, lot["lot_id"], "FT")
    await run_step(db, users, lot["lot_id"], "FT-01")

    done = await lot_service.get_lot(db, lot["lot_id"])
    assert done["status"] == "COMPLETED"
    carrier = await carrier_service.get_carrier(db, "MAG-001")
    assert carrier["current_lot_id"] is None and carrier["status"] == "EMPTY"


async def test_carrier_history_and_overview(factory):
    db = factory
    await make_carrier(db)
    await make_carrier(db, "MAG-002")
    lot = await make_lot(db, qty=2)
    await carrier_service.assign(db, "MAG-001", lot["lot_id"], "op001")

    history = await carrier_service.carrier_history(db, "MAG-001")
    assert [h["action"] for h in history] == ["ASSIGN"]

    overview = await carrier_service.overview(db)
    assert overview["total"] == 2 and overview["in_use"] == 1 and overview["available"] == 1


async def test_carrier_not_found(factory):
    with pytest.raises(NotFoundError):
        await carrier_service.get_carrier(factory, "NOPE")


# ── 客訴 ────────────────────────────────────────────────────
async def _shipped_lot(db, users) -> dict:
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "FT")
    await run_step(db, users, lot["lot_id"], "FT-01")
    await lot_service.ship_lots(
        db,
        {"customer_code": "MTK", "lot_ids": [lot["lot_id"]], "customer_po": "PO-1", "remark": ""},
        users["planner01"],
    )
    return await lot_service.get_lot(db, lot["lot_id"])


async def make_complaint(db, lot_ids, unit_seqs=None, severity="MAJOR") -> dict:
    return await complaint_service.create_complaint(
        db,
        {
            "customer_code": "MTK", "customer_ref": "CUST-8899", "device_id": "",
            "lot_ids": lot_ids, "unit_seqs": unit_seqs or [], "qty": 3,
            "severity": severity, "category": "電性", "description": "客戶端測試失效",
            "owner": "qc01", "due_date": utcnow() + timedelta(days=14), "received_at": None,
        },
        "qc01",
    )


async def test_create_complaint_runs_impact_analysis(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    complaint = await make_complaint(db, [lot["lot_id"]])

    assert complaint["complaint_no"].startswith("CM")
    assert complaint["status"] == "OPEN"
    assert complaint["device_id"] == "TEST-QFN48"

    impact = complaint["impact"]
    assert lot["lot_id"] in impact["reported_lots"]
    assert impact["impacted_lot_count"] >= 1
    assert "MTK" in impact["affected_customers"]
    assert len(impact["source_wafers"]) == 2


async def test_complaint_rejects_unknown_lot(factory):
    with pytest.raises(ValidationError, match="批號不存在"):
        await make_complaint(factory, ["L999999"])


async def test_complaint_rejects_unknown_customer(factory):
    with pytest.raises(ValidationError, match="客戶不存在"):
        await complaint_service.create_complaint(
            factory,
            {"customer_code": "NOPE", "lot_ids": [], "description": "x", "severity": "MINOR"},
            "qc01",
        )


async def test_impact_includes_die_coordinates(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    for wafer_id in lot["wafer_ids"]:
        await wafermap_service.upload_map(
            db,
            {"wafer_id": wafer_id, "source": "CP", "grid": circular_grid(12),
             "pass_bins": [1], "null_bin": -1, "update_wafer": True},
            "eng01",
        )
    await wafermap_service.assign_dies(
        db, {"lot_id": lot["lot_id"], "qty": 10, "wafer_ids": []}, "op001"
    )

    complaint = await make_complaint(db, [lot["lot_id"]], unit_seqs=[3, 7])
    dies = complaint["impact"]["returned_dies"]
    assert len(dies) == 2
    assert {d["unit_seq"] for d in dies} == {3, 7}
    assert all("die_x" in d and "die_y" in d for d in dies)


async def test_8d_flow_and_close(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    complaint = await make_complaint(db, [lot["lot_id"]])
    no = complaint["complaint_no"]

    updated = await complaint_service.update_d8(
        db, no, "D2", {"content": "客戶端 FT 開路，比例 0.3%", "completed": True}, "qc01"
    )
    assert updated["status"] == "INVESTIGATING"  # 一開始填 8D 就自動進入調查中

    for step, content in (
        ("D3", "圈選同晶圓批全數暫停出貨"),
        ("D4", "打線第二銲點附著力不足"),
        ("D5", "調整超音波功率並更新配方"),
    ):
        await complaint_service.update_d8(db, no, step, {"content": content}, "eng01")

    await complaint_service.change_status(db, no, "ACTION", {"remark": "對策執行中"}, "qc01")

    # D6（對策驗證）還沒做完就不准結案
    with pytest.raises(StateError, match="尚未完成"):
        await complaint_service.change_status(db, no, "CLOSED", {}, "qc01")
    await complaint_service.update_d8(
        db, no, "D6", {"content": "連續 5 批拉力量測皆在管制內"}, "eng01"
    )
    closed = await complaint_service.change_status(db, no, "CLOSED", {"remark": "結案"}, "qc01")
    assert closed["status"] == "CLOSED" and closed["closed_at"] is not None
    assert closed["root_cause"] == "打線第二銲點附著力不足"
    assert closed["corrective_action"] == "調整超音波功率並更新配方"


async def test_closed_complaint_is_immutable(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    no = (await make_complaint(db, [lot["lot_id"]]))["complaint_no"]
    for step in ("D2", "D3", "D4", "D5", "D6"):
        await complaint_service.update_d8(db, no, step, {"content": "x"}, "qc01")
    await complaint_service.change_status(db, no, "ACTION", {}, "qc01")
    await complaint_service.change_status(db, no, "CLOSED", {}, "qc01")

    with pytest.raises(StateError, match="不可再修改"):
        await complaint_service.update_d8(db, no, "D7", {"content": "y"}, "qc01")


async def test_invalid_status_transition(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    no = (await make_complaint(db, [lot["lot_id"]]))["complaint_no"]
    with pytest.raises(StateError, match="不可由"):
        await complaint_service.change_status(db, no, "CLOSED", {}, "qc01")


async def test_unknown_d8_step(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    no = (await make_complaint(db, [lot["lot_id"]]))["complaint_no"]
    with pytest.raises(ValidationError, match="不支援的 8D 步驟"):
        await complaint_service.update_d8(db, no, "D9", {"content": "x"}, "qc01")


async def test_events_record_the_timeline(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    no = (await make_complaint(db, [lot["lot_id"]]))["complaint_no"]
    await complaint_service.add_note(db, no, "已電話聯繫客戶窗口", "qc01")
    await complaint_service.update_d8(db, no, "D1", {"content": "成立跨部門小組"}, "qc01")

    detail = await complaint_service.get_complaint(db, no)
    actions = [e["action"] for e in detail["events"]]
    assert actions == ["CREATE", "NOTE", "UPDATE"]


async def test_summary_counts_and_overdue(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    complaint = await make_complaint(db, [lot["lot_id"]])
    await db.execute(
        "UPDATE complaints SET due_date = $1 WHERE complaint_no = $2",
        utcnow() - timedelta(days=1), complaint["complaint_no"],
    )
    summary = await complaint_service.summary(db, utcnow() - timedelta(days=1), utcnow() + timedelta(days=1))
    assert summary["total"] == 1 and summary["open"] == 1
    assert summary["by_severity"]["MAJOR"] == 1
    assert [o["complaint_no"] for o in summary["overdue"]] == [complaint["complaint_no"]]


async def test_refresh_impact(factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    complaint = await make_complaint(db, [lot["lot_id"]])
    refreshed = await complaint_service.refresh_impact(db, complaint["complaint_no"], "qc01")
    assert refreshed["impact"]["impacted_lot_count"] >= 1


# ── API ─────────────────────────────────────────────────────
async def test_quality_ops_api_flow(client, token, factory, users):
    db = factory
    lot = await _shipped_lot(db, users)
    qc = await token("qc01")

    preview = await client.post(
        "/api/quality-ops/complaints/impact-preview",
        json={"customer_code": "MTK", "lot_ids": [lot["lot_id"]], "description": "試算"},
        headers=qc,
    )
    assert preview.status_code == 200 and preview.json()["impacted_lot_count"] >= 1

    created = await client.post(
        "/api/quality-ops/complaints",
        json={"customer_code": "MTK", "lot_ids": [lot["lot_id"]], "qty": 2,
              "severity": "CRITICAL", "description": "客戶端失效"},
        headers=qc,
    )
    assert created.status_code == 201, created.text
    no = created.json()["complaint_no"]

    d8 = await client.put(
        f"/api/quality-ops/complaints/{no}/d8/D2",
        json={"content": "描述問題"}, headers=qc,
    )
    assert d8.json()["status"] == "INVESTIGATING"

    detail = await client.get(f"/api/quality-ops/complaints/{no}", headers=qc)
    assert detail.json()["d8"]["D2"]["content"] == "描述問題"

    carriers = await client.post(
        "/api/quality-ops/carriers",
        json={"carrier_id": "API-MAG", "carrier_type": "MAGAZINE", "capacity": 25},
        headers=qc,
    )
    assert carriers.status_code == 201

    overview = await client.get("/api/quality-ops/carriers/overview", headers=qc)
    assert overview.json()["total"] == 1


async def test_complaint_api_requires_qc(client, token, factory):
    op = await token("op001")
    res = await client.post(
        "/api/quality-ops/complaints",
        json={"customer_code": "MTK", "lot_ids": [], "description": "x"},
        headers=op,
    )
    assert res.status_code == 403
