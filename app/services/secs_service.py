"""SECS/GEM 設備連線服務（GEM Host 側）。

分三層：

* ``secs.py``        —— 純協定編解碼，不碰資料庫
* ``secs_client.py`` —— HSMS-SS 連線與計時器
* 本檔               —— GEM 主機邏輯：連線設定、訊息記錄、事件對應到 MES 動作

真正讓 MES 有價值的是最後一段：設備送來的 CEID（事件代碼）要能自動變成
「設備轉生產中」「加工結束可出站」「異常轉非計畫停機」這些現場動作，
否則機台連上了也只是多一份 log。事件對應規則存在 ``secs_event_rules``，
可依設備覆寫，讓同一套 MES 對付不同廠牌的機台。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.database import (
    T_EQUIPMENTS,
    T_RECIPES,
    T_SECS_EVENT_RULES,
    T_SECS_LINKS,
    T_SECS_MESSAGES,
    fetch_all,
    fetch_one,
)
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import plain_values, utcnow
from app.models.enums import EquipmentState, SECSConnectionState, SECSEventAction
from app.services import audit_service, equipment_service, recipe_service, secs
from app.services.secs_client import HsmsClient

logger = logging.getLogger("mes.secs")

MDLN = "MES-HOST"
SOFTREV = "1.0.0"

DIRECTION_SEND = "SEND"
DIRECTION_RECV = "RECV"

#: 設備狀態字串 → MES 的 E10 狀態；涵蓋常見機台用語
EQ_STATE_ALIASES: dict[str, EquipmentState] = {
    "PRODUCTIVE": EquipmentState.PRODUCTIVE,
    "RUN": EquipmentState.PRODUCTIVE,
    "RUNNING": EquipmentState.PRODUCTIVE,
    "EXECUTING": EquipmentState.PRODUCTIVE,
    "STANDBY": EquipmentState.STANDBY,
    "IDLE": EquipmentState.STANDBY,
    "READY": EquipmentState.STANDBY,
    "ENGINEERING": EquipmentState.ENGINEERING,
    "SETUP": EquipmentState.SCHEDULED_DOWN,
    "PM": EquipmentState.SCHEDULED_DOWN,
    "SCHEDULED_DOWN": EquipmentState.SCHEDULED_DOWN,
    "DOWN": EquipmentState.UNSCHEDULED_DOWN,
    "ALARM": EquipmentState.UNSCHEDULED_DOWN,
    "UNSCHEDULED_DOWN": EquipmentState.UNSCHEDULED_DOWN,
    "OFFLINE": EquipmentState.NON_SCHEDULED,
    "NON_SCHEDULED": EquipmentState.NON_SCHEDULED,
}

LINK_COLUMNS = (
    "host", "port", "session_id", "mode", "t3_timeout_sec", "t5_timeout_sec",
    "linktest_sec", "enabled",
)


# ── 連線設定 ────────────────────────────────────────────────
async def upsert_link(db, eq_id: str, payload: dict, actor: str) -> dict:
    eq = await fetch_one(db, f"SELECT eq_id FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id)
    if eq is None:
        raise ValidationError(f"設備不存在：{eq_id}")

    data = {k: v for k, v in payload.items() if k in LINK_COLUMNS and v is not None}
    now = utcnow()
    existing = await fetch_one(db, f"SELECT eq_id FROM {T_SECS_LINKS} WHERE eq_id = $1", eq_id)
    if existing is None:
        columns = ("eq_id", *data.keys(), "created_at", "updated_at")
        values = (eq_id, *data.values(), now, now)
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        row = await fetch_one(
            db,
            f"INSERT INTO {T_SECS_LINKS} ({', '.join(columns)}) VALUES ({placeholders}) RETURNING *",
            *values,
        )
    else:
        if not data:
            raise ValidationError("沒有需要更新的欄位")
        assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(data, start=1))
        row = await fetch_one(
            db,
            f"UPDATE {T_SECS_LINKS} SET {assignments}, updated_at = ${len(data) + 1} "
            f"WHERE eq_id = ${len(data) + 2} RETURNING *",
            *data.values(), now, eq_id,
        )
    await audit_service.record_change(db, actor, "UPSERT", T_SECS_LINKS, eq_id, payload)
    return row


async def get_link(db, eq_id: str) -> dict:
    row = await fetch_one(db, f"SELECT * FROM {T_SECS_LINKS} WHERE eq_id = $1", eq_id)
    if row is None:
        raise NotFoundError(f"設備 {eq_id} 尚未設定 SECS 連線")
    return row


async def list_links(db) -> list[dict]:
    rows = await fetch_all(
        db,
        f"""
        SELECT l.*, e.name AS eq_name, e.current_state, e.area
        FROM {T_SECS_LINKS} l
        JOIN {T_EQUIPMENTS} e ON e.eq_id = l.eq_id
        ORDER BY l.eq_id
        """,
    )
    live = manager.states()
    return [{**row, "live_state": live.get(row["eq_id"])} for row in rows]


async def delete_link(db, eq_id: str, actor: str) -> dict:
    await get_link(db, eq_id)
    await manager.stop(eq_id)
    await db.execute(f"DELETE FROM {T_SECS_LINKS} WHERE eq_id = $1", eq_id)
    await audit_service.record_change(db, actor, "DELETE", T_SECS_LINKS, eq_id, None)
    return {"eq_id": eq_id, "deleted": True}


async def set_connection_state(db, eq_id: str, state: str, error: str = "") -> None:
    now = utcnow()
    await db.execute(
        f"""
        UPDATE {T_SECS_LINKS}
        SET connection_state = $1,
            last_error = NULLIF($2, ''),
            last_connected_at = CASE WHEN $1 = $3 THEN $4 ELSE last_connected_at END,
            updated_at = $4
        WHERE eq_id = $5
        """,
        state, error, SECSConnectionState.SELECTED.value, now, eq_id,
    )


# ── 訊息記錄 ────────────────────────────────────────────────
async def log_message(
    db, eq_id: str, direction: str, message: dict, description: str = "", raw: bytes | None = None
) -> dict:
    return await fetch_one(
        db,
        f"""
        INSERT INTO {T_SECS_MESSAGES}
            (eq_id, direction, stream, function, w_bit, system_bytes, description, body, raw_hex, timestamp)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        eq_id, direction, int(message.get("stream", 0)), int(message.get("function", 0)),
        bool(message.get("w_bit")), int(message.get("system_bytes", 0)),
        description or secs.message_name(message), plain_values(message.get("body")),
        raw.hex() if raw else None, utcnow(),
    )


async def list_messages(
    db, eq_id: str | None = None, stream: int | None = None, limit: int = 200
) -> list[dict]:
    rows = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_SECS_MESSAGES}
        WHERE ($1::text IS NULL OR eq_id = $1) AND ($2::int IS NULL OR stream = $2)
        ORDER BY timestamp DESC, id DESC LIMIT $3
        """,
        eq_id, stream, limit,
    )
    return [{**row, "sml": secs.to_sml(row["body"]) if row["body"] else ""} for row in rows]


# ── 事件對應規則 ────────────────────────────────────────────
async def upsert_rule(db, payload: dict, actor: str) -> dict:
    action = str(payload["action"])
    if action not in {a.value for a in SECSEventAction}:
        raise ValidationError(f"不支援的動作：{action}")
    row = await fetch_one(
        db,
        f"""
        INSERT INTO {T_SECS_EVENT_RULES} (eq_id, ceid, name, action, params, enabled)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (eq_id, ceid) DO UPDATE SET
            name = EXCLUDED.name, action = EXCLUDED.action,
            params = EXCLUDED.params, enabled = EXCLUDED.enabled
        RETURNING *
        """,
        payload.get("eq_id", ""), int(payload["ceid"]), payload["name"], action,
        plain_values(payload.get("params") or {}), bool(payload.get("enabled", True)),
    )
    await audit_service.record_change(
        db, actor, "UPSERT", T_SECS_EVENT_RULES, f"{payload.get('eq_id', '')}#{payload['ceid']}", payload
    )
    return row


async def list_rules(db, eq_id: str | None = None) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_SECS_EVENT_RULES}
        WHERE ($1::text IS NULL OR eq_id = $1 OR eq_id = '')
        ORDER BY ceid, eq_id
        """,
        eq_id,
    )


async def delete_rule(db, rule_id: int, actor: str) -> dict:
    row = await fetch_one(
        db, f"DELETE FROM {T_SECS_EVENT_RULES} WHERE id = $1 RETURNING *", rule_id
    )
    if row is None:
        raise NotFoundError(f"找不到事件規則：{rule_id}")
    await audit_service.record_change(db, actor, "DELETE", T_SECS_EVENT_RULES, str(rule_id), None)
    return {"id": rule_id, "deleted": True}


async def find_rule(db, eq_id: str, ceid: int) -> dict | None:
    """設備專屬規則優先於通用規則。"""
    return await fetch_one(
        db,
        f"""
        SELECT * FROM {T_SECS_EVENT_RULES}
        WHERE ceid = $1 AND enabled AND (eq_id = $2 OR eq_id = '')
        ORDER BY (eq_id = $2) DESC LIMIT 1
        """,
        ceid, eq_id,
    )


# ── GEM 主機邏輯 ────────────────────────────────────────────
def _report_values(body: dict | None) -> dict[str, Any]:
    """把 S6F11 的報告內容攤平成 ``{VID: 值}``。

    S6F11 的結構是 ``<L DATAID CEID <L <L RPTID <L V1 V2 …>>>>>``；
    不同廠牌對 VID 的命名天差地遠，攤平之後再交給規則參數挑選。
    """
    values: dict[str, Any] = {}
    parsed = secs.to_python(body)
    if not isinstance(parsed, list) or len(parsed) < 3:
        return values
    reports = parsed[2]
    if not isinstance(reports, list):
        return values
    for report in reports:
        if not isinstance(report, list) or len(report) < 2:
            continue
        rptid, variables = report[0], report[1]
        if not isinstance(variables, list):
            variables = [variables]
        for index, value in enumerate(variables):
            values[f"{rptid}.{index}"] = value
            values.setdefault(str(index), value)
    return values


def _resolve_state(rule: dict, values: dict[str, Any]) -> EquipmentState | None:
    """規則可以直接指定狀態，也可以指名從報告的哪個變數取值。"""
    params = rule.get("params") or {}
    raw = params.get("state")
    if not raw and params.get("state_vid") is not None:
        raw = values.get(str(params["state_vid"]))
    if raw is None:
        return None
    return EQ_STATE_ALIASES.get(str(raw).strip().upper())


async def _apply_rule(db, eq_id: str, rule: dict, values: dict[str, Any], actor: str) -> dict:
    action = rule["action"]
    params = rule.get("params") or {}

    if action == SECSEventAction.EQ_STATE.value:
        state = _resolve_state(rule, values)
        if state is None:
            return {"action": action, "applied": False, "reason": "規則未指定可辨識的設備狀態"}
        current = await fetch_one(
            db, f"SELECT current_lot_id FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id
        )
        eq = await equipment_service.set_state(
            db, eq_id, state, actor,
            reason_code=f"SECS:{rule['ceid']}", remark=rule["name"],
            # 設備狀態由機台回報，但在機批號仍以 MES 的進出站為準
            lot_id=(current or {}).get("current_lot_id"),
        )
        return {"action": action, "applied": True, "state": eq["current_state"]}

    if action == SECSEventAction.ALARM.value:
        eq = await equipment_service.set_state(
            db, eq_id, EquipmentState.UNSCHEDULED_DOWN, actor,
            reason_code=f"SECS:{rule['ceid']}", remark=params.get("remark") or rule["name"],
        )
        return {"action": action, "applied": True, "state": eq["current_state"]}

    if action == SECSEventAction.RECIPE_LOADED.value:
        vid = str(params.get("ppid_vid", "0"))
        ppid = params.get("ppid") or values.get(vid)
        if not ppid:
            return {"action": action, "applied": False, "reason": "事件報告中找不到配方名稱"}
        checksum_vid = params.get("checksum_vid")
        row = await recipe_service.set_loaded(
            db, eq_id,
            {"ppid": str(ppid), "checksum": str(values.get(str(checksum_vid), "") or "")},
            actor, source=recipe_service.SOURCE_SECS,
        )
        return {"action": action, "applied": True, "ppid": row["ppid"]}

    if action == SECSEventAction.TRACK_OUT_READY.value:
        eq = await fetch_one(
            db, f"SELECT eq_id, current_lot_id FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id
        )
        lot_id = (eq or {}).get("current_lot_id")
        return {
            "action": action,
            "applied": True,
            "lot_id": lot_id,
            "message": (
                f"設備 {eq_id} 回報加工結束，批號 {lot_id} 可出站"
                if lot_id else f"設備 {eq_id} 回報加工結束，但 MES 上沒有在機批號"
            ),
        }

    return {"action": SECSEventAction.LOG_ONLY.value, "applied": True}


async def handle_event_report(db, eq_id: str, body: dict | None, actor: str = "secs") -> dict:
    """處理 S6F11：查規則 → 執行 MES 動作。"""
    parsed = secs.to_python(body)
    ceid = None
    if isinstance(parsed, list) and len(parsed) >= 2:
        ceid = parsed[1]
    if isinstance(ceid, list):
        ceid = ceid[0] if ceid else None
    if ceid is None:
        return {"ceid": None, "matched": False, "result": {"action": "LOG_ONLY", "applied": False}}

    rule = await find_rule(db, eq_id, int(ceid))
    if rule is None:
        return {"ceid": int(ceid), "matched": False,
                "result": {"action": SECSEventAction.LOG_ONLY.value, "applied": True}}
    values = _report_values(body)
    result = await _apply_rule(db, eq_id, rule, values, actor)
    return {"ceid": int(ceid), "matched": True, "rule": rule["name"], "values": values, "result": result}


async def handle_alarm(db, eq_id: str, body: dict | None, actor: str = "secs") -> dict:
    """處理 S5F1：ALCD 最高位元為 1 代表警報發生，為 0 代表解除。"""
    parsed = secs.to_python(body)
    alcd, alid, altx = 0, None, ""
    if isinstance(parsed, list) and parsed:
        alcd = parsed[0] if isinstance(parsed[0], int) else 0
        if len(parsed) > 1:
            alid = parsed[1]
        if len(parsed) > 2:
            altx = str(parsed[2])
    raised = bool(int(alcd) & 0x80)
    if raised:
        eq = await equipment_service.set_state(
            db, eq_id, EquipmentState.UNSCHEDULED_DOWN, actor,
            reason_code=f"ALARM:{alid}", remark=altx or "設備警報",
        )
    else:
        eq = await equipment_service.set_state(
            db, eq_id, EquipmentState.STANDBY, actor,
            reason_code=f"ALARM_CLEAR:{alid}", remark=altx or "警報解除",
        )
    return {
        "alid": alid, "alarm_text": altx, "raised": raised, "state": eq["current_state"],
    }


async def handle_message(db, eq_id: str, message: dict, actor: str = "secs") -> dict:
    """GEM 主機收到訊息後的統一入口。

    回傳 ``reply``（要回給設備的訊息，沒有就是 None）與 ``actions``（對 MES 的影響），
    連線層與模擬測試都走同一條路徑。
    """
    stream, function = int(message.get("stream", 0)), int(message.get("function", 0))
    body = message.get("body")
    await log_message(db, eq_id, DIRECTION_RECV, message)

    reply: dict | None = None
    actions: dict[str, Any] = {}

    if (stream, function) == (1, 1):  # Are You There
        reply = {"function": 2, "body": secs.L(secs.A(MDLN), secs.A(SOFTREV))}
    elif (stream, function) == (1, 13):  # Establish Communications Request
        reply = {"function": 14, "body": secs.L(secs.B(0), secs.L(secs.A(MDLN), secs.A(SOFTREV)))}
    elif (stream, function) == (5, 1):  # Alarm Report Send
        actions = await handle_alarm(db, eq_id, body, actor)
        reply = {"function": 2, "body": secs.B(0)}
    elif (stream, function) == (6, 11):  # Event Report Send
        actions = await handle_event_report(db, eq_id, body, actor)
        reply = {"function": 12, "body": secs.B(0)}
    elif stream == 9:  # 對端回報的協定錯誤，只留紀錄
        actions = {"error_message": secs.message_name(message)}
    elif message.get("w_bit"):
        # 未實作的訊息一律回 SxF0（Abort），不能讓對方一直等
        reply = {"function": 0, "body": None}

    if reply is not None:
        outgoing = {
            "stream": stream, "function": reply["function"], "w_bit": False,
            "system_bytes": message.get("system_bytes", 0), "body": reply["body"],
        }
        await log_message(db, eq_id, DIRECTION_SEND, outgoing)
    return {"reply": reply, "actions": actions}


async def simulate_event(db, eq_id: str, payload: dict, actor: str) -> dict:
    """不接實機也能驗證整條路徑：直接灌一則 S6F11 進來。

    導入初期機台還沒接上、或要重現某個事件時非常實用。
    """
    ceid = int(payload["ceid"])
    reports = payload.get("reports") or []
    body = secs.L(
        secs.U4(int(payload.get("data_id", 0))),
        secs.U4(ceid),
        secs.L(
            *[
                secs.L(
                    secs.U4(int(report.get("rptid", index + 1))),
                    secs.L(*[secs.A(str(v)) for v in (report.get("values") or [])]),
                )
                for index, report in enumerate(reports)
            ]
        ),
    )
    message = {
        "stream": 6, "function": 11, "w_bit": True, "system_bytes": 0,
        "stype": secs.ST_DATA, "body": body,
    }
    return await handle_message(db, eq_id, message, actor)


async def send_command(db, eq_id: str, payload: dict, actor: str) -> dict:
    """S2F41 遠端指令（START / STOP / PP-SELECT…）—— 需要真的連上設備。"""
    client = manager.get(eq_id)
    if client is None or not client.selected:
        raise StateError(f"設備 {eq_id} 目前未連線（SELECTED），無法下達遠端指令")
    body = secs.L(
        secs.A(str(payload["command"])),
        secs.L(
            *[
                secs.L(secs.A(str(name)), secs.A(str(value)))
                for name, value in (payload.get("parameters") or {}).items()
            ]
        ),
    )
    request = {"stream": 2, "function": 41, "w_bit": True, "system_bytes": 0, "body": body}
    await log_message(db, eq_id, DIRECTION_SEND, request, f"遠端指令 {payload['command']}")
    reply = await client.request(2, 41, body)
    await log_message(db, eq_id, DIRECTION_RECV, reply)
    hcack = secs.to_python(reply.get("body"))
    if isinstance(hcack, list) and hcack:
        hcack = hcack[0]
    await audit_service.record_change(
        db, actor, "COMMAND", T_SECS_LINKS, eq_id, {"command": payload["command"], "hcack": hcack}
    )
    return {"eq_id": eq_id, "command": payload["command"], "hcack": hcack, "reply": reply}


async def _require_selected(eq_id: str) -> HsmsClient:
    client = manager.get(eq_id)
    if client is None or not client.selected:
        raise StateError(f"設備 {eq_id} 目前未連線（SELECTED），無法執行此操作")
    return client


async def list_equipment_recipes(db, eq_id: str, actor: str) -> dict:
    """S7F19 —— 問機台「你身上有哪些配方」。

    導入時最實用的一支：核可清單與機台實際擁有的配方常常對不起來，
    先撈回來比對，才知道要下發哪幾支。
    """
    client = await _require_selected(eq_id)
    request = {"stream": 7, "function": 19, "w_bit": True, "system_bytes": 0, "body": None}
    await log_message(db, eq_id, DIRECTION_SEND, request, "查詢機台配方清單")
    reply = await client.request(7, 19, None)
    await log_message(db, eq_id, DIRECTION_RECV, reply)

    ppids = secs.to_python(reply.get("body")) or []
    if isinstance(ppids, str):
        ppids = [ppids]
    ppids = [str(p) for p in ppids]

    approved = await fetch_all(
        db, f"SELECT DISTINCT ppid FROM {T_RECIPES} WHERE status = 'RELEASED'"
    )
    approved_set = {r["ppid"] for r in approved}
    return {
        "eq_id": eq_id,
        "ppids": ppids,
        "approved": sorted(set(ppids) & approved_set),
        "not_approved": sorted(set(ppids) - approved_set),
        "missing_on_equipment": sorted(approved_set - set(ppids)),
    }


async def upload_recipe(db, eq_id: str, ppid: str, actor: str) -> dict:
    """S7F5 —— 把機台上的配方內容取回來，用來核對參數是否被改過。"""
    client = await _require_selected(eq_id)
    body = secs.A(ppid)
    await log_message(
        db, eq_id, DIRECTION_SEND,
        {"stream": 7, "function": 5, "w_bit": True, "system_bytes": 0, "body": body},
        f"取回配方 {ppid}",
    )
    reply = await client.request(7, 5, body)
    await log_message(db, eq_id, DIRECTION_RECV, reply)
    return {"eq_id": eq_id, "ppid": ppid, "content": secs.to_python(reply.get("body"))}


async def download_recipe(db, eq_id: str, ppid: str, version: int | None, actor: str) -> dict:
    """S7F3 —— 把 MES 上核可的配方下發到機台。"""
    # 先確認連線：機台沒連上時，「設備未連線」比「找不到配方」更貼近現場實情
    client = await _require_selected(eq_id)
    recipe = await recipe_service.get_recipe(db, ppid, version)
    if recipe["status"] != recipe_service.STATUS_RELEASED:
        raise StateError(f"配方 {ppid} v{recipe['version']} 尚未發行，不可下發到機台")
    # PPBODY 以「參數名=值」逐行組成；不同機台格式不同，實機導入時依手冊調整
    text = "\n".join(f"{k}={v}" for k, v in sorted((recipe["parameters"] or {}).items()))
    body = secs.L(secs.A(ppid), secs.A(text))
    await log_message(
        db, eq_id, DIRECTION_SEND,
        {"stream": 7, "function": 3, "w_bit": True, "system_bytes": 0, "body": body},
        f"下發配方 {ppid} v{recipe['version']}",
    )
    reply = await client.request(7, 3, body)
    await log_message(db, eq_id, DIRECTION_RECV, reply)

    ackc7 = secs.to_python(reply.get("body"))
    if isinstance(ackc7, list):
        ackc7 = ackc7[0] if ackc7 else None
    if ackc7 not in (0, None):
        raise StateError(f"設備 {eq_id} 拒絕配方下發，ACKC7={ackc7}")

    await recipe_service.set_loaded(
        db, eq_id,
        {"ppid": ppid, "version": recipe["version"], "checksum": recipe["checksum"]},
        actor, source=recipe_service.SOURCE_DOWNLOAD,
    )
    await audit_service.record_change(
        db, actor, "DOWNLOAD_RECIPE", T_SECS_LINKS, eq_id, {"ppid": ppid, "version": recipe["version"]}
    )
    return {"eq_id": eq_id, "ppid": ppid, "version": recipe["version"], "ackc7": ackc7}


async def select_recipe(db, eq_id: str, ppid: str, actor: str) -> dict:
    """S2F41 PP-SELECT —— 叫機台切換到指定配方，並把結果寫回 MES。"""
    result = await send_command(
        db, eq_id, {"command": "PP-SELECT", "parameters": {"PPID": ppid}}, actor
    )
    if result.get("hcack") not in (0, None):
        raise StateError(f"設備 {eq_id} 拒絕切換配方 {ppid}，HCACK={result.get('hcack')}")
    await recipe_service.set_loaded(
        db, eq_id, {"ppid": ppid}, actor, source=recipe_service.SOURCE_SECS
    )
    return {"eq_id": eq_id, "ppid": ppid, "hcack": result.get("hcack")}


async def sync_clock(db, eq_id: str, actor: str, now: datetime | None = None) -> dict:
    """S2F31 對時 —— 設備時間跟 MES 差太多，事件時序就全亂了。"""
    client = manager.get(eq_id)
    if client is None or not client.selected:
        raise StateError(f"設備 {eq_id} 目前未連線（SELECTED），無法對時")
    stamp = (now or utcnow()).strftime("%Y%m%d%H%M%S00")
    body = secs.A(stamp)
    await log_message(
        db, eq_id, DIRECTION_SEND,
        {"stream": 2, "function": 31, "w_bit": True, "system_bytes": 0, "body": body}, "對時",
    )
    reply = await client.request(2, 31, body)
    await log_message(db, eq_id, DIRECTION_RECV, reply)
    return {"eq_id": eq_id, "timestamp": stamp, "tiack": secs.to_python(reply.get("body"))}


# ── 連線管理員 ──────────────────────────────────────────────
class ConnectionManager:
    """管理所有設備的 HSMS 連線；資料庫寫入交給連線池自己取連線。"""

    def __init__(self) -> None:
        self._clients: dict[str, HsmsClient] = {}

    def get(self, eq_id: str) -> HsmsClient | None:
        return self._clients.get(eq_id)

    def states(self) -> dict[str, str]:
        return {eq_id: client.state for eq_id, client in self._clients.items()}

    def info(self) -> list[dict]:
        return [client.info() for client in self._clients.values()]

    async def start(self, link: dict) -> HsmsClient:
        eq_id = link["eq_id"]
        await self.stop(eq_id)
        client = HsmsClient(
            eq_id=eq_id,
            host=link["host"],
            port=int(link["port"]),
            session_id=int(link.get("session_id") or 0),
            t3_timeout_sec=int(link.get("t3_timeout_sec") or 45),
            t5_timeout_sec=int(link.get("t5_timeout_sec") or 10),
            linktest_sec=int(link.get("linktest_sec") or 30),
            on_message=self._on_message,
            on_state=self._on_state,
        )
        self._clients[eq_id] = client
        await client.start()
        return client

    async def stop(self, eq_id: str) -> bool:
        client = self._clients.pop(eq_id, None)
        if client is None:
            return False
        await client.stop()
        return True

    async def stop_all(self) -> None:
        for eq_id in list(self._clients):
            await self.stop(eq_id)

    async def _on_state(self, client: HsmsClient, state: str, error: str) -> None:
        from app.database import acquire

        async with acquire() as conn:
            await set_connection_state(conn, client.eq_id, state, error)

    async def _on_message(self, client: HsmsClient, message: dict) -> None:
        from app.database import acquire

        async with acquire() as conn:
            result = await handle_message(conn, client.eq_id, message)
        reply = result.get("reply")
        if reply is not None:
            await client.reply(message, reply["function"], reply["body"])


manager = ConnectionManager()


async def start_link(db, eq_id: str, actor: str) -> dict:
    link = await get_link(db, eq_id)
    if not link["enabled"]:
        raise StateError(f"設備 {eq_id} 的 SECS 連線已停用，請先啟用再連線")
    await manager.start(link)
    await audit_service.record_change(db, actor, "CONNECT", T_SECS_LINKS, eq_id, None)
    return {"eq_id": eq_id, "started": True, "host": link["host"], "port": link["port"]}


async def stop_link(db, eq_id: str, actor: str) -> dict:
    stopped = await manager.stop(eq_id)
    await set_connection_state(db, eq_id, SECSConnectionState.NOT_CONNECTED.value)
    await audit_service.record_change(db, actor, "DISCONNECT", T_SECS_LINKS, eq_id, None)
    return {"eq_id": eq_id, "stopped": stopped}


async def start_enabled_links(db) -> list[str]:
    """服務啟動時把所有啟用中的連線拉起來。"""
    links = await fetch_all(db, f"SELECT * FROM {T_SECS_LINKS} WHERE enabled")
    started: list[str] = []
    for link in links:
        try:
            await manager.start(link)
            started.append(link["eq_id"])
        except Exception as exc:
            logger.warning("設備 %s 的 SECS 連線啟動失敗：%s", link["eq_id"], exc)
    return started


async def status(db) -> dict:
    """連線看板：幾台連上、幾台斷線、最近的訊息量。"""
    links = await list_links(db)
    recent = await fetch_all(
        db,
        f"""
        SELECT eq_id, count(*) AS messages, max(timestamp) AS last_at
        FROM {T_SECS_MESSAGES}
        WHERE timestamp >= now() - interval '1 hour'
        GROUP BY eq_id
        """,
    )
    by_eq = {r["eq_id"]: r for r in recent}
    selected = sum(1 for l in links if l["connection_state"] == SECSConnectionState.SELECTED.value)
    return {
        "total": len(links),
        "selected": selected,
        "disconnected": len(links) - selected,
        "links": [
            {
                **link,
                "messages_last_hour": int(by_eq.get(link["eq_id"], {}).get("messages", 0) or 0),
                "last_message_at": by_eq.get(link["eq_id"], {}).get("last_at"),
            }
            for link in links
        ],
    }
