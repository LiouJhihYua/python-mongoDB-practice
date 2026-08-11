"""現場終端機測試。"""

from __future__ import annotations

import pytest

from app.errors import NotFoundError
from app.services import (
    carrier_service,
    lot_service,
    recipe_service,
    sampling_service,
    sop_service,
    terminal_service,
    tool_service,
)
from tests.helpers import advance_to, make_lot, run_step
from tests.test_recipes import make_recipe
from tests.test_sampling import LEVELS
from tests.test_sop import STEPS

pytestmark = pytest.mark.asyncio


async def _lot_at(db, users, op_code: str):
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], op_code)
    return await lot_service.get_lot(db, lot["lot_id"])


# ── 掃描解析 ────────────────────────────────────────────────
async def test_scan_resolves_lot(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    result = await terminal_service.resolve(db, lot["lot_id"], users["op001"], "WB-01")

    assert result["kind"] == "LOT"
    assert result["ambiguous"] is False
    assert result["preflight"]["can_track_in"] is True
    assert [a["action"] for a in result["actions"]] == ["TRACK_IN", "HOLD"]


async def test_scan_resolves_equipment(factory, users):
    result = await terminal_service.resolve(factory, "WB-01", users["op001"])
    assert result["kind"] == "EQUIPMENT"
    assert result["equipment"]["name"] == "打線機 01"


async def test_scan_resolves_tool(factory, users):
    result = await terminal_service.resolve(factory, "CAP-001", users["op001"])
    assert result["kind"] == "TOOL"
    assert result["tool"]["usage_ratio"] == 0.0
    assert result["tool"]["needs_attention"] is False


async def test_scan_resolves_carrier(factory, users):
    db = factory
    await carrier_service.create_carrier(
        db, {"carrier_id": "MAG-777", "carrier_type": "MAGAZINE", "capacity": 25}, "qc01"
    )
    result = await terminal_service.resolve(db, "MAG-777", users["op001"])
    assert result["kind"] == "CARRIER"
    assert result["carrier"]["status"] == "EMPTY"


async def test_scan_resolves_wafer(factory, users):
    result = await terminal_service.resolve(factory, "WTEST001-01", users["op001"])
    assert result["kind"] == "WAFER"
    assert result["wafer"]["device_id"] == "TEST-QFN48"


async def test_scan_unknown_code(factory, users):
    with pytest.raises(NotFoundError, match="查無此條碼"):
        await terminal_service.resolve(factory, "NO-SUCH-THING", users["op001"])


async def test_scan_empty_code(factory, users):
    with pytest.raises(NotFoundError, match="請掃描"):
        await terminal_service.resolve(factory, "   ", users["op001"])


async def test_scan_reports_all_matches_when_ambiguous(factory, users):
    """編碼規則撞號時全部回報，讓現場自己選 —— 系統猜錯比較糟。"""
    db = factory
    await carrier_service.create_carrier(
        db, {"carrier_id": "CAP-001", "carrier_type": "TRAY", "capacity": 0}, "qc01"
    )
    result = await terminal_service.resolve(db, "CAP-001", users["op001"])
    assert result["ambiguous"] is True
    assert {m["kind"] for m in result["matches"]} == {"TOOL", "CARRIER"}


# ── 進站前置檢查 ────────────────────────────────────────────
async def test_preflight_passes_when_ready(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")

    assert check["blockers"] == []
    assert check["can_track_in"] is True
    assert check["operation"]["op_code"] == "WIRE_BOND"


async def test_preflight_flags_missing_equipment(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    check = await terminal_service.preflight(db, lot, users["op001"], "")
    assert any("必須指定設備" in b for b in check["blockers"])
    assert check["can_track_in"] is False


async def test_preflight_flags_wrong_equipment(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    check = await terminal_service.preflight(db, lot, users["op001"], "FT-01")
    assert any("不具備" in b for b in check["blockers"])


async def test_preflight_flags_uncertified_operator(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    check = await terminal_service.preflight(db, lot, users["op002"], "WB-01")
    assert any("資格認證" in b for b in check["blockers"])


async def test_preflight_flags_hold(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    await lot_service.hold_lot(
        db, {"lot_id": lot["lot_id"], "reason": "QUALITY", "remark": "抽檢"}, users["qc01"]
    )
    lot = await lot_service.get_lot(db, lot["lot_id"])
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")
    assert any("扣留中" in b for b in check["blockers"])
    assert "RELEASE" in [a["action"] for a in check["actions"]]


async def test_preflight_flags_unacknowledged_sop(factory, users):
    """這正是終端機的重點：按下去之前就知道會被擋。"""
    db = factory
    await db.execute("UPDATE operations SET require_sop_ack = TRUE WHERE op_code = 'WIRE_BOND'")
    await sop_service.create_sop(
        db,
        {"sop_code": "SOP-WB", "title": "打線作業", "op_code": "WIRE_BOND", "device_id": "",
         "summary": "", "steps": STEPS, "hazards": "", "ppe": [], "attachments": [],
         "require_ack": True},
        "eng01",
    )
    await sop_service.release_sop(db, "SOP-WB", 1, {}, "eng01")

    lot = await _lot_at(db, users, "WIRE_BOND")
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")
    assert any("尚未確認作業指導書" in b for b in check["blockers"])
    assert check["info"]["sop_ack_needed"] is True
    assert check["actions"][0]["action"] == "ACK_SOP"

    await sop_service.acknowledge(db, {"sop_code": "SOP-WB", "version": 1}, "op001")
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")
    assert check["blockers"] == []


async def test_preflight_flags_recipe_mismatch(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    await make_recipe(db, ppid="WB-OK")
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WRONG"}, "op001")

    lot = await _lot_at(db, users, "WIRE_BOND")
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")
    assert any("未核可" in b for b in check["blockers"])
    assert check["info"]["recipe"]["loaded"] == "WRONG"
    assert check["info"]["recipe"]["approved"] == ["WB-OK"]


async def test_preflight_flags_expired_tool(factory, users):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "op001")
    await db.execute(
        "UPDATE tools SET used_count = life_limit, status = 'EXPIRED' WHERE tool_id = 'CAP-001'"
    )
    lot = await _lot_at(db, users, "WIRE_BOND")
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")
    assert any("治具已到期" in b for b in check["blockers"])


async def test_preflight_warns_on_qtime(factory, users, clock):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    clock.advance(minutes=45)  # WIRE_BOND 的 Q-Time 上限是 60 分
    lot = await lot_service.get_lot(db, lot["lot_id"])
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")
    assert any("Q-Time 只剩" in w for w in check["warnings"])
    assert check["blockers"] == []


async def test_preflight_blocks_expired_qtime(factory, users, clock):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    clock.advance(minutes=90)
    lot = await lot_service.get_lot(db, lot["lot_id"])
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")
    assert any("Q-Time 已逾時" in b for b in check["blockers"])
    assert check["can_track_in"] is False


async def test_preflight_on_running_lot_offers_track_out(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    lot = await lot_service.get_lot(db, lot["lot_id"])
    check = await terminal_service.preflight(db, lot, users["op001"], "WB-01")

    assert check["status"] == "RUNNING"
    assert [a["action"] for a in check["actions"]] == ["TRACK_OUT", "HOLD"]
    # 出站畫面要能預填應產出量
    assert check["info"]["expected_output"] == 2000
    assert check["info"]["elapsed_sec"] is not None


async def test_preflight_reports_sampling_plan(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'FT'")
    await sampling_service.create_plan(
        db,
        {"plan_code": "FT-PLAN", "name": "測試抽樣", "op_code": "FT", "plan_type": "AQL",
         "lot_interval": 1, "aql": 1.0, "levels": LEVELS},
        "qc01",
    )
    lot = await _lot_at(db, users, "FT")
    check = await terminal_service.preflight(db, lot, users["op001"], "FT-01")
    assert check["info"]["sampling_plan"] == "FT-PLAN"


# ── 站別工作台 ──────────────────────────────────────────────
async def test_station_returns_full_workspace(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    data = await terminal_service.station(db, "WB-01", users["op001"])

    assert data["equipment"]["eq_id"] == "WB-01"
    assert data["equipment"]["runnable"] is True
    assert data["current_lot"] is None
    assert [q["lot_id"] for q in data["queue"]] == [lot["lot_id"]]
    assert data["queue_total"] == 1
    assert data["queue_truncated"] is False
    assert data["operator"]["username"] == "op001"


async def test_station_queue_reports_true_total_when_truncated(factory, users):
    """只顯示前幾批，但總數要誠實。

    螢幕上寫「2 批」而實際有 5 批在等，會讓現場低估自己落後多少。
    """
    db = factory
    for _ in range(5):
        await _lot_at(db, users, "WIRE_BOND")

    data = await terminal_service.station(db, "WB-01", users["op001"], queue_limit=2)
    assert len(data["queue"]) == 2
    assert data["queue_shown"] == 2
    assert data["queue_total"] == 5
    assert data["queue_truncated"] is True


async def test_station_shows_lot_on_machine(factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    data = await terminal_service.station(db, "WB-01", users["op001"])

    assert data["current_lot"]["lot_id"] == lot["lot_id"]
    assert data["current_lot"]["status"] == "RUNNING"
    assert data["equipment"]["current_lot_id"] == lot["lot_id"]


async def test_station_lists_tools_and_recipe(factory, users):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "op001")
    await recipe_service.set_loaded(db, "WB-01", {"ppid": "WB-A", "version": 1}, "op001")
    data = await terminal_service.station(db, "WB-01", users["op001"])

    assert [t["tool_id"] for t in data["tools"]] == ["CAP-001"]
    assert data["loaded_recipe"]["ppid"] == "WB-A"


async def test_station_lists_pending_sops_for_this_station_only(factory, users):
    db = factory
    for code, op_code in (("SOP-WB", "WIRE_BOND"), ("SOP-FT", "FT")):
        await sop_service.create_sop(
            db,
            {"sop_code": code, "title": op_code, "op_code": op_code, "device_id": "",
             "summary": "", "steps": STEPS, "hazards": "", "ppe": [], "attachments": [],
             "require_ack": True},
            "eng01",
        )
        await sop_service.release_sop(db, code, 1, {}, "eng01")

    data = await terminal_service.station(db, "WB-01", users["op001"])
    assert [s["sop_code"] for s in data["pending_sops"]] == ["SOP-WB"]


async def test_station_unknown_equipment(factory, users):
    with pytest.raises(NotFoundError):
        await terminal_service.station(factory, "NO-EQ", users["op001"])


async def test_stations_list(factory, users):
    rows = await terminal_service.stations(factory)
    assert {r["eq_id"] for r in rows} == {"DS-01", "WB-01", "FT-01"}
    assert await terminal_service.stations(factory, "ASSY") == [
        r for r in rows if r["area"] == "ASSY"
    ]


# ── API ─────────────────────────────────────────────────────
async def test_terminal_api_flow(client, token, factory, users):
    db = factory
    lot = await _lot_at(db, users, "WIRE_BOND")
    headers = await token("op001")

    stations = await client.get("/api/terminal/stations", headers=headers)
    assert stations.status_code == 200 and len(stations.json()) == 3

    scan = await client.post(
        "/api/terminal/scan", json={"code": lot["lot_id"], "eq_id": "WB-01"}, headers=headers
    )
    assert scan.status_code == 200
    assert scan.json()["kind"] == "LOT"
    assert scan.json()["preflight"]["can_track_in"] is True

    station = await client.get("/api/terminal/station/WB-01", headers=headers)
    assert station.json()["queue"][0]["lot_id"] == lot["lot_id"]

    # 終端機按下進站走的是既有的生產 API，不另外實作一套
    result = await client.post(
        "/api/lots/track-in",
        json={"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, headers=headers,
    )
    assert result.status_code == 200

    preflight = await client.get(
        f"/api/terminal/lots/{lot['lot_id']}/preflight?eq_id=WB-01", headers=headers
    )
    assert preflight.json()["status"] == "RUNNING"
    assert "TRACK_OUT" in [a["action"] for a in preflight.json()["actions"]]


async def test_terminal_api_requires_login(client, factory):
    res = await client.post("/api/terminal/scan", json={"code": "X"})
    assert res.status_code == 401


async def test_terminal_page_is_served(client, factory):
    res = await client.get("/terminal")
    assert res.status_code == 200
    assert "現場終端機" in res.text
