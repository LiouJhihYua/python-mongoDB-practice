"""批號（Lot）生產執行核心 —— MES 的心臟。

涵蓋開批、Track-In / Track-Out、單位換算、Q-Time 管制、拆批、併批、
扣留／放行、報廢與重工。

換到 PostgreSQL 之後，每個會動到多張表的動作都包在資料庫交易裡，
並對批號列加上 ``FOR UPDATE`` 列鎖 —— 兩個作業員同時對同一批進站時，
後到的那個會等待並看到最新狀態，而不是兩邊都成功。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.config import settings
from app.database import (
    T_DEFECT_CODES,
    T_DEFECT_RECORDS,
    T_DEVICES,
    T_HOLDS,
    T_LOT_HISTORY,
    T_LOTS,
    T_OPERATIONS,
    T_SHIPMENTS,
    T_WAFERS,
    T_WORK_ORDERS,
    fetch_all,
    fetch_one,
    next_sequence,
)
from app.errors import NotFoundError, PermissionError_, StateError, ValidationError
from app.models.base import ensure_aware, plain_values, shift_of, to_local, utcnow
from app.models.enums import (
    ACTIVE_LOT_STATUSES,
    EquipmentState,
    HoldReason,
    HoldStatus,
    LotAction,
    LotStatus,
    RUNNABLE_EQUIPMENT_STATES,
    UnitTransform,
    UnitType,
    WorkOrderStatus,
)
from app.services import (
    equipment_service,
    master_service,
    material_service,
    sop_service,
    tool_service,
)
from app.services.user_service import is_certified

RUNNABLE_STATES = [s.value for s in RUNNABLE_EQUIPMENT_STATES]
ACTIVE_STATUS_VALUES = [s.value for s in ACTIVE_LOT_STATUSES]


# ── 基本查詢 ────────────────────────────────────────────────
async def _gen_lot_id(db) -> str:
    ym = f"{to_local(utcnow()):%y%m}"
    return f"L{ym}{await next_sequence(db, f'lot:{ym}'):05d}"


async def get_lot(db, lot_id: str, raw: bool = False) -> dict:
    row = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1", lot_id)
    if row is None:
        raise NotFoundError(f"找不到批號：{lot_id}")
    return row


async def _lock_lot(db, lot_id: str) -> dict:
    """取得批號並鎖住該列，避免同一批被兩個人同時處理。"""
    row = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1 FOR UPDATE", lot_id)
    if row is None:
        raise NotFoundError(f"找不到批號：{lot_id}")
    return row


async def list_lots(db, query: dict) -> dict:
    where = """
        WHERE ($1::text IS NULL OR status = $1)
          AND ($2::text IS NULL OR device_id = $2)
          AND ($3::text IS NULL OR wo_no = $3)
          AND ($4::text IS NULL OR customer_code = $4)
          AND ($5::text IS NULL OR current_op = $5)
    """
    status = LotStatus.HOLD.value if query.get("on_hold") is True else query.get("status")
    args = (status, query.get("device_id"), query.get("wo_no"),
            query.get("customer_code"), query.get("op_code"))
    skip, limit = query.get("skip", 0), query.get("limit", 50)

    total = await db.fetchval(f"SELECT count(*) FROM {T_LOTS} {where}", *args)
    items = await fetch_all(
        db,
        f"SELECT * FROM {T_LOTS} {where} ORDER BY priority ASC, created_at ASC OFFSET $6 LIMIT $7",
        *args, skip, limit,
    )
    return {"items": items, "total": total, "skip": skip, "limit": limit}


async def lot_history(db, lot_id: str, limit: int = 200) -> list[dict]:
    await get_lot(db, lot_id)
    return await fetch_all(
        db,
        f"SELECT * FROM {T_LOT_HISTORY} WHERE lot_id = $1 ORDER BY timestamp ASC, id ASC LIMIT $2",
        lot_id, limit,
    )


async def _context(db, lot: dict) -> tuple[dict, dict, dict, dict]:
    """取得批號目前所在的 流程 / 站序 / 站別 / 產品。"""
    route = await master_service.get_active_route(db, lot["route_code"], lot.get("route_version"))
    step = master_service.get_route_step(route, lot["current_seq"])
    if step is None:
        raise StateError(
            f"批號 {lot['lot_id']} 的站序 {lot['current_seq']} 不存在於流程 {route['route_code']}"
        )
    operation = await fetch_one(db, f"SELECT * FROM {T_OPERATIONS} WHERE op_code = $1", step["op_code"])
    if operation is None:
        raise NotFoundError(f"找不到站別：{step['op_code']}")
    if not operation["active"]:
        raise StateError(f"站別 {step['op_code']} 已停用，無法作業")
    device = await fetch_one(db, f"SELECT * FROM {T_DEVICES} WHERE device_id = $1", lot["device_id"])
    if device is None:
        raise NotFoundError(f"找不到產品料號：{lot['device_id']}")
    return route, step, operation, device


def compute_expected_output(qty_in: int, operation: dict, device: dict, current_unit: str) -> tuple[int, str]:
    """依站別的單位換算規則推算「應產出量」與產出單位。

    OSAT 的特性：同一批在切割前以「片」計、切割後以「顆」計，
    良率必須以換算後的應產出量為分母才有意義。
    """
    transform = operation.get("unit_transform", UnitTransform.NONE.value)
    declared_unit = operation.get("output_unit")

    if transform == UnitTransform.WAFER_TO_DIE.value:
        ratio = int(device.get("gross_die_per_wafer", 1) or 1)
        return qty_in * ratio, declared_unit or UnitType.DIE.value
    if transform == UnitTransform.DIE_TO_UNIT.value:
        return qty_in, declared_unit or UnitType.UNIT.value
    if transform == UnitTransform.STRIP_TO_UNIT.value:
        ratio = int(device.get("units_per_strip", 1) or 1)
        return qty_in * ratio, declared_unit or UnitType.UNIT.value
    if transform == UnitTransform.UNIT_TO_REEL.value:
        ratio = int(device.get("units_per_reel", 1) or 1)
        return math.ceil(qty_in / ratio), declared_unit or UnitType.REEL.value
    return qty_in, declared_unit or current_unit


HISTORY_COLUMNS = (
    "lot_id", "wo_no", "device_id", "customer_code", "route_code", "seq", "op_code",
    "action", "operator", "timestamp", "shift", "eq_id", "qty_in", "qty_expected",
    "qty_good", "qty_reject", "unit_in", "unit_out", "process_sec", "queue_sec",
    "qtime_limit_min", "qtime_violation", "step_yield", "standard_cycle_time_sec",
    "defects", "bin_map", "materials", "remark",
)


async def _write_history(db, lot: dict, action: LotAction, actor: str, **extra) -> dict:
    now = extra.pop("timestamp", None) or utcnow()
    doc: dict[str, Any] = {
        "lot_id": lot["lot_id"],
        "wo_no": lot.get("wo_no"),
        "device_id": lot.get("device_id"),
        "customer_code": lot.get("customer_code"),
        "route_code": lot.get("route_code"),
        "seq": lot.get("current_seq"),
        "op_code": lot.get("current_op"),
        "action": action.value,
        "operator": actor,
        "timestamp": now,
        "shift": shift_of(now),
        "eq_id": None,
        "qty_in": 0,
        "qty_expected": 0,
        "qty_good": 0,
        "qty_reject": 0,
        "unit_in": None,
        "unit_out": None,
        "process_sec": 0.0,
        "queue_sec": 0.0,
        "qtime_limit_min": 0,
        "qtime_violation": False,
        "step_yield": None,
        "standard_cycle_time_sec": 0,
        "defects": [],
        "bin_map": None,
        "materials": [],
        "remark": "",
    }
    doc.update({k: v for k, v in extra.items() if k in HISTORY_COLUMNS})
    placeholders = ", ".join(f"${i}" for i in range(1, len(HISTORY_COLUMNS) + 1))
    return await fetch_one(
        db,
        f"INSERT INTO {T_LOT_HISTORY} ({', '.join(HISTORY_COLUMNS)}) "
        f"VALUES ({placeholders}) RETURNING *",
        *(plain_values(doc[c]) for c in HISTORY_COLUMNS),
    )


async def find_open_hold(db, lot_id: str) -> dict | None:
    return await fetch_one(
        db, f"SELECT * FROM {T_HOLDS} WHERE lot_id = $1 AND status = $2", lot_id, HoldStatus.OPEN.value
    )


async def _set_lot(db, lot_id: str, **fields) -> dict:
    """更新批號欄位（值為 None 時仍會寫入，用於清空 eq_id 等）。"""
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(fields, start=1))
    return await fetch_one(
        db,
        f"UPDATE {T_LOTS} SET {assignments} WHERE lot_id = ${len(fields) + 1} RETURNING *",
        *(plain_values(v) for v in fields.values()), lot_id,
    )


# ── 開批 ────────────────────────────────────────────────────
async def create_lot(db, payload: dict, actor: str) -> dict:
    async with db.transaction():
        wo = await fetch_one(
            db, f"SELECT * FROM {T_WORK_ORDERS} WHERE wo_no = $1 FOR UPDATE", payload["wo_no"]
        )
        if wo is None:
            raise ValidationError(f"工單不存在：{payload['wo_no']}")
        if wo["status"] not in {WorkOrderStatus.RELEASED.value, WorkOrderStatus.IN_PROGRESS.value}:
            raise StateError(f"工單 {wo['wo_no']} 狀態為 {wo['status']}，需先下達（RELEASED）才能開批")

        qty = int(payload["qty"])
        remaining = int(wo["plan_qty"]) - int(wo["released_qty"])
        if qty > remaining:
            raise ValidationError(f"投料量 {qty} 超過工單剩餘可投料量 {remaining}")

        route = await master_service.get_active_route(db, wo["route_code"])
        step = master_service.first_step(route)

        wafer_ids = payload.get("wafer_ids") or []
        if wafer_ids:
            await _reserve_wafers(db, wafer_ids, wo["device_id"], qty)

        now = utcnow()
        lot_id = await _gen_lot_id(db)
        lot = await fetch_one(
            db,
            f"""
            INSERT INTO {T_LOTS}
                (lot_id, wo_no, device_id, customer_code, package_code, route_code, route_version,
                 current_seq, current_op, status, qty, initial_qty, unit_type, carrier_id, priority,
                 wafer_ids, remark, created_at, created_by, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11, $12, $13, $14, $15, $16, $17, $18, $17)
            RETURNING *
            """,
            lot_id, wo["wo_no"], wo["device_id"], wo["customer_code"], wo["package_code"],
            route["route_code"], route["version"], step["seq"], step["op_code"],
            LotStatus.WAITING.value, qty, wo["unit_type"], payload.get("carrier_id", ""),
            payload.get("priority") or wo["priority"], wafer_ids, payload.get("remark", ""),
            now, actor,
        )
        if wafer_ids:
            await db.execute(
                f"""
                UPDATE {T_WAFERS} SET assembly_lot_id = $1, consumed = TRUE, consumed_at = $2
                WHERE wafer_id = ANY($3::text[])
                """,
                lot_id, now, wafer_ids,
            )
        await db.execute(
            f"""
            UPDATE {T_WORK_ORDERS}
            SET released_qty = released_qty + $1, lot_count = lot_count + 1,
                status = $2, updated_at = now()
            WHERE wo_no = $3
            """,
            qty, WorkOrderStatus.IN_PROGRESS.value, wo["wo_no"],
        )
        await _write_history(
            db, lot, LotAction.CREATE, actor,
            qty_in=qty, qty_good=qty, unit_in=lot["unit_type"], unit_out=lot["unit_type"],
            remark=f"自工單 {wo['wo_no']} 投料 {qty} {lot['unit_type']}",
        )
        return lot


async def _reserve_wafers(db, wafer_ids: list[str], device_id: str, qty: int) -> None:
    if len(set(wafer_ids)) != len(wafer_ids):
        raise ValidationError("投入晶圓 ID 重複")
    if len(wafer_ids) != qty:
        raise ValidationError(f"投入晶圓數 {len(wafer_ids)} 與批量 {qty} 不符")

    rows = await fetch_all(
        db,
        f"SELECT wafer_id, device_id, consumed FROM {T_WAFERS} WHERE wafer_id = ANY($1::text[]) FOR UPDATE",
        wafer_ids,
    )
    found = {r["wafer_id"] for r in rows}
    missing = set(wafer_ids) - found
    if missing:
        raise ValidationError(f"晶圓不存在：{', '.join(sorted(missing))}")
    used = sorted(r["wafer_id"] for r in rows if r["consumed"])
    if used:
        raise ValidationError(f"晶圓已被投入其他批號：{', '.join(used)}")
    wrong = sorted(r["wafer_id"] for r in rows if r["device_id"] != device_id)
    if wrong:
        raise ValidationError(f"晶圓料號與工單不符：{', '.join(wrong)}")


# ── 進站 ────────────────────────────────────────────────────
async def track_in(db, payload: dict, user: dict) -> dict:
    """進站。

    Q-Time 逾時的自動扣留必須先隨交易提交、再把錯誤拋出去，
    否則 raise 會把整筆扣留紀錄一起回滾掉。
    """
    result, pending_error = await _track_in_tx(db, payload, user)
    if pending_error is not None:
        raise pending_error
    return result


async def _track_in_tx(db, payload: dict, user: dict) -> tuple[dict | None, Exception | None]:
    actor = user["username"]
    async with db.transaction():
        lot = await _lock_lot(db, payload["lot_id"])
        status = lot["status"]
        if status == LotStatus.HOLD.value:
            hold = await find_open_hold(db, lot["lot_id"])
            raise StateError(f"批號 {lot['lot_id']} 扣留中（{(hold or {}).get('reason', '')}），請先放行")
        if status == LotStatus.RUNNING.value:
            raise StateError(f"批號 {lot['lot_id']} 已在 {lot['eq_id']} 加工中，請先出站")
        if status != LotStatus.WAITING.value:
            raise StateError(f"批號 {lot['lot_id']} 狀態為 {status}，不可進站")

        route, step, operation, device = await _context(db, lot)
        op_code = operation["op_code"]

        if operation["requires_certification"] and not is_certified(user, op_code):
            raise PermissionError_(f"作業員 {actor} 未取得 {op_code} 站別資格認證")
        if operation.get("require_sop_ack"):
            # 站別要求先讀過 e-SOP：沒簽認新版就進不了站
            await sop_service.ensure_acknowledged(db, op_code, lot["device_id"], actor)

        eq = None
        if operation["requires_equipment"]:
            eq_id = payload.get("eq_id")
            if not eq_id:
                raise ValidationError(f"站別 {op_code} 必須指定設備")
            eq = await equipment_service.lock_equipment(db, eq_id)
            if not eq["active"]:
                raise StateError(f"設備 {eq_id} 已停用")
            if op_code not in (eq["op_codes"] or []):
                raise ValidationError(f"設備 {eq_id} 不具備 {op_code} 站別能力")
            if eq["current_lot_id"] and eq["current_lot_id"] != lot["lot_id"]:
                raise StateError(f"設備 {eq_id} 正在加工批號 {eq['current_lot_id']}")
            if eq["current_state"] not in RUNNABLE_STATES:
                raise StateError(f"設備 {eq_id} 目前狀態為 {eq['current_state']}，不可投料")

        now = utcnow()
        since = ensure_aware(lot["last_track_out_at"] or lot["created_at"])
        queue_sec = max(0.0, (now - since).total_seconds())
        limit_min = int(operation["max_queue_minutes"] or 0)
        violated = limit_min > 0 and queue_sec > limit_min * 60
        #: 品保放行 Q-Time 扣留後給予特採，避免放行→再次逾時→再扣留的死循環
        waived = violated and lot["qtime_waived_seq"] == lot["current_seq"]

        if violated and settings.auto_hold_on_qtime_violation and not waived:
            # 自動扣留是刻意留下的紀錄，必須跟著交易一起提交；
            # 因此先寫入，等交易結束後才把錯誤拋出去，否則副作用會被回滾。
            await db.execute(
                f"UPDATE {T_LOTS} SET qtime_violations = qtime_violations + 1 WHERE lot_id = $1",
                lot["lot_id"],
            )
            await _write_history(
                db, lot, LotAction.HOLD, actor,
                eq_id=payload.get("eq_id"), queue_sec=round(queue_sec, 1),
                qtime_limit_min=limit_min, qtime_violation=True,
                remark=f"Q-Time 逾時：等待 {queue_sec / 60:.1f} 分，上限 {limit_min} 分",
            )
            await apply_hold(
                db, lot, HoldReason.QTIME, actor,
                f"Q-Time 逾時自動扣留：等待 {queue_sec / 60:.1f} 分 > 上限 {limit_min} 分",
            )
            return None, StateError(
                f"批號 {lot['lot_id']} 於 {op_code} 站 Q-Time 逾時"
                f"（等待 {queue_sec / 60:.1f} 分 / 上限 {limit_min} 分），已自動扣留待品保判定"
            )

        result = await _set_lot(
            db, lot["lot_id"],
            status=LotStatus.RUNNING.value,
            eq_id=payload.get("eq_id") or None,
            operator=actor,
            track_in_at=now,
            qtime_waived_seq=None,  # 特採僅對本站有效，用掉即失效
            qtime_violations=lot["qtime_violations"] + (1 if violated else 0),
        )
        if eq is not None:
            await equipment_service.set_state(
                db, eq["eq_id"], EquipmentState.PRODUCTIVE, actor,
                reason_code="TRACK_IN", remark=f"批號 {lot['lot_id']}", lot_id=lot["lot_id"],
            )
        await _write_history(
            db, lot, LotAction.TRACK_IN, actor,
            eq_id=payload.get("eq_id"), qty_in=lot["qty"], unit_in=lot["unit_type"],
            queue_sec=round(queue_sec, 1), qtime_limit_min=limit_min, qtime_violation=violated,
            timestamp=now, remark=payload.get("remark", ""),
        )
        return result, None


# ── 出站 ────────────────────────────────────────────────────
async def track_out(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    async with db.transaction():
        lot = await _lock_lot(db, payload["lot_id"])
        # 加工途中被扣留（例如 SPC 判異）的批號仍須能出站，否則料會卡死在機台上。
        # 扣留擋的是「下一次進站」，不是「這一次出站」。
        held_while_running = None
        if lot["status"] == LotStatus.HOLD.value:
            held_while_running = await find_open_hold(db, lot["lot_id"])
            if not held_while_running or held_while_running["status_before_hold"] != LotStatus.RUNNING.value:
                raise StateError(f"批號 {lot['lot_id']} 扣留中且不在機台上，請先放行")
        elif lot["status"] != LotStatus.RUNNING.value:
            raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，需先 Track-In 才能出站")

        route, step, operation, device = await _context(db, lot)
        op_code = operation["op_code"]
        qty_in = int(lot["qty"])
        expected, out_unit = compute_expected_output(qty_in, operation, device, lot["unit_type"])

        bin_map = payload.get("bin_map")
        if bin_map:
            if not operation["is_test"]:
                raise ValidationError(f"站別 {op_code} 非測試站，不可使用 Bin 分佈出站")
            total = sum(int(v) for v in bin_map.values())
            if total != expected:
                raise ValidationError(
                    f"Bin 合計 {total} 與應產出量 {expected} 不符（進站 {qty_in} {lot['unit_type']}）"
                )
            pass_bins = {str(b) for b in (operation["pass_bins"] or [1])}
            good = sum(int(v) for k, v in bin_map.items() if str(k) in pass_bins)
            reject = total - good
        else:
            good = int(payload.get("good_qty") or 0)
            reject = int(payload.get("reject_qty") or 0)
            if good + reject != expected:
                raise ValidationError(
                    f"良品 {good} + 不良 {reject} = {good + reject}，應等於應產出量 {expected}"
                    f"（進站 {qty_in} {lot['unit_type']} → {out_unit}）"
                )

        defects = [dict(d) for d in (payload.get("defects") or [])]
        if defects:
            defect_total = sum(int(d["qty"]) for d in defects)
            if defect_total != reject:
                raise ValidationError(f"不良明細合計 {defect_total} 與不良數 {reject} 不符")
            await _validate_defect_codes(db, defects, op_code)
        elif reject > 0 and not bin_map:
            raise ValidationError("有不良數時必須填寫不良代碼明細（defects）")

        materials_used = []
        if payload.get("materials"):
            materials_used = await material_service.consume(db, payload["materials"], lot, op_code, actor)

        now = utcnow()
        track_in_at = ensure_aware(lot["track_in_at"]) if lot["track_in_at"] else now
        process_sec = max(0.0, (now - track_in_at).total_seconds())
        step_yield = round(good / expected, 6) if expected else 0.0
        next_step = master_service.next_step(route, lot["current_seq"])

        update: dict[str, Any] = {
            "qty": good,
            "unit_type": out_unit,
            "eq_id": None,
            "track_in_at": None,
            "last_track_out_at": now,
            "scrap_qty": int(lot["scrap_qty"]) + reject,
            "completed_at": lot["completed_at"],
        }
        if good == 0:
            update["status"] = LotStatus.SCRAPPED.value
            update["completed_at"] = now
        elif next_step is not None:
            update["status"] = LotStatus.WAITING.value
            update["current_seq"] = next_step["seq"]
            update["current_op"] = next_step["op_code"]
        else:
            update["status"] = LotStatus.COMPLETED.value
            update["completed_at"] = now

        if held_while_running is not None and update["status"] != LotStatus.SCRAPPED.value:
            # 料已離開機台，扣留狀態保留到下一站；放行後回到待進站
            update["status"] = LotStatus.HOLD.value
            update["completed_at"] = lot["completed_at"]
            await db.execute(
                f"UPDATE {T_HOLDS} SET status_before_hold = $1 WHERE id = $2",
                LotStatus.WAITING.value, held_while_running["id"],
            )

        result = await _set_lot(db, lot["lot_id"], **update)

        tool_alerts: list[dict] = []
        if lot["eq_id"]:
            await equipment_service.set_state(
                db, lot["eq_id"], EquipmentState.STANDBY, actor,
                reason_code="TRACK_OUT", remark=f"批號 {lot['lot_id']} 出站", lot_id=None,
            )
            # 治具壽命以加工顆數累計；到期會把設備轉為計畫停機待換刀
            tool_alerts = await tool_service.consume(db, lot["eq_id"], expected, lot["lot_id"], actor)

        history = await _write_history(
            db, lot, LotAction.TRACK_OUT, actor,
            eq_id=lot["eq_id"], qty_in=qty_in, qty_expected=expected,
            qty_good=good, qty_reject=reject, unit_in=lot["unit_type"], unit_out=out_unit,
            defects=defects, bin_map=bin_map, materials=materials_used,
            process_sec=round(process_sec, 1), step_yield=step_yield,
            standard_cycle_time_sec=operation["standard_cycle_time_sec"],
            timestamp=now, remark=payload.get("remark", ""),
        )
        if defects:
            await _record_defects(db, lot, operation, defects, actor, now)

        result["last_step"] = {
            "op_code": op_code,
            "qty_in": qty_in,
            "qty_expected": expected,
            "qty_good": good,
            "qty_reject": reject,
            "step_yield": step_yield,
            "process_sec": history["process_sec"],
        }
        result["tool_alerts"] = tool_alerts
        return result


async def _validate_defect_codes(db, defects: list[dict], op_code: str) -> None:
    codes = [d["defect_code"] for d in defects]
    rows = await fetch_all(
        db, f"SELECT * FROM {T_DEFECT_CODES} WHERE code = ANY($1::text[])", codes
    )
    found = {r["code"]: r for r in rows}
    missing = set(codes) - set(found)
    if missing:
        raise ValidationError(f"不良代碼不存在：{', '.join(sorted(missing))}")
    for code, doc in found.items():
        applicable = doc["op_codes"] or []
        if applicable and op_code not in applicable:
            raise ValidationError(f"不良代碼 {code} 不適用於站別 {op_code}")


async def _record_defects(db, lot: dict, operation: dict, defects: list[dict], actor: str, now: datetime) -> None:
    rows = await fetch_all(
        db, f"SELECT * FROM {T_DEFECT_CODES} WHERE code = ANY($1::text[])",
        [d["defect_code"] for d in defects],
    )
    meta = {r["code"]: r for r in rows}
    await db.executemany(
        f"""
        INSERT INTO {T_DEFECT_RECORDS}
            (lot_id, wo_no, device_id, customer_code, op_code, seq, eq_id, defect_code,
             defect_name, category, disposition, qty, remark, operator, timestamp, shift)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
        """,
        [
            (
                lot["lot_id"], lot["wo_no"], lot["device_id"], lot["customer_code"],
                operation["op_code"], lot["current_seq"], lot["eq_id"], d["defect_code"],
                meta.get(d["defect_code"], {}).get("name", ""),
                meta.get(d["defect_code"], {}).get("category", ""),
                meta.get(d["defect_code"], {}).get("default_disposition", "SCRAP"),
                int(d["qty"]), d.get("remark", ""), actor, now, shift_of(now),
            )
            for d in defects
        ],
    )


# ── 扣留 / 放行 ─────────────────────────────────────────────
async def apply_hold(db, lot: dict, reason: HoldReason | str, actor: str, remark: str) -> dict:
    now = utcnow()
    await db.execute(
        f"""
        INSERT INTO {T_HOLDS}
            (lot_id, wo_no, device_id, op_code, seq, reason, remark, status,
             held_by, held_at, status_before_hold)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        lot["lot_id"], lot.get("wo_no"), lot.get("device_id"), lot.get("current_op"),
        lot.get("current_seq"), str(reason), remark, HoldStatus.OPEN.value,
        actor, now, lot["status"],
    )
    return await _set_lot(db, lot["lot_id"], status=LotStatus.HOLD.value)


async def hold_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    async with db.transaction():
        lot = await _lock_lot(db, payload["lot_id"])
        if lot["status"] not in {LotStatus.WAITING.value, LotStatus.RUNNING.value}:
            raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，不可扣留")
        if await find_open_hold(db, lot["lot_id"]):
            raise StateError(f"批號 {lot['lot_id']} 已在扣留中")
        result = await apply_hold(
            db, lot, payload.get("reason", HoldReason.QUALITY), actor, payload.get("remark", "")
        )
        await _write_history(
            db, lot, LotAction.HOLD, actor,
            remark=f"{payload.get('reason')} / {payload.get('remark', '')}",
        )
        return result


async def release_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    async with db.transaction():
        lot = await _lock_lot(db, payload["lot_id"])
        hold = await find_open_hold(db, lot["lot_id"])
        if hold is None:
            raise StateError(f"批號 {lot['lot_id']} 目前沒有扣留紀錄")

        await db.execute(
            f"""
            UPDATE {T_HOLDS}
            SET status = $1, released_by = $2, released_at = $3, release_remark = $4
            WHERE id = $5
            """,
            HoldStatus.RELEASED.value, actor, utcnow(), payload.get("remark", ""), hold["id"],
        )
        restored = hold["status_before_hold"] or LotStatus.WAITING.value
        if restored not in {LotStatus.WAITING.value, LotStatus.RUNNING.value}:
            restored = LotStatus.WAITING.value

        fields: dict[str, Any] = {"status": restored}
        if hold["reason"] == HoldReason.QTIME.value:
            # 品保已判定可用，對目前站別給予一次性特採
            fields["qtime_waived_seq"] = lot["current_seq"]
        result = await _set_lot(db, lot["lot_id"], **fields)
        await _write_history(db, lot, LotAction.RELEASE, actor, remark=payload.get("remark", ""))
        return result


# ── 拆批 / 併批 ─────────────────────────────────────────────
CHILD_COLUMNS = (
    "lot_id", "wo_no", "device_id", "customer_code", "package_code", "route_code",
    "route_version", "current_seq", "current_op", "status", "qty", "initial_qty",
    "unit_type", "carrier_id", "priority", "parent_lot_id", "wafer_ids",
    "last_track_out_at", "remark", "created_at", "created_by", "updated_at",
)


async def split_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    async with db.transaction():
        lot = await _lock_lot(db, payload["lot_id"])
        if lot["status"] != LotStatus.WAITING.value:
            raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，僅待進站（WAITING）可拆批")
        quantities = [int(q) for q in payload["quantities"]]
        if sum(quantities) != int(lot["qty"]):
            raise ValidationError(f"子批合計 {sum(quantities)} 與母批現有量 {lot['qty']} 不符")

        now = utcnow()
        wafer_ids = list(lot["wafer_ids"] or [])
        can_slice = lot["unit_type"] == UnitType.WAFER.value and len(wafer_ids) == int(lot["qty"])

        children, cursor_pos = [], 0
        for idx, qty in enumerate(quantities, start=1):
            child_id = f"{lot['lot_id']}.{idx}"
            if can_slice:
                child_wafers = wafer_ids[cursor_pos: cursor_pos + qty]
                cursor_pos += qty
            else:
                child_wafers = list(wafer_ids)  # 追溯用：子批同樣源自這些晶圓
            values = {
                **{c: lot[c] for c in CHILD_COLUMNS if c in lot},
                "lot_id": child_id,
                "qty": qty,
                "initial_qty": qty,
                "status": LotStatus.WAITING.value,
                "parent_lot_id": lot["lot_id"],
                "wafer_ids": child_wafers,
                "remark": payload.get("reason", ""),
                "created_at": now,
                "created_by": actor,
                "updated_at": now,
            }
            placeholders = ", ".join(f"${i}" for i in range(1, len(CHILD_COLUMNS) + 1))
            child = await fetch_one(
                db,
                f"INSERT INTO {T_LOTS} ({', '.join(CHILD_COLUMNS)}) "
                f"VALUES ({placeholders}) RETURNING *",
                *(values[c] for c in CHILD_COLUMNS),
            )
            await _write_history(
                db, child, LotAction.CREATE, actor,
                qty_in=qty, qty_good=qty, unit_in=child["unit_type"], unit_out=child["unit_type"],
                remark=f"由 {lot['lot_id']} 拆批產生",
            )
            children.append(child)

        await _set_lot(
            db, lot["lot_id"],
            status=LotStatus.SPLIT.value,
            child_lot_ids=[c["lot_id"] for c in children],
            qty=0,
            completed_at=now,
        )
        await _write_history(
            db, lot, LotAction.SPLIT, actor, qty_in=int(lot["qty"]),
            remark=f"拆為 {', '.join(c['lot_id'] for c in children)}；原因：{payload.get('reason', '')}",
        )
        return {"parent_lot_id": lot["lot_id"], "children": children}


async def merge_lots(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot_ids = list(dict.fromkeys(payload["lot_ids"]))
    if len(lot_ids) < 2:
        raise ValidationError("併批至少需要兩個不同批號")

    async with db.transaction():
        lots = await fetch_all(
            db,
            f"SELECT * FROM {T_LOTS} WHERE lot_id = ANY($1::text[]) ORDER BY lot_id FOR UPDATE",
            lot_ids,
        )
        missing = set(lot_ids) - {l["lot_id"] for l in lots}
        if missing:
            raise NotFoundError(f"找不到批號：{', '.join(sorted(missing))}")

        keys = {(l["device_id"], l["route_code"], l["current_seq"], l["unit_type"]) for l in lots}
        if len(keys) != 1:
            raise ValidationError("併批要求相同產品料號、流程、站序與計量單位")
        bad = sorted(l["lot_id"] for l in lots if l["status"] != LotStatus.WAITING.value)
        if bad:
            raise StateError(f"以下批號非待進站狀態，不可併批：{', '.join(bad)}")

        now = utcnow()
        base = lots[0]
        new_id = await _gen_lot_id(db)
        last_out = [ensure_aware(l["last_track_out_at"]) for l in lots if l["last_track_out_at"]]
        merged = await fetch_one(
            db,
            f"""
            INSERT INTO {T_LOTS}
                (lot_id, wo_no, device_id, customer_code, package_code, route_code, route_version,
                 current_seq, current_op, status, qty, initial_qty, unit_type, scrap_qty,
                 carrier_id, priority, merged_from, wafer_ids, last_track_out_at,
                 qtime_violations, rework_count, remark, created_at, created_by, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                    $17, $18, $19, $20, $21, $22, $23, $24, $23)
            RETURNING *
            """,
            new_id, base["wo_no"], base["device_id"], base["customer_code"], base["package_code"],
            base["route_code"], base["route_version"], base["current_seq"], base["current_op"],
            LotStatus.WAITING.value,
            sum(int(l["qty"]) for l in lots), sum(int(l["initial_qty"]) for l in lots),
            base["unit_type"], sum(int(l["scrap_qty"]) for l in lots),
            payload.get("carrier_id", ""), min(int(l["priority"]) for l in lots),
            sorted(lot_ids), sorted({w for l in lots for w in (l["wafer_ids"] or [])}),
            max(last_out) if last_out else None,
            sum(int(l["qtime_violations"]) for l in lots),
            max(int(l["rework_count"]) for l in lots),
            payload.get("reason", ""), now, actor,
        )
        await db.execute(
            f"""
            UPDATE {T_LOTS} SET status = $1, merged_into = $2, qty = 0,
                                completed_at = $3, updated_at = $3
            WHERE lot_id = ANY($4::text[])
            """,
            LotStatus.MERGED.value, new_id, now, lot_ids,
        )
        for src in lots:
            await _write_history(db, src, LotAction.MERGE, actor, remark=f"併入 {new_id}")
        await _write_history(
            db, merged, LotAction.CREATE, actor,
            qty_in=merged["qty"], qty_good=merged["qty"],
            unit_in=merged["unit_type"], unit_out=merged["unit_type"],
            remark=f"由 {', '.join(sorted(lot_ids))} 併批產生",
        )
        return merged


# ── 報廢 / 重工 / 出貨 ──────────────────────────────────────
async def scrap_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    async with db.transaction():
        lot = await _lock_lot(db, payload["lot_id"])
        if lot["status"] not in ACTIVE_STATUS_VALUES:
            raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，不可報廢")
        qty = int(payload["qty"])
        if qty > int(lot["qty"]):
            raise ValidationError(f"報廢量 {qty} 超過批號現有量 {lot['qty']}")

        _, _, operation, _ = await _context(db, lot)
        defects = [{"defect_code": payload["defect_code"], "qty": qty}]
        await _validate_defect_codes(db, defects, operation["op_code"])

        now = utcnow()
        remaining = int(lot["qty"]) - qty
        update: dict[str, Any] = {"qty": remaining, "scrap_qty": int(lot["scrap_qty"]) + qty}
        if remaining == 0:
            update.update(
                status=LotStatus.SCRAPPED.value, completed_at=now, eq_id=None, track_in_at=None
            )
        result = await _set_lot(db, lot["lot_id"], **update)
        await _record_defects(db, lot, operation, defects, actor, now)
        await _write_history(
            db, lot, LotAction.SCRAP, actor,
            qty_in=int(lot["qty"]), qty_good=remaining, qty_reject=qty, defects=defects,
            timestamp=now, remark=payload.get("remark", ""),
        )
        return result


async def rework_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    async with db.transaction():
        lot = await _lock_lot(db, payload["lot_id"])
        if lot["status"] not in {LotStatus.WAITING.value, LotStatus.HOLD.value}:
            raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，僅待進站或扣留中可重工")
        to_seq = int(payload["to_seq"])
        if to_seq >= int(lot["current_seq"]):
            raise ValidationError(f"重工站序 {to_seq} 必須小於目前站序 {lot['current_seq']}")

        route = await master_service.get_active_route(db, lot["route_code"], lot["route_version"])
        target = master_service.get_route_step(route, to_seq)
        if target is None:
            raise ValidationError(f"流程 {route['route_code']} 沒有站序 {to_seq}")
        operation = await fetch_one(
            db, f"SELECT * FROM {T_OPERATIONS} WHERE op_code = $1", target["op_code"]
        )
        if operation is not None and not operation["allow_rework"]:
            raise StateError(f"站別 {target['op_code']} 不允許重工")

        now = utcnow()
        from_seq, from_op = lot["current_seq"], lot["current_op"]
        result = await _set_lot(
            db, lot["lot_id"],
            current_seq=target["seq"],
            current_op=target["op_code"],
            status=LotStatus.WAITING.value,
            last_track_out_at=now,  # 重工重新起算 Q-Time
            rework_count=int(lot["rework_count"]) + 1,
        )
        await _write_history(
            db, lot, LotAction.REWORK, actor, qty_in=int(lot["qty"]),
            remark=(
                f"由 {from_seq}/{from_op} 退回 {target['seq']}/{target['op_code']}；"
                f"原因：{payload.get('reason', '')}"
            ),
        )
        return result


async def ship_lots(db, payload: dict, user: dict) -> dict:
    """出貨：只有全流程完工的批號可出貨，並產生出貨單供追溯。"""
    actor = user["username"]
    lot_ids = list(dict.fromkeys(payload["lot_ids"]))
    async with db.transaction():
        lots = await fetch_all(
            db, f"SELECT * FROM {T_LOTS} WHERE lot_id = ANY($1::text[]) FOR UPDATE", lot_ids
        )
        missing = set(lot_ids) - {l["lot_id"] for l in lots}
        if missing:
            raise NotFoundError(f"找不到批號：{', '.join(sorted(missing))}")
        not_done = sorted(l["lot_id"] for l in lots if l["status"] != LotStatus.COMPLETED.value)
        if not_done:
            raise StateError(f"以下批號尚未完工，不可出貨：{', '.join(not_done)}")
        wrong = sorted(l["lot_id"] for l in lots if l["customer_code"] != payload["customer_code"])
        if wrong:
            raise ValidationError(f"以下批號不屬於客戶 {payload['customer_code']}：{', '.join(wrong)}")

        now = utcnow()
        ym = f"{to_local(now):%y%m}"
        shipment_no = f"SH{ym}{await next_sequence(db, f'shipment:{ym}'):04d}"
        total_qty = sum(int(l["qty"]) for l in lots)
        await db.execute(
            f"""
            INSERT INTO {T_SHIPMENTS}
                (shipment_no, customer_code, customer_po, lot_ids, device_ids,
                 total_qty, remark, shipped_by, shipped_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            shipment_no, payload["customer_code"], payload.get("customer_po", ""),
            sorted(lot_ids), sorted({l["device_id"] for l in lots}),
            total_qty, payload.get("remark", ""), actor, now,
        )
        await db.execute(
            f"""
            UPDATE {T_LOTS} SET status = $1, shipment_no = $2, updated_at = $3
            WHERE lot_id = ANY($4::text[])
            """,
            LotStatus.SHIPPED.value, shipment_no, now, lot_ids,
        )
        for lot in lots:
            await _write_history(
                db, lot, LotAction.SHIP, actor,
                qty_in=int(lot["qty"]), qty_good=int(lot["qty"]),
                timestamp=now, remark=f"出貨單 {shipment_no}",
            )
        return {
            "shipment_no": shipment_no,
            "customer_code": payload["customer_code"],
            "lot_ids": sorted(lot_ids),
            "total_qty": total_qty,
            "shipped_at": now,
        }
