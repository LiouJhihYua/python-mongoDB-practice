"""e-SOP 電子作業指導書測試。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.errors import NotFoundError, PermissionError_, StateError, ValidationError
from app.models.base import utcnow
from app.services import lot_service, sop_service
from tests.helpers import advance_to, make_lot

pytestmark = pytest.mark.asyncio

STEPS = [
    {"seq": 1, "instruction": "確認機台已完成暖機", "detail": "", "image": "",
     "checkpoint": "溫度 150±5°C", "duration_sec": 60},
    {"seq": 2, "instruction": "裝載 Magazine", "detail": "", "image": "",
     "checkpoint": "確認方向標記朝上", "duration_sec": 30},
]


async def make_sop(db, sop_code="SOP-WB", op_code="WIRE_BOND", device_id="",
                   steps=None, require_ack=True, release=True, effective_from=None) -> dict:
    doc = await sop_service.create_sop(
        db,
        {
            "sop_code": sop_code, "title": f"{op_code} 作業指導書", "op_code": op_code,
            "device_id": device_id, "summary": "打線作業標準", "steps": STEPS if steps is None else steps,
            "hazards": "高溫、夾傷", "ppe": ["手套", "防靜電衣"], "attachments": [],
            "require_ack": require_ack,
        },
        "eng01",
    )
    if release:
        doc = await sop_service.release_sop(
            db, sop_code, doc["version"], {"effective_from": effective_from, "remark": ""}, "eng01"
        )
    return doc


# ── 版本控制 ────────────────────────────────────────────────
async def test_create_starts_as_draft(factory):
    doc = await make_sop(factory, release=False)
    assert doc["version"] == 1 and doc["status"] == "DRAFT"
    assert doc["effective_from"] is None


async def test_only_one_draft_at_a_time(factory):
    await make_sop(factory, release=False)
    with pytest.raises(StateError, match="已有草稿版本"):
        await make_sop(factory, release=False)


async def test_create_rejects_unknown_operation(factory):
    with pytest.raises(ValidationError, match="站別不存在"):
        await make_sop(factory, op_code="NO_SUCH_OP", release=False)


async def test_release_requires_steps(factory):
    doc = await make_sop(factory, steps=[], release=False)
    with pytest.raises(ValidationError, match="尚無任何步驟"):
        await sop_service.release_sop(factory, "SOP-WB", doc["version"], {}, "eng01")


async def test_release_obsoletes_previous_version(factory):
    db = factory
    await make_sop(db)
    revised = await sop_service.revise_sop(db, "SOP-WB", "eng01")
    assert revised["version"] == 2 and revised["status"] == "DRAFT"
    assert revised["steps"] == STEPS  # 內容自動複製

    released = await sop_service.release_sop(db, "SOP-WB", 2, {}, "eng01")
    assert released["obsoleted_versions"] == [1]
    v1 = await sop_service.get_sop(db, "SOP-WB", 1)
    assert v1["status"] == "OBSOLETE"


async def test_revise_rejects_when_draft_exists(factory):
    db = factory
    await make_sop(db)
    await sop_service.revise_sop(db, "SOP-WB", "eng01")
    with pytest.raises(StateError, match="已有草稿版本"):
        await sop_service.revise_sop(db, "SOP-WB", "eng01")


async def test_update_only_allowed_on_draft(factory):
    db = factory
    doc = await make_sop(db, release=False)
    updated = await sop_service.update_sop(
        db, "SOP-WB", doc["version"], {"title": "新標題"}, "eng01"
    )
    assert updated["title"] == "新標題"

    await sop_service.release_sop(db, "SOP-WB", doc["version"], {}, "eng01")
    with pytest.raises(StateError, match="僅草稿可修改"):
        await sop_service.update_sop(db, "SOP-WB", doc["version"], {"title": "再改"}, "eng01")


async def test_obsolete_sop(factory):
    db = factory
    await make_sop(db)
    result = await sop_service.obsolete_sop(db, "SOP-WB", 1, "eng01")
    assert result["status"] == "OBSOLETE"
    with pytest.raises(StateError, match="已作廢"):
        await sop_service.obsolete_sop(db, "SOP-WB", 1, "eng01")


async def test_get_unknown_sop(factory):
    with pytest.raises(NotFoundError):
        await sop_service.get_sop(factory, "NOPE")


# ── 適用版本 ────────────────────────────────────────────────
async def test_applicable_prefers_device_specific(factory):
    db = factory
    await make_sop(db, sop_code="SOP-WB-COMMON")
    await make_sop(db, sop_code="SOP-WB-QFN48", device_id="TEST-QFN48")

    specific = await sop_service.applicable_sop(db, "WIRE_BOND", "TEST-QFN48")
    assert specific["sop_code"] == "SOP-WB-QFN48"

    generic = await sop_service.applicable_sop(db, "WIRE_BOND", "OTHER-DEVICE")
    assert generic["sop_code"] == "SOP-WB-COMMON"


async def test_future_effective_date_not_applicable_yet(factory):
    db = factory
    await make_sop(db, effective_from=utcnow() + timedelta(days=1))
    assert await sop_service.applicable_sop(db, "WIRE_BOND", "TEST-QFN48") is None


async def test_station_sop_reports_ack_status(factory):
    db = factory
    await make_sop(db)
    before = await sop_service.station_sop(db, "WIRE_BOND", "TEST-QFN48", "op001")
    assert before["sop"]["sop_code"] == "SOP-WB" and before["acknowledged"] is False

    await sop_service.acknowledge(db, {"sop_code": "SOP-WB", "version": None}, "op001")
    after = await sop_service.station_sop(db, "WIRE_BOND", "TEST-QFN48", "op001")
    assert after["acknowledged"] is True and after["acknowledged_at"] is not None


async def test_station_without_sop(factory):
    result = await sop_service.station_sop(factory, "FT", "TEST-QFN48", "op001")
    assert result["sop"] is None and result["acknowledged"] is True


# ── 簽認 ────────────────────────────────────────────────────
async def test_acknowledge_is_idempotent(factory):
    db = factory
    await make_sop(db)
    first = await sop_service.acknowledge(db, {"sop_code": "SOP-WB", "version": None}, "op001")
    second = await sop_service.acknowledge(db, {"sop_code": "SOP-WB", "version": None}, "op001")
    assert first["already_acknowledged"] is False
    assert second["already_acknowledged"] is True
    assert second["acknowledged_at"] == first["acknowledged_at"]


async def test_acknowledge_rejects_draft(factory):
    db = factory
    await make_sop(db, release=False)
    with pytest.raises(StateError, match="僅生效版本可簽認"):
        await sop_service.acknowledge(db, {"sop_code": "SOP-WB", "version": None}, "op001")


async def test_pending_list_excludes_acknowledged(factory):
    db = factory
    await make_sop(db, sop_code="SOP-WB")
    await make_sop(db, sop_code="SOP-FT", op_code="FT")

    pending = await sop_service.pending_for_user(db, "op001")
    assert {p["sop_code"] for p in pending} == {"SOP-WB", "SOP-FT"}

    await sop_service.acknowledge(db, {"sop_code": "SOP-WB"}, "op001")
    pending = await sop_service.pending_for_user(db, "op001")
    assert {p["sop_code"] for p in pending} == {"SOP-FT"}


async def test_new_version_requires_new_acknowledgement(factory):
    db = factory
    await make_sop(db)
    await sop_service.acknowledge(db, {"sop_code": "SOP-WB"}, "op001")
    await sop_service.revise_sop(db, "SOP-WB", "eng01")
    await sop_service.release_sop(db, "SOP-WB", 2, {}, "eng01")

    pending = await sop_service.pending_for_user(db, "op001")
    assert [(p["sop_code"], p["version"]) for p in pending] == [("SOP-WB", 2)]


async def test_compliance_reports_rate(factory):
    db = factory
    await make_sop(db)
    await sop_service.acknowledge(db, {"sop_code": "SOP-WB"}, "op001")
    rows = await sop_service.compliance(db, "WIRE_BOND")
    assert len(rows) == 1
    assert rows[0]["acknowledged"] == 1
    assert rows[0]["target_headcount"] == 3  # op001 / op002 / eng01
    assert rows[0]["rate"] == pytest.approx(1 / 3, abs=1e-4)


async def test_acknowledgement_list(factory):
    db = factory
    await make_sop(db)
    await sop_service.acknowledge(db, {"sop_code": "SOP-WB"}, "op001")
    await sop_service.acknowledge(db, {"sop_code": "SOP-WB"}, "op002")
    rows = await sop_service.acknowledgements(db, "SOP-WB")
    assert {r["username"] for r in rows} == {"op001", "op002"}


# ── 與進站的整合 ────────────────────────────────────────────
async def test_track_in_blocked_until_sop_acknowledged(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_sop_ack = TRUE WHERE op_code = 'WIRE_BOND'")
    await make_sop(db)

    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")

    with pytest.raises(PermissionError_, match="尚未確認"):
        await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
        )

    await sop_service.acknowledge(db, {"sop_code": "SOP-WB"}, "op001")
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    assert result["status"] == "RUNNING"


async def test_track_in_blocked_when_station_has_no_sop(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_sop_ack = TRUE WHERE op_code = 'WIRE_BOND'")
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")

    with pytest.raises(StateError, match="尚未發行 SOP"):
        await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
        )


async def test_track_in_unaffected_when_flag_off(factory, users):
    db = factory
    await make_sop(db)
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    assert result["status"] == "RUNNING"


# ── API ─────────────────────────────────────────────────────
async def test_sop_api_flow(client, token, factory):
    eng = await token("eng01")
    res = await client.post(
        "/api/sops",
        json={
            "sop_code": "SOP-API", "title": "打線作業", "op_code": "WIRE_BOND",
            "summary": "測試", "steps": STEPS, "ppe": ["手套"],
        },
        headers=eng,
    )
    assert res.status_code == 201, res.text
    version = res.json()["version"]

    release = await client.post(f"/api/sops/SOP-API/{version}/release", json={}, headers=eng)
    assert release.status_code == 200 and release.json()["status"] == "RELEASED"

    op = await token("op001")
    station = await client.get("/api/sops/station/WIRE_BOND?device_id=TEST-QFN48", headers=op)
    assert station.json()["acknowledged"] is False

    ack = await client.post("/api/sops/acknowledge", json={"sop_code": "SOP-API"}, headers=op)
    assert ack.json()["already_acknowledged"] is False

    pending = await client.get("/api/sops/pending", headers=op)
    assert pending.json() == []

    compliance = await client.get("/api/sops/compliance", headers=eng)
    assert compliance.json()[0]["acknowledged"] == 1


async def test_sop_api_requires_engineer_to_edit(client, token, factory):
    op = await token("op001")
    res = await client.post(
        "/api/sops",
        json={"sop_code": "X", "title": "X", "op_code": "WIRE_BOND", "steps": STEPS},
        headers=op,
    )
    assert res.status_code == 403
