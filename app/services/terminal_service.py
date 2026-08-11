"""現場終端機。

看板是給辦公室看的；這一層是給站在機台旁邊、戴著手套、拿掃描槍的人用的。
兩者對 API 的需求完全不同：

* **一次掃描要回答完整個問題** —— 掃到的這串字是什麼？現在什麼狀態？我能做什麼？
  現場網路常常不好，來回問三四次會慢到讓人放棄
* **會被擋的原因要「事先」知道** —— 最讓現場惱火的是按下進站才被拒絕。
  ``preflight`` 把 Track-In 會做的檢查先跑一遍（唯讀），讓終端機在按鈕旁邊
  就標出「你還沒簽 SOP」「機台配方沒對上」
* **一次呼叫餵滿整個畫面** —— ``station`` 把設備狀態、在機批號、待進站佇列、
  SOP、配方、治具壽命全部包成一包

``preflight`` 是**諮詢性質**的：真正的把關仍然在 ``lot_service.track_in`` 裡，
而且是在資料庫交易與列鎖之內。這裡先算一次只是為了讓現場少白跑一趟。
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.database import (
    T_CARRIERS,
    T_EQUIPMENTS,
    T_LOTS,
    T_TOOLS,
    T_WAFERS,
    fetch_all,
    fetch_one,
)
from app.errors import MESError, NotFoundError
from app.models.base import ensure_aware, utcnow
from app.models.enums import LotStatus, RUNNABLE_EQUIPMENT_STATES
from app.services import (
    dispatch_service,
    lot_service,
    recipe_service,
    sampling_service,
    sop_service,
    tool_service,
)
from app.services.user_service import is_certified

KIND_LOT = "LOT"
KIND_EQUIPMENT = "EQUIPMENT"
KIND_TOOL = "TOOL"
KIND_CARRIER = "CARRIER"
KIND_WAFER = "WAFER"

RUNNABLE_STATES = {s.value for s in RUNNABLE_EQUIPMENT_STATES}

#: Q-Time 剩餘低於此分鐘數就在終端機上示警
QTIME_WARN_MINUTES = 30


# ── 掃描解析 ────────────────────────────────────────────────
async def resolve(db, code: str, user: dict, eq_id: str = "") -> dict:
    """掃到一串字，回答「這是什麼、現在什麼狀態、我能做什麼」。

    刻意**不猜**：五種主檔都查一遍，全部命中的都回報。編碼規則撞號時
    （例如載具與治具用了同一個前綴），現場自己選比系統猜錯好。
    """
    code = (code or "").strip()
    if not code:
        raise NotFoundError("請掃描或輸入條碼")

    matches: list[dict[str, Any]] = []
    result: dict[str, Any] = {"code": code, "matches": matches, "actions": [], "blockers": []}

    lot = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1", code)
    if lot is not None:
        matches.append({"kind": KIND_LOT, "id": lot["lot_id"], "label": _lot_label(lot)})
        result["lot"] = lot

    equipment = await fetch_one(db, f"SELECT * FROM {T_EQUIPMENTS} WHERE eq_id = $1", code)
    if equipment is not None:
        matches.append({"kind": KIND_EQUIPMENT, "id": equipment["eq_id"],
                        "label": f"{equipment['eq_id']} {equipment['name']}"})
        result["equipment"] = equipment

    tool = await fetch_one(db, f"SELECT * FROM {T_TOOLS} WHERE tool_id = $1", code)
    if tool is not None:
        decorated = tool_service._decorate(dict(tool))
        matches.append({"kind": KIND_TOOL, "id": tool["tool_id"],
                        "label": f"{tool['tool_id']} {tool['name']}"})
        result["tool"] = decorated

    carrier = await fetch_one(db, f"SELECT * FROM {T_CARRIERS} WHERE carrier_id = $1", code)
    if carrier is not None:
        matches.append({"kind": KIND_CARRIER, "id": carrier["carrier_id"],
                        "label": f"{carrier['carrier_id']} {carrier['carrier_type']}"})
        result["carrier"] = carrier

    wafer = await fetch_one(db, f"SELECT * FROM {T_WAFERS} WHERE wafer_id = $1", code)
    if wafer is not None:
        matches.append({"kind": KIND_WAFER, "id": wafer["wafer_id"],
                        "label": f"{wafer['wafer_id']} {wafer['device_id']}"})
        result["wafer"] = wafer

    if not matches:
        raise NotFoundError(f"查無此條碼：{code}（批號／設備／治具／載具／晶圓都找不到）")

    result["kind"] = matches[0]["kind"]
    result["ambiguous"] = len(matches) > 1

    if lot is not None:
        check = await preflight(db, lot, user, eq_id)
        result["preflight"] = check
        result["actions"] = check["actions"]
        result["blockers"] = check["blockers"]
    return result


def _lot_label(lot: dict) -> str:
    return f"{lot['lot_id']} {lot['device_id']} {lot['qty']}{lot['unit_type']}"


# ── 進站前置檢查（唯讀）─────────────────────────────────────
async def preflight(db, lot: dict, user: dict, eq_id: str = "") -> dict:
    """把 Track-In 會做的檢查先跑一遍，讓現場在按下去之前就知道會不會被擋。

    **這是諮詢性質的**：真正的把關仍在 ``lot_service.track_in`` 的交易與列鎖之內
    （否則兩個人同時進站時檢查會失效）。這裡先算一次只是為了少白跑一趟。
    """
    actor = user["username"]
    blockers: list[str] = []
    warnings: list[str] = []
    info: dict[str, Any] = {}
    status = lot["status"]

    if status == LotStatus.HOLD.value:
        hold = await lot_service.find_open_hold(db, lot["lot_id"])
        blockers.append(f"批號扣留中（{(hold or {}).get('reason', '')}）：{(hold or {}).get('remark', '')}")
    elif status == LotStatus.RUNNING.value:
        info["running_on"] = lot["eq_id"]
    elif status != LotStatus.WAITING.value:
        blockers.append(f"批號狀態為 {status}，不可作業")

    try:
        _route, _step, operation, device = await lot_service._context(db, lot)
    except MESError as exc:
        return {
            "lot_id": lot["lot_id"], "status": status,
            "blockers": blockers + [exc.message], "warnings": warnings,
            "info": info, "actions": [], "operation": None,
        }

    op_code = operation["op_code"]
    info["op_code"] = op_code
    info["op_name"] = operation["name"]
    info["requires_equipment"] = operation["requires_equipment"]

    # 作業員站別資格
    if operation["requires_certification"] and not is_certified(user, op_code):
        blockers.append(f"作業員 {actor} 未取得 {op_code} 站別資格認證")

    # 設備
    if operation["requires_equipment"]:
        if not eq_id:
            blockers.append(f"站別 {op_code} 必須指定設備，請先掃機台條碼")
        else:
            eq = await fetch_one(db, f"SELECT * FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id)
            if eq is None:
                blockers.append(f"設備不存在：{eq_id}")
            elif not eq["active"]:
                blockers.append(f"設備 {eq_id} 已停用")
            elif op_code not in (eq["op_codes"] or []):
                blockers.append(f"設備 {eq_id} 不具備 {op_code} 站別能力")
            elif eq["current_lot_id"] and eq["current_lot_id"] != lot["lot_id"]:
                blockers.append(f"設備 {eq_id} 正在加工批號 {eq['current_lot_id']}")
            elif eq["current_state"] not in RUNNABLE_STATES:
                blockers.append(f"設備 {eq_id} 目前狀態為 {eq['current_state']}，不可投料")

    # Q-Time
    now = utcnow()
    since = ensure_aware(lot["last_track_out_at"] or lot["created_at"])
    waiting_min = (now - since).total_seconds() / 60
    limit_min = int(operation["max_queue_minutes"] or 0)
    info["waiting_minutes"] = round(waiting_min, 1)
    if limit_min:
        remaining = round(limit_min - waiting_min, 1)
        info["qtime_limit_min"] = limit_min
        info["qtime_remaining_min"] = remaining
        waived = lot["qtime_waived_seq"] == lot["current_seq"]
        if remaining < 0 and not waived:
            if settings.auto_hold_on_qtime_violation:
                blockers.append(
                    f"Q-Time 已逾時 {abs(remaining):.0f} 分（上限 {limit_min} 分），"
                    f"進站會被自動扣留，請先找品保"
                )
            else:
                warnings.append(f"Q-Time 已逾時 {abs(remaining):.0f} 分")
        elif remaining < 0 and waived:
            warnings.append("Q-Time 逾時但品保已特採，本站可進站")
        elif remaining < QTIME_WARN_MINUTES:
            warnings.append(f"Q-Time 只剩 {remaining:.0f} 分，請優先處理")

    # e-SOP
    if operation.get("require_sop_ack"):
        doc = await sop_service.applicable_sop(db, op_code, lot["device_id"])
        if doc is None:
            blockers.append(f"站別 {op_code} 尚未發行 SOP")
        else:
            info["sop"] = {"sop_code": doc["sop_code"], "version": doc["version"],
                           "title": doc["title"], "require_ack": doc["require_ack"]}
            if doc["require_ack"]:
                ack = await fetch_one(
                    db,
                    "SELECT 1 AS ok FROM sop_acknowledgements "
                    "WHERE sop_code = $1 AND version = $2 AND username = $3",
                    doc["sop_code"], doc["version"], actor,
                )
                if ack is None:
                    blockers.append(
                        f"尚未確認作業指導書 {doc['sop_code']} v{doc['version']}（{doc['title']}）"
                    )
                    info["sop_ack_needed"] = True

    # 配方
    if operation.get("require_recipe_check") and eq_id:
        approved = await recipe_service.approved_recipes(db, op_code, lot["device_id"])
        loaded = await recipe_service.loaded_recipe(db, eq_id)
        current = (loaded or {}).get("ppid", "") or ""
        info["recipe"] = {
            "approved": [r["ppid"] for r in approved],
            "loaded": current,
        }
        if not approved:
            blockers.append(f"{op_code} 站與料號 {lot['device_id']} 尚無核可配方")
        elif not current:
            blockers.append(f"設備 {eq_id} 未回報載入的配方")
        elif current not in {r["ppid"] for r in approved}:
            blockers.append(f"機台配方 {current} 未核可（應為 {', '.join(r['ppid'] for r in approved)}）")

    # 抽檢
    if operation.get("require_sampling_decision"):
        plan = await sampling_service.applicable_plan(
            db, op_code, lot["device_id"], lot.get("customer_code", "")
        )
        if plan is not None:
            info["sampling_plan"] = plan["plan_code"]

    # 治具壽命
    if eq_id:
        tools = await fetch_all(
            db, f"SELECT * FROM {T_TOOLS} WHERE eq_id = $1 AND status <> 'SCRAPPED'", eq_id
        )
        alerts = [tool_service._decorate(dict(t)) for t in tools]
        expired = [t for t in alerts if t["status"] == "EXPIRED"]
        warn = [t for t in alerts if t["status"] != "EXPIRED" and t.get("needs_attention")]
        info["tools"] = alerts
        if expired:
            blockers.append(f"治具已到期需更換：{', '.join(t['tool_id'] for t in expired)}")
        for tool in warn:
            warnings.append(f"治具 {tool['tool_id']} 已用 {tool['usage_ratio']:.0%} 壽命")

    # 出站的預估產出量，讓終端機可以預填
    if status == LotStatus.RUNNING.value or (
        status == LotStatus.HOLD.value and lot["eq_id"]
    ):
        expected, out_unit = lot_service.compute_expected_output(
            int(lot["qty"]), operation, device, lot["unit_type"]
        )
        info["expected_output"] = expected
        info["output_unit"] = out_unit
        info["is_test_station"] = operation["is_test"]
        info["pass_bins"] = list(operation["pass_bins"] or [1])
        if lot["track_in_at"]:
            elapsed = (now - ensure_aware(lot["track_in_at"])).total_seconds()
            info["elapsed_sec"] = round(elapsed)
            info["standard_cycle_time_sec"] = operation["standard_cycle_time_sec"]

    actions = _actions_for(lot, blockers, info)
    return {
        "lot_id": lot["lot_id"],
        "status": status,
        "device_id": lot["device_id"],
        "qty": lot["qty"],
        "unit_type": lot["unit_type"],
        "operation": {"op_code": op_code, "name": operation["name"], "seq": lot["current_seq"]},
        "blockers": blockers,
        "warnings": warnings,
        "info": info,
        "actions": actions,
        "can_track_in": not blockers and lot["status"] == LotStatus.WAITING.value,
    }


def _actions_for(lot: dict, blockers: list[str], info: dict) -> list[dict]:
    """依批號狀態與檢查結果算出終端機該顯示哪些按鈕。"""
    status = lot["status"]
    actions: list[dict] = []

    if status == LotStatus.WAITING.value:
        actions.append({
            "action": "TRACK_IN", "label": "進站",
            "enabled": not blockers,
            "reason": blockers[0] if blockers else "",
        })
    if status == LotStatus.RUNNING.value:
        actions.append({"action": "TRACK_OUT", "label": "出站", "enabled": True, "reason": ""})
    if status == LotStatus.HOLD.value:
        # 加工途中被扣留的料還在機台上，必須讓它出得來，否則機台被卡死
        if lot["eq_id"]:
            actions.append({
                "action": "TRACK_OUT", "label": "出站（扣留中）",
                "enabled": True, "reason": "料仍在機台上，可出站但下一站需先放行",
            })
        actions.append({"action": "RELEASE", "label": "放行", "enabled": True,
                        "reason": "需品保權限"})
    if status in {LotStatus.WAITING.value, LotStatus.RUNNING.value}:
        actions.append({"action": "HOLD", "label": "扣留", "enabled": True, "reason": ""})
    if info.get("sop_ack_needed"):
        actions.insert(0, {"action": "ACK_SOP", "label": "閱讀並簽認 SOP",
                           "enabled": True, "reason": ""})
    return actions


# ── 站別工作台 ──────────────────────────────────────────────
async def station(db, eq_id: str, user: dict, queue_limit: int = 12) -> dict:
    """一次呼叫餵滿整個終端機畫面。

    現場網路常常不好，分成五六支 API 來回問會慢到讓人放棄。
    """
    eq = await fetch_one(db, f"SELECT * FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id)
    if eq is None:
        raise NotFoundError(f"找不到設備：{eq_id}")

    now = utcnow()
    op_codes = list(eq["op_codes"] or [])

    current: dict[str, Any] | None = None
    if eq["current_lot_id"]:
        lot = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1", eq["current_lot_id"])
        if lot is not None:
            current = await preflight(db, lot, user, eq_id)

    # 這台機能做的站別的待進站佇列
    queue: list[dict] = []
    waiting_total = 0
    for op_code in op_codes:
        result = await dispatch_service.dispatch_list(db, op_code, limit=queue_limit)
        queue.extend(result["items"])
        waiting_total += result["total"]
    queue.sort(key=lambda r: (r["urgency"] != "CRITICAL", r["urgency"] != "URGENT", r["priority"]))
    # 只顯示前幾筆，但要誠實說出總共有幾批在等 ——
    # 螢幕上寫「12」而實際有 40 批，會讓現場低估自己的落後程度
    queue = queue[:queue_limit]

    loaded = await recipe_service.loaded_recipe(db, eq_id)
    tools = [
        tool_service._decorate(dict(t))
        for t in await fetch_all(
            db, f"SELECT * FROM {T_TOOLS} WHERE eq_id = $1 AND status <> 'SCRAPPED' ORDER BY tool_id",
            eq_id,
        )
    ]
    pending_sops = await sop_service.pending_for_user(db, user["username"])

    return {
        "generated_at": now,
        "equipment": {
            "eq_id": eq["eq_id"], "name": eq["name"], "model": eq["model"], "area": eq["area"],
            "op_codes": op_codes, "current_state": eq["current_state"],
            "state_since": eq["state_since"], "current_lot_id": eq["current_lot_id"],
            "runnable": eq["current_state"] in RUNNABLE_STATES and eq["active"],
        },
        "current_lot": current,
        "queue": queue,
        "queue_shown": len(queue),
        "queue_total": waiting_total,
        "queue_truncated": waiting_total > len(queue),
        "loaded_recipe": loaded,
        "tools": tools,
        "tools_need_change": [t["tool_id"] for t in tools if t.get("needs_attention")],
        "pending_sops": [
            {"sop_code": s["sop_code"], "version": s["version"], "title": s["title"],
             "op_code": s["op_code"]}
            for s in pending_sops if s["op_code"] in op_codes
        ],
        "operator": {
            "username": user["username"],
            "full_name": user.get("full_name") or user["username"],
            "roles": list(user.get("roles") or []),
            "certifications": list(user.get("certifications") or []),
        },
    }


async def stations(db, area: str | None = None) -> list[dict]:
    """終端機開機時選機台用的清單。"""
    return await fetch_all(
        db,
        f"""
        SELECT eq_id, name, model, area, op_codes, current_state, current_lot_id
        FROM {T_EQUIPMENTS}
        WHERE active AND ($1::text IS NULL OR area = $1)
        ORDER BY area, eq_id
        """,
        area,
    )
