"""SECS/GEM 協定與設備連線測試。"""

from __future__ import annotations

import asyncio

import pytest

from app.errors import NotFoundError, StateError, ValidationError
from app.services import secs, secs_service
from app.services.secs_client import HsmsClient
from scripts.eqsim import EquipmentSimulator

pytestmark = pytest.mark.asyncio


# ── SECS-II 編解碼 ──────────────────────────────────────────
def _round_trip(item: dict) -> dict:
    decoded, consumed = secs.decode_item(secs.encode_item(item))
    assert consumed == len(secs.encode_item(item))
    return decoded


def test_ascii_round_trip():
    assert _round_trip(secs.A("WB-01")) == {"format": "A", "value": "WB-01"}


def test_numeric_round_trip():
    assert _round_trip(secs.U4(1, 2, 3)) == {"format": "U4", "value": [1, 2, 3]}
    assert _round_trip(secs.I2(-5)) == {"format": "I2", "value": [-5]}
    assert _round_trip(secs.U1(255)) == {"format": "U1", "value": [255]}


def test_float_round_trip():
    assert _round_trip(secs.F8(3.25))["value"] == [3.25]
    assert _round_trip(secs.F4(1.5))["value"] == [1.5]


def test_binary_and_boolean_round_trip():
    assert _round_trip(secs.B(0x80, 0x01)) == {"format": "B", "value": [128, 1]}
    assert _round_trip(secs.BOOLEAN(True, False)) == {"format": "BOOLEAN", "value": [True, False]}


def test_nested_list_round_trip():
    item = secs.L(secs.A("MES"), secs.L(secs.U4(7), secs.B(1)), secs.U2(9))
    assert _round_trip(item) == item


def test_empty_list():
    assert _round_trip(secs.L()) == {"format": "L", "value": []}


def test_long_item_uses_two_length_bytes():
    item = secs.A("x" * 300)
    raw = secs.encode_item(item)
    assert raw[0] & 0b11 == 2  # 長度佔 2 位元組
    assert _round_trip(item)["value"] == "x" * 300


def test_encode_rejects_unknown_format():
    with pytest.raises(secs.SECSError):
        secs.encode_item({"format": "ZZ", "value": 1})


def test_decode_rejects_truncated_data():
    raw = secs.encode_item(secs.A("hello"))
    with pytest.raises(secs.SECSError):
        secs.decode_item(raw[:-2])


def test_to_python_flattens_single_values():
    body = secs.L(secs.U4(3), secs.A("RUN"), secs.U4(1, 2))
    assert secs.to_python(body) == [3, "RUN", [1, 2]]


def test_to_sml_renders_structure():
    sml = secs.to_sml(secs.L(secs.A("MES"), secs.U4(1)))
    assert "<L [2]" in sml and '<A "MES">' in sml and "<U4 1>" in sml


# ── HSMS 訊息 ───────────────────────────────────────────────
def test_message_round_trip():
    body = secs.L(secs.A("MES"), secs.A("1.0.0"))
    raw = secs.encode_message(5, 1, 13, True, 0xDEADBEEF, body)
    frames, rest = secs.split_frames(raw)
    assert rest == b"" and len(frames) == 1

    message = secs.decode_message(frames[0])
    assert message["session_id"] == 5
    assert (message["stream"], message["function"]) == (1, 13)
    assert message["w_bit"] is True
    assert message["system_bytes"] == 0xDEADBEEF
    assert message["body"] == body
    assert secs.message_name(message) == "S1F13 W"


def test_control_message_round_trip():
    raw = secs.encode_message(0, 0, 0, False, 42, None, secs.ST_SELECT_REQ)
    message = secs.decode_message(secs.split_frames(raw)[0][0])
    assert message["stype"] == secs.ST_SELECT_REQ
    assert secs.message_name(message) == "SELECT.REQ"
    assert message["body"] is None


def test_split_frames_handles_partial_stream():
    first = secs.encode_message(0, 1, 1, True, 1, None)
    second = secs.encode_message(0, 6, 11, True, 2, secs.L(secs.U4(9)))
    stream = first + second
    frames, rest = secs.split_frames(stream[: len(first) + 6])
    assert len(frames) == 1 and rest == stream[len(first): len(first) + 6]

    frames2, rest2 = secs.split_frames(rest + stream[len(first) + 6:])
    assert len(frames2) == 1 and rest2 == b""


def test_decode_message_rejects_short_frame():
    with pytest.raises(secs.SECSError):
        secs.decode_message(b"\x00\x01")


# ── 連線設定 ────────────────────────────────────────────────
async def test_upsert_and_get_link(factory):
    db = factory
    created = await secs_service.upsert_link(
        db, "WB-01", {"host": "10.0.0.5", "port": 5001, "session_id": 1}, "eng01"
    )
    assert created["host"] == "10.0.0.5" and created["port"] == 5001
    assert created["connection_state"] == "NOT_CONNECTED"

    updated = await secs_service.upsert_link(db, "WB-01", {"port": 6000}, "eng01")
    assert updated["port"] == 6000 and updated["host"] == "10.0.0.5"

    links = await secs_service.list_links(db)
    assert len(links) == 1 and links[0]["eq_name"] == "打線機 01"


async def test_link_requires_known_equipment(factory):
    with pytest.raises(ValidationError, match="設備不存在"):
        await secs_service.upsert_link(factory, "NO-SUCH-EQ", {"host": "x"}, "eng01")


async def test_get_link_not_found(factory):
    with pytest.raises(NotFoundError):
        await secs_service.get_link(factory, "WB-01")


async def test_delete_link(factory):
    db = factory
    await secs_service.upsert_link(db, "WB-01", {"host": "127.0.0.1"}, "eng01")
    assert (await secs_service.delete_link(db, "WB-01", "eng01"))["deleted"] is True
    with pytest.raises(NotFoundError):
        await secs_service.get_link(db, "WB-01")


async def test_connect_rejects_disabled_link(factory):
    db = factory
    await secs_service.upsert_link(db, "WB-01", {"host": "127.0.0.1", "enabled": False}, "eng01")
    with pytest.raises(StateError, match="已停用"):
        await secs_service.start_link(db, "WB-01", "eng01")


# ── 事件規則 ────────────────────────────────────────────────
async def test_rule_upsert_and_priority(factory):
    db = factory
    await secs_service.upsert_rule(
        db, {"eq_id": "", "ceid": 2003, "name": "通用狀態", "action": "EQ_STATE",
             "params": {"state": "STANDBY"}, "enabled": True}, "eng01",
    )
    await secs_service.upsert_rule(
        db, {"eq_id": "WB-01", "ceid": 2003, "name": "打線機狀態", "action": "EQ_STATE",
             "params": {"state_vid": "0"}, "enabled": True}, "eng01",
    )
    rule = await secs_service.find_rule(db, "WB-01", 2003)
    assert rule["name"] == "打線機狀態"

    other = await secs_service.find_rule(db, "FT-01", 2003)
    assert other["name"] == "通用狀態"


async def test_rule_rejects_unknown_action(factory):
    with pytest.raises(ValidationError, match="不支援的動作"):
        await secs_service.upsert_rule(
            factory, {"ceid": 1, "name": "X", "action": "NOPE"}, "eng01"
        )


async def test_delete_rule(factory):
    db = factory
    rule = await secs_service.upsert_rule(
        db, {"ceid": 9, "name": "X", "action": "LOG_ONLY"}, "eng01"
    )
    assert (await secs_service.delete_rule(db, rule["id"], "eng01"))["deleted"] is True
    with pytest.raises(NotFoundError):
        await secs_service.delete_rule(db, rule["id"], "eng01")


# ── GEM 訊息處理 ────────────────────────────────────────────
async def test_are_you_there_gets_reply(factory):
    db = factory
    message = {"stream": 1, "function": 1, "w_bit": True, "system_bytes": 7,
               "stype": secs.ST_DATA, "body": None}
    result = await secs_service.handle_message(db, "WB-01", message)
    assert result["reply"]["function"] == 2
    assert secs.to_python(result["reply"]["body"])[0] == secs_service.MDLN

    rows = await secs_service.list_messages(db, "WB-01")
    assert {r["direction"] for r in rows} == {"RECV", "SEND"}


async def test_establish_communications(factory):
    message = {"stream": 1, "function": 13, "w_bit": True, "system_bytes": 1,
               "stype": secs.ST_DATA, "body": secs.L()}
    result = await secs_service.handle_message(factory, "WB-01", message)
    assert result["reply"]["function"] == 14
    assert secs.to_python(result["reply"]["body"])[0] == 0  # COMMACK = 0


async def test_unimplemented_message_gets_abort(factory):
    message = {"stream": 7, "function": 3, "w_bit": True, "system_bytes": 1,
               "stype": secs.ST_DATA, "body": None}
    result = await secs_service.handle_message(factory, "WB-01", message)
    assert result["reply"]["function"] == 0


async def test_message_without_w_bit_needs_no_reply(factory):
    message = {"stream": 9, "function": 5, "w_bit": False, "system_bytes": 1,
               "stype": secs.ST_DATA, "body": None}
    result = await secs_service.handle_message(factory, "WB-01", message)
    assert result["reply"] is None


# ── 事件 → MES 動作 ────────────────────────────────────────
async def test_event_changes_equipment_state(factory):
    db = factory
    await secs_service.upsert_rule(
        db, {"eq_id": "WB-01", "ceid": 2001, "name": "開始加工", "action": "EQ_STATE",
             "params": {"state": "PRODUCTIVE"}}, "eng01",
    )
    result = await secs_service.simulate_event(
        db, "WB-01", {"ceid": 2001, "data_id": 0, "reports": []}, "eng01"
    )
    assert result["actions"]["matched"] is True
    assert result["actions"]["result"]["state"] == "PRODUCTIVE"

    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'WB-01'")
    assert eq["current_state"] == "PRODUCTIVE" and eq["state_reason"] == "SECS:2001"


async def test_event_state_read_from_report_variable(factory):
    db = factory
    await secs_service.upsert_rule(
        db, {"eq_id": "WB-01", "ceid": 2003, "name": "狀態變更", "action": "EQ_STATE",
             "params": {"state_vid": "1.0"}}, "eng01",
    )
    result = await secs_service.simulate_event(
        db, "WB-01", {"ceid": 2003, "reports": [{"rptid": 1, "values": ["DOWN"]}]}, "eng01"
    )
    assert result["actions"]["result"]["state"] == "UNSCHEDULED_DOWN"


async def test_event_with_unmappable_state_is_reported(factory):
    db = factory
    await secs_service.upsert_rule(
        db, {"eq_id": "WB-01", "ceid": 2003, "name": "狀態變更", "action": "EQ_STATE",
             "params": {"state_vid": "1.0"}}, "eng01",
    )
    result = await secs_service.simulate_event(
        db, "WB-01", {"ceid": 2003, "reports": [{"rptid": 1, "values": ["外星狀態"]}]}, "eng01"
    )
    assert result["actions"]["result"]["applied"] is False


async def test_unknown_ceid_is_logged_only(factory):
    result = await secs_service.simulate_event(factory, "WB-01", {"ceid": 9999}, "eng01")
    assert result["actions"]["matched"] is False
    assert result["actions"]["result"]["action"] == "LOG_ONLY"


async def test_track_out_ready_reports_lot_on_equipment(factory, users):
    from tests.helpers import advance_to, make_lot
    from app.services import lot_service

    db = factory
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "WB-01", "remark": ""}, users["op001"]
    )
    await secs_service.upsert_rule(
        db, {"eq_id": "WB-01", "ceid": 2002, "name": "加工結束", "action": "TRACK_OUT_READY"}, "eng01"
    )
    result = await secs_service.simulate_event(db, "WB-01", {"ceid": 2002}, "eng01")
    assert result["actions"]["result"]["lot_id"] == lot["lot_id"]


async def test_alarm_sets_and_clears_equipment_down(factory):
    db = factory
    raised = await secs_service.handle_message(
        db, "WB-01",
        {"stream": 5, "function": 1, "w_bit": True, "system_bytes": 1, "stype": secs.ST_DATA,
         "body": secs.L(secs.B(0x81), secs.U4(101), secs.A("吸嘴真空不足"))},
    )
    assert raised["actions"]["raised"] is True
    assert raised["actions"]["state"] == "UNSCHEDULED_DOWN"
    assert raised["reply"]["function"] == 2

    cleared = await secs_service.handle_message(
        db, "WB-01",
        {"stream": 5, "function": 1, "w_bit": True, "system_bytes": 2, "stype": secs.ST_DATA,
         "body": secs.L(secs.B(0x01), secs.U4(101), secs.A("吸嘴真空不足"))},
    )
    assert cleared["actions"]["raised"] is False
    assert cleared["actions"]["state"] == "STANDBY"


async def test_status_dashboard(factory):
    db = factory
    await secs_service.upsert_link(db, "WB-01", {"host": "127.0.0.1", "port": 5000}, "eng01")
    await secs_service.simulate_event(db, "WB-01", {"ceid": 1}, "eng01")
    status = await secs_service.status(db)
    assert status["total"] == 1 and status["disconnected"] == 1
    assert status["links"][0]["messages_last_hour"] >= 1


async def test_command_requires_live_connection(factory):
    with pytest.raises(StateError, match="未連線"):
        await secs_service.send_command(factory, "WB-01", {"command": "START"}, "eng01")


# ── 與模擬器的端對端連線 ────────────────────────────────────
async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def test_end_to_end_against_simulator(factory):
    """把模擬器當成真機台：連線 → Select → S1F1 → 設備主動送事件。"""
    db = factory
    await secs_service.upsert_rule(
        db, {"eq_id": "WB-01", "ceid": 2003, "name": "狀態變更", "action": "EQ_STATE",
             "params": {"state_vid": "1.0"}}, "eng01",
    )

    sim = EquipmentSimulator(port=0, model="SIM-BONDER")
    port = await sim.start()
    received: list[dict] = []

    async def on_message(client, message):
        result = await secs_service.handle_message(db, client.eq_id, message)
        received.append(result)
        if result["reply"] is not None:
            await client.reply(message, result["reply"]["function"], result["reply"]["body"])

    client = HsmsClient(
        "WB-01", "127.0.0.1", port, t5_timeout_sec=1, t6_timeout_sec=2, linktest_sec=5,
        on_message=on_message,
    )
    try:
        await client.start()
        assert await _wait_for(lambda: client.selected), "未能進入 SELECTED"

        # 主機 → 設備：Are You There
        reply = await client.request(1, 1, None, timeout=3)
        assert (reply["stream"], reply["function"]) == (1, 2)
        assert secs.to_python(reply["body"])[0] == "SIM-BONDER"

        # 設備 → 主機：事件報告，MES 應自動改設備狀態
        await sim.send_event(2003, [["EXECUTING"]])
        assert await _wait_for(lambda: bool(received)), "未收到事件報告"
        assert received[0]["actions"]["result"]["state"] == "PRODUCTIVE"

        eq = await db.fetchrow("SELECT current_state FROM equipments WHERE eq_id = 'WB-01'")
        assert eq["current_state"] == "PRODUCTIVE"
    finally:
        await client.stop()
        await sim.stop()


async def test_simulator_answers_remote_command(factory):
    sim = EquipmentSimulator(port=0)
    port = await sim.start()
    client = HsmsClient("WB-01", "127.0.0.1", port, t5_timeout_sec=1, t6_timeout_sec=2, linktest_sec=5)
    try:
        await client.start()
        assert await _wait_for(lambda: client.selected)
        reply = await client.request(2, 41, secs.L(secs.A("START"), secs.L()), timeout=3)
        assert (reply["stream"], reply["function"]) == (2, 42)
        assert secs.to_python(reply["body"])[0] == 0
        assert sim.state == "EXECUTING"
    finally:
        await client.stop()
        await sim.stop()


# ── API ─────────────────────────────────────────────────────
async def test_secs_api_flow(client, token, factory):
    headers = await token("eng01")

    link = await client.put(
        "/api/secs/links/WB-01", json={"host": "127.0.0.1", "port": 5000}, headers=headers
    )
    assert link.status_code == 200, link.text

    rule = await client.put(
        "/api/secs/rules",
        json={"eq_id": "WB-01", "ceid": 2001, "name": "開始加工", "action": "EQ_STATE",
              "params": {"state": "PRODUCTIVE"}},
        headers=headers,
    )
    assert rule.status_code == 200

    event = await client.post(
        "/api/secs/links/WB-01/simulate-event", json={"ceid": 2001}, headers=headers
    )
    assert event.json()["actions"]["result"]["state"] == "PRODUCTIVE"

    messages = await client.get("/api/secs/messages?eq_id=WB-01", headers=headers)
    assert any(m["stream"] == 6 for m in messages.json())

    status = await client.get("/api/secs/status", headers=headers)
    assert status.json()["total"] == 1


async def test_decode_api(client, token, factory):
    headers = await token("eng01")
    raw = secs.encode_message(0, 6, 11, True, 5, secs.L(secs.U4(0), secs.U4(2001), secs.L()))
    res = await client.post("/api/secs/decode", json={"hex": raw.hex()}, headers=headers)
    assert res.status_code == 200
    assert res.json()["name"] == "S6F11 W"
    assert res.json()["python"] == [0, 2001, []]


async def test_decode_api_rejects_garbage(client, token, factory):
    headers = await token("eng01")
    res = await client.post("/api/secs/decode", json={"hex": "zzzz"}, headers=headers)
    assert res.status_code == 422
