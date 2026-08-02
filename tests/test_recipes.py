"""配方管理與進站驗證測試。"""

from __future__ import annotations

import pytest

from app.errors import NotFoundError, StateError, ValidationError
from app.services import lot_service, recipe_service
from tests.helpers import advance_to, make_lot

pytestmark = pytest.mark.asyncio

PARAMS = {"溫度": 175, "時間": 30, "力道": 25}


async def make_recipe(db, ppid="WB-QFN48-A", op_code="WIRE_BOND", device_id="TEST-QFN48",
                      eq_model="", parameters=None, checksum="", release=True) -> dict:
    doc = await recipe_service.create_recipe(
        db,
        {
            "ppid": ppid, "name": f"{op_code} 配方", "op_code": op_code,
            "device_id": device_id, "eq_model": eq_model,
            "parameters": PARAMS if parameters is None else parameters,
            "checksum": checksum, "remark": "",
        },
        "eng01",
    )
    if release:
        doc = await recipe_service.release_recipe(db, ppid, doc["version"], {}, "eng01")
    return doc


# ── 版本管理 ────────────────────────────────────────────────
async def test_create_starts_as_draft(factory):
    doc = await make_recipe(factory, release=False)
    assert doc["version"] == 1 and doc["status"] == "DRAFT"


async def test_only_one_draft_at_a_time(factory):
    await make_recipe(factory, release=False)
    with pytest.raises(StateError, match="已有草稿版本"):
        await make_recipe(factory, release=False)


async def test_create_rejects_unknown_operation(factory):
    with pytest.raises(ValidationError, match="站別不存在"):
        await make_recipe(factory, op_code="NO_SUCH_OP", release=False)


async def test_release_requires_parameters(factory):
    doc = await make_recipe(factory, parameters={}, release=False)
    with pytest.raises(ValidationError, match="尚無任何參數"):
        await recipe_service.release_recipe(factory, "WB-QFN48-A", doc["version"], {}, "eng01")


async def test_release_obsoletes_previous_version(factory):
    db = factory
    await make_recipe(db)
    revised = await recipe_service.revise_recipe(db, "WB-QFN48-A", "eng01")
    assert revised["version"] == 2 and revised["parameters"] == PARAMS

    released = await recipe_service.release_recipe(db, "WB-QFN48-A", 2, {}, "eng01")
    assert released["obsoleted_versions"] == [1]
    assert (await recipe_service.get_recipe(db, "WB-QFN48-A", 1))["status"] == "OBSOLETE"


async def test_update_only_on_draft(factory):
    db = factory
    doc = await make_recipe(db, release=False)
    updated = await recipe_service.update_recipe(
        db, "WB-QFN48-A", doc["version"], {"parameters": {"溫度": 180}}, "eng01"
    )
    assert updated["parameters"] == {"溫度": 180}

    await recipe_service.release_recipe(db, "WB-QFN48-A", doc["version"], {}, "eng01")
    with pytest.raises(StateError, match="僅草稿可修改"):
        await recipe_service.update_recipe(db, "WB-QFN48-A", doc["version"], {"name": "X"}, "eng01")


async def test_obsolete_recipe(factory):
    db = factory
    await make_recipe(db)
    assert (await recipe_service.obsolete_recipe(db, "WB-QFN48-A", 1, "eng01"))["status"] == "OBSOLETE"
    with pytest.raises(StateError, match="已作廢"):
        await recipe_service.obsolete_recipe(db, "WB-QFN48-A", 1, "eng01")


async def test_get_unknown_recipe(factory):
    with pytest.raises(NotFoundError):
        await recipe_service.get_recipe(factory, "NOPE")


# ── 核可清單 ────────────────────────────────────────────────
async def test_approved_prefers_device_specific(factory):
    db = factory
    await make_recipe(db, ppid="WB-COMMON", device_id="")
    await make_recipe(db, ppid="WB-QFN48", device_id="TEST-QFN48")

    approved = await recipe_service.approved_recipes(db, "WIRE_BOND", "TEST-QFN48")
    assert approved[0]["ppid"] == "WB-QFN48"
    assert {r["ppid"] for r in approved} == {"WB-QFN48", "WB-COMMON"}

    other = await recipe_service.approved_recipes(db, "WIRE_BOND", "OTHER")
    assert {r["ppid"] for r in other} == {"WB-COMMON"}


async def test_draft_recipe_is_not_approved(factory):
    db = factory
    await make_recipe(db, release=False)
    assert await recipe_service.approved_recipes(db, "WIRE_BOND", "TEST-QFN48") == []


# ── 機台配方 ────────────────────────────────────────────────
async def test_set_and_get_loaded(factory):
    db = factory
    row = await recipe_service.set_loaded(
        db, "WB-01", {"ppid": "WB-QFN48-A", "version": 1, "checksum": "abc"}, "op001"
    )
    assert row["ppid"] == "WB-QFN48-A" and row["source"] == "MANUAL"

    again = await recipe_service.set_loaded(db, "WB-01", {"ppid": "OTHER"}, "op001")
    assert again["ppid"] == "OTHER"
    count = await db.fetchval("SELECT count(*) FROM equipment_recipes WHERE eq_id = 'WB-01'")
    assert count == 1


async def test_set_loaded_rejects_unknown_equipment(factory):
    with pytest.raises(ValidationError, match="設備不存在"):
        await recipe_service.set_loaded(factory, "NO-EQ", {"ppid": "X"}, "op001")


# ── 進站比對 ────────────────────────────────────────────────
async def _lot_at_wire_bond(db, users) -> dict:
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    return await lot_service.get_lot(db, lot["lot_id"])


async def test_track_in_blocked_when_recipe_mismatched(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    await make_recipe(db, ppid="WB-QFN48-A")
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WRONG-RECIPE"}, "op001")

    lot = await _lot_at_wire_bond(db, users)
    with pytest.raises(StateError, match="未核可"):
        await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
        )

    check = await db.fetchrow(
        "SELECT * FROM recipe_checks WHERE lot_id = $1 ORDER BY id DESC", lot["lot_id"]
    )
    assert check["passed"] is False and check["loaded_ppid"] == "WRONG-RECIPE"


async def test_track_in_passes_with_approved_recipe(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    await make_recipe(db, ppid="WB-QFN48-A")
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WB-QFN48-A"}, "op001")

    lot = await _lot_at_wire_bond(db, users)
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    assert result["status"] == "RUNNING"

    check = await db.fetchrow(
        "SELECT * FROM recipe_checks WHERE lot_id = $1 ORDER BY id DESC", lot["lot_id"]
    )
    assert check["passed"] is True


async def test_track_in_blocked_when_no_recipe_released(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    lot = await _lot_at_wire_bond(db, users)
    with pytest.raises(StateError, match="尚無已發行的核可配方"):
        await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
        )


async def test_track_in_blocked_when_equipment_never_reported(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    await make_recipe(db, ppid="WB-QFN48-A")
    lot = await _lot_at_wire_bond(db, users)
    with pytest.raises(StateError, match="未回報目前載入的配方"):
        await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
        )


async def test_checksum_mismatch_is_blocked(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    await make_recipe(db, ppid="WB-QFN48-A", checksum="AAAA")
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WB-QFN48-A", "checksum": "BBBB"}, "op001")

    lot = await _lot_at_wire_bond(db, users)
    with pytest.raises(StateError, match="校驗碼不符"):
        await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
        )


async def test_checksum_skipped_when_equipment_cannot_report(factory, users):
    """老機台回報不出校驗碼時不該永遠過不了關。"""
    db = factory
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    await make_recipe(db, ppid="WB-QFN48-A", checksum="AAAA")
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WB-QFN48-A", "checksum": ""}, "op001")

    lot = await _lot_at_wire_bond(db, users)
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    assert result["status"] == "RUNNING"


async def test_track_in_unaffected_when_flag_off(factory, users):
    db = factory
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WRONG"}, "op001")
    lot = await _lot_at_wire_bond(db, users)
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    assert result["status"] == "RUNNING"


async def test_status_overview_flags_unknown_recipe(factory):
    db = factory
    await make_recipe(db, ppid="WB-QFN48-A")
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WB-QFN48-A"}, "op001")
    await recipe_service.set_loaded(db, "FT-01", {"ppid": "MYSTERY"}, "op001")

    overview = await recipe_service.status_overview(db)
    assert overview["loaded"] == 2
    assert overview["unknown_recipe"] == 1
    by_eq = {e["eq_id"]: e for e in overview["equipments"]}
    assert by_eq["WB-01"]["recognised"] is True
    assert by_eq["FT-01"]["recognised"] is False


# ── API ─────────────────────────────────────────────────────
async def test_recipe_api_flow(client, token, factory):
    eng = await token("eng01")
    res = await client.post(
        "/api/recipes",
        json={"ppid": "API-RCP", "name": "測試配方", "op_code": "WIRE_BOND",
              "device_id": "TEST-QFN48", "parameters": PARAMS},
        headers=eng,
    )
    assert res.status_code == 201, res.text

    release = await client.post(f"/api/recipes/API-RCP/{res.json()['version']}/release",
                                json={}, headers=eng)
    assert release.status_code == 200 and release.json()["status"] == "RELEASED"

    approved = await client.get(
        "/api/recipes/approved?op_code=WIRE_BOND&device_id=TEST-QFN48", headers=eng
    )
    assert [r["ppid"] for r in approved.json()] == ["API-RCP"]

    op = await token("op001")
    loaded = await client.put(
        "/api/recipes/equipments/WB-01/loaded", json={"ppid": "API-RCP"}, headers=op
    )
    assert loaded.status_code == 200

    overview = await client.get("/api/recipes/status", headers=eng)
    assert overview.json()["loaded"] == 1


async def test_recipe_api_requires_engineer(client, token, factory):
    op = await token("op001")
    res = await client.post(
        "/api/recipes",
        json={"ppid": "X", "name": "X", "op_code": "WIRE_BOND", "parameters": {"a": 1}},
        headers=op,
    )
    assert res.status_code == 403


async def test_secs_command_requires_connection(factory):
    from app.services import secs_service

    with pytest.raises(StateError, match="未連線"):
        await secs_service.download_recipe(factory, "WB-01", "X", None, "eng01")
