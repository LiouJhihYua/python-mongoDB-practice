"""批號（Lot）生產執行核心 —— MES 的心臟。

涵蓋開批、Track-In / Track-Out、單位換算、Q-Time 管制、拆批、併批、
扣留／放行、報廢與重工。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.config import settings
from app.database import (
    COL_DEFECT_CODES,
    COL_DEFECT_RECORDS,
    COL_DEVICES,
    COL_HOLDS,
    COL_LOT_HISTORY,
    COL_LOTS,
    COL_OPERATIONS,
    COL_SHIPMENTS,
    COL_WAFERS,
    COL_WORK_ORDERS,
    next_sequence,
)
from app.errors import NotFoundError, PermissionError_, StateError, ValidationError
from app.models.base import clean, clean_all, ensure_aware, shift_of, to_local, utcnow
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
from app.services import equipment_service, master_service, material_service
from app.services.user_service import is_certified


# ── 基本查詢 ────────────────────────────────────────────────
async def _gen_lot_id(db) -> str:
    ym = f"{to_local(utcnow()):%y%m}"
    return f"L{ym}{await next_sequence(db, f'lot:{ym}'):05d}"


async def get_lot(db, lot_id: str, raw: bool = False) -> dict:
    doc = await db[COL_LOTS].find_one({"lot_id": lot_id})
    if doc is None:
        raise NotFoundError(f"找不到批號：{lot_id}")
    return doc if raw else clean(doc)


async def list_lots(db, query: dict) -> dict:
    filt: dict[str, Any] = {}
    for field in ("status", "device_id", "wo_no", "customer_code"):
        if query.get(field):
            filt[field] = query[field]
    if query.get("op_code"):
        filt["current_op"] = query["op_code"]
    if query.get("on_hold") is True:
        filt["status"] = LotStatus.HOLD.value
    skip, limit = query.get("skip", 0), query.get("limit", 50)
    total = await db[COL_LOTS].count_documents(filt)
    cursor = (
        db[COL_LOTS].find(filt).sort([("priority", 1), ("created_at", 1)]).skip(skip).limit(limit)
    )
    return {
        "items": clean_all(await cursor.to_list(length=limit)),
        "total": total,
        "skip": skip,
        "limit": limit,
    }


async def lot_history(db, lot_id: str, limit: int = 200) -> list[dict]:
    await get_lot(db, lot_id, raw=True)
    cursor = db[COL_LOT_HISTORY].find({"lot_id": lot_id}).sort([("timestamp", 1)]).limit(limit)
    return clean_all(await cursor.to_list(length=limit))


async def _context(db, lot: dict) -> tuple[dict, dict, dict, dict]:
    """取得批號目前所在的 流程 / 站序 / 站別 / 產品。"""
    route = await master_service.get_active_route(db, lot["route_code"], lot.get("route_version"))
    step = await master_service.get_route_step(route, lot["current_seq"])
    if step is None:
        raise StateError(f"批號 {lot['lot_id']} 的站序 {lot['current_seq']} 不存在於流程 {route['route_code']}")
    operation = await db[COL_OPERATIONS].find_one({"op_code": step["op_code"]})
    if operation is None:
        raise NotFoundError(f"找不到站別：{step['op_code']}")
    if not operation.get("active", True):
        raise StateError(f"站別 {step['op_code']} 已停用，無法作業")
    device = await db[COL_DEVICES].find_one({"device_id": lot["device_id"]})
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


async def _write_history(db, lot: dict, action: LotAction, actor: str, **extra) -> dict:
    now = extra.pop("timestamp", None) or utcnow()
    doc = {
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
        "process_sec": 0.0,
        "queue_sec": 0.0,
        "defects": [],
        "remark": "",
    }
    doc.update(extra)
    await db[COL_LOT_HISTORY].insert_one(doc)
    return doc


async def _open_hold(db, lot_id: str) -> dict | None:
    return await db[COL_HOLDS].find_one({"lot_id": lot_id, "status": HoldStatus.OPEN.value})


# ── 開批 ────────────────────────────────────────────────────
async def create_lot(db, payload: dict, actor: str) -> dict:
    wo = await db[COL_WORK_ORDERS].find_one({"wo_no": payload["wo_no"]})
    if wo is None:
        raise ValidationError(f"工單不存在：{payload['wo_no']}")
    if wo["status"] not in {WorkOrderStatus.RELEASED.value, WorkOrderStatus.IN_PROGRESS.value}:
        raise StateError(f"工單 {wo['wo_no']} 狀態為 {wo['status']}，需先下達（RELEASED）才能開批")

    qty = int(payload["qty"])
    remaining = int(wo["plan_qty"]) - int(wo.get("released_qty", 0))
    if qty > remaining:
        raise ValidationError(f"投料量 {qty} 超過工單剩餘可投料量 {remaining}")

    device = await db[COL_DEVICES].find_one({"device_id": wo["device_id"]})
    route = await master_service.get_active_route(db, wo["route_code"])
    step = master_service.first_step(route)

    wafer_ids = payload.get("wafer_ids") or []
    if wafer_ids:
        await _reserve_wafers(db, wafer_ids, wo["device_id"], qty)

    now = utcnow()
    lot_id = await _gen_lot_id(db)
    lot = {
        "lot_id": lot_id,
        "wo_no": wo["wo_no"],
        "device_id": wo["device_id"],
        "customer_code": wo["customer_code"],
        "package_code": wo["package_code"],
        "route_code": route["route_code"],
        "route_version": route["version"],
        "current_seq": step["seq"],
        "current_op": step["op_code"],
        "status": LotStatus.WAITING.value,
        "qty": qty,
        "initial_qty": qty,
        "unit_type": wo.get("unit_type", UnitType.WAFER.value),
        "scrap_qty": 0,
        "carrier_id": payload.get("carrier_id", ""),
        "priority": payload.get("priority") or wo.get("priority", 5),
        "parent_lot_id": None,
        "child_lot_ids": [],
        "merged_from": [],
        "merged_into": None,
        "wafer_ids": wafer_ids,
        "eq_id": None,
        "track_in_at": None,
        "last_track_out_at": None,
        "qtime_violations": 0,
        "qtime_waived_seq": None,
        "rework_count": 0,
        "remark": payload.get("remark", ""),
        "created_at": now,
        "created_by": actor,
        "updated_at": now,
        "completed_at": None,
    }
    await db[COL_LOTS].insert_one(dict(lot))
    if wafer_ids:
        await db[COL_WAFERS].update_many(
            {"wafer_id": {"$in": wafer_ids}},
            {"$set": {"assembly_lot_id": lot_id, "consumed": True, "consumed_at": now}},
        )

    await db[COL_WORK_ORDERS].update_one(
        {"wo_no": wo["wo_no"]},
        {
            "$inc": {"released_qty": qty, "lot_count": 1},
            "$set": {"status": WorkOrderStatus.IN_PROGRESS.value, "updated_at": now},
        },
    )
    await _write_history(
        db, lot, LotAction.CREATE, actor,
        qty_in=qty, qty_good=qty, remark=f"自工單 {wo['wo_no']} 投料 {qty} {lot['unit_type']}",
    )
    return clean(await db[COL_LOTS].find_one({"lot_id": lot_id}))


async def _reserve_wafers(db, wafer_ids: list[str], device_id: str, qty: int) -> None:
    if len(set(wafer_ids)) != len(wafer_ids):
        raise ValidationError("投入晶圓 ID 重複")
    if len(wafer_ids) != qty:
        raise ValidationError(f"投入晶圓數 {len(wafer_ids)} 與批量 {qty} 不符")
    docs = await db[COL_WAFERS].find({"wafer_id": {"$in": wafer_ids}}).to_list(length=len(wafer_ids))
    found = {d["wafer_id"] for d in docs}
    missing = set(wafer_ids) - found
    if missing:
        raise ValidationError(f"晶圓不存在：{', '.join(sorted(missing))}")
    used = [d["wafer_id"] for d in docs if d.get("consumed")]
    if used:
        raise ValidationError(f"晶圓已被投入其他批號：{', '.join(sorted(used))}")
    wrong = [d["wafer_id"] for d in docs if d.get("device_id") != device_id]
    if wrong:
        raise ValidationError(f"晶圓料號與工單不符：{', '.join(sorted(wrong))}")


# ── 進站 ────────────────────────────────────────────────────
async def track_in(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot = await get_lot(db, payload["lot_id"], raw=True)
    status = lot["status"]
    if status == LotStatus.HOLD.value:
        hold = await _open_hold(db, lot["lot_id"])
        reason = (hold or {}).get("reason", "")
        raise StateError(f"批號 {lot['lot_id']} 扣留中（{reason}），請先放行")
    if status == LotStatus.RUNNING.value:
        raise StateError(f"批號 {lot['lot_id']} 已在 {lot.get('eq_id')} 加工中，請先出站")
    if status != LotStatus.WAITING.value:
        raise StateError(f"批號 {lot['lot_id']} 狀態為 {status}，不可進站")

    route, step, operation, device = await _context(db, lot)
    op_code = operation["op_code"]

    if operation.get("requires_certification") and not is_certified(user, op_code):
        raise PermissionError_(f"作業員 {actor} 未取得 {op_code} 站別資格認證")

    eq = None
    if operation.get("requires_equipment", True):
        eq_id = payload.get("eq_id")
        if not eq_id:
            raise ValidationError(f"站別 {op_code} 必須指定設備")
        eq = await equipment_service.get_equipment(db, eq_id)
        if not eq.get("active", True):
            raise StateError(f"設備 {eq_id} 已停用")
        if op_code not in (eq.get("op_codes") or []):
            raise ValidationError(f"設備 {eq_id} 不具備 {op_code} 站別能力")
        if eq.get("current_lot_id") and eq["current_lot_id"] != lot["lot_id"]:
            raise StateError(f"設備 {eq_id} 正在加工批號 {eq['current_lot_id']}")
        if eq.get("current_state") not in {s.value for s in RUNNABLE_EQUIPMENT_STATES}:
            raise StateError(f"設備 {eq_id} 目前狀態為 {eq.get('current_state')}，不可投料")

    now = utcnow()
    since = ensure_aware(lot.get("last_track_out_at") or lot["created_at"])
    queue_sec = max(0.0, (now - since).total_seconds())
    limit_min = int(operation.get("max_queue_minutes", 0) or 0)
    violated = limit_min > 0 and queue_sec > limit_min * 60
    #: 品保放行 Q-Time 扣留後給予特採，避免放行→再次逾時→再扣留的死循環
    waived = violated and lot.get("qtime_waived_seq") == lot["current_seq"]

    if violated and settings.auto_hold_on_qtime_violation and not waived:
        await db[COL_LOTS].update_one(
            {"lot_id": lot["lot_id"]},
            {"$inc": {"qtime_violations": 1}, "$set": {"updated_at": now}},
        )
        await _write_history(
            db, lot, LotAction.HOLD, actor,
            eq_id=payload.get("eq_id"), queue_sec=queue_sec,
            qtime_limit_min=limit_min, qtime_violation=True,
            remark=f"Q-Time 逾時：等待 {queue_sec / 60:.1f} 分，上限 {limit_min} 分",
        )
        await _apply_hold(
            db, lot, HoldReason.QTIME, actor,
            f"Q-Time 逾時自動扣留：等待 {queue_sec / 60:.1f} 分 > 上限 {limit_min} 分",
        )
        raise StateError(
            f"批號 {lot['lot_id']} 於 {op_code} 站 Q-Time 逾時（等待 {queue_sec / 60:.1f} 分 / 上限 {limit_min} 分），"
            "已自動扣留待品保判定"
        )

    await db[COL_LOTS].update_one(
        {"lot_id": lot["lot_id"]},
        {
            "$set": {
                "status": LotStatus.RUNNING.value,
                "eq_id": payload.get("eq_id") or None,
                "operator": actor,
                "track_in_at": now,
                "updated_at": now,
                "qtime_waived_seq": None,  # 特採僅對本站有效，用掉即失效
            },
            "$inc": {"qtime_violations": 1 if violated else 0},
        },
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
    return clean(await db[COL_LOTS].find_one({"lot_id": lot["lot_id"]}))


# ── 出站 ────────────────────────────────────────────────────
async def track_out(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot = await get_lot(db, payload["lot_id"], raw=True)
    if lot["status"] != LotStatus.RUNNING.value:
        raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，需先 Track-In 才能出站")

    route, step, operation, device = await _context(db, lot)
    op_code = operation["op_code"]
    qty_in = int(lot["qty"])
    expected, out_unit = compute_expected_output(qty_in, operation, device, lot["unit_type"])

    bin_map = payload.get("bin_map")
    if bin_map:
        if not operation.get("is_test"):
            raise ValidationError(f"站別 {op_code} 非測試站，不可使用 Bin 分佈出站")
        total = sum(int(v) for v in bin_map.values())
        if total != expected:
            raise ValidationError(
                f"Bin 合計 {total} 與應產出量 {expected} 不符（進站 {qty_in} {lot['unit_type']}）"
            )
        pass_bins = {str(b) for b in (operation.get("pass_bins") or [1])}
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
        materials_used = await material_service.consume(
            db, payload["materials"], lot, op_code, actor
        )

    now = utcnow()
    track_in_at = ensure_aware(lot["track_in_at"]) if lot.get("track_in_at") else now
    process_sec = max(0.0, (now - track_in_at).total_seconds())
    step_yield = round(good / expected, 6) if expected else 0.0

    next_step = master_service.next_step(route, lot["current_seq"])
    update: dict[str, Any] = {
        "qty": good,
        "unit_type": out_unit,
        "eq_id": None,
        "track_in_at": None,
        "last_track_out_at": now,
        "updated_at": now,
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

    await db[COL_LOTS].update_one(
        {"lot_id": lot["lot_id"]},
        {"$set": update, "$inc": {"scrap_qty": reject}},
    )
    if lot.get("eq_id"):
        await equipment_service.set_state(
            db, lot["eq_id"], EquipmentState.STANDBY, actor,
            reason_code="TRACK_OUT", remark=f"批號 {lot['lot_id']} 出站", lot_id=None,
        )

    history = await _write_history(
        db, lot, LotAction.TRACK_OUT, actor,
        eq_id=lot.get("eq_id"), qty_in=qty_in, qty_expected=expected,
        qty_good=good, qty_reject=reject, unit_in=lot["unit_type"], unit_out=out_unit,
        defects=defects, bin_map=bin_map, materials=materials_used,
        process_sec=round(process_sec, 1), step_yield=step_yield,
        standard_cycle_time_sec=operation.get("standard_cycle_time_sec", 0),
        timestamp=now, remark=payload.get("remark", ""),
    )
    if defects:
        await _record_defects(db, lot, operation, defects, actor, now)

    result = clean(await db[COL_LOTS].find_one({"lot_id": lot["lot_id"]}))
    result["last_step"] = {
        "op_code": op_code,
        "qty_in": qty_in,
        "qty_expected": expected,
        "qty_good": good,
        "qty_reject": reject,
        "step_yield": step_yield,
        "process_sec": history["process_sec"],
    }
    return result


async def _validate_defect_codes(db, defects: list[dict], op_code: str) -> None:
    codes = [d["defect_code"] for d in defects]
    docs = await db[COL_DEFECT_CODES].find({"code": {"$in": codes}}).to_list(length=len(codes))
    found = {d["code"]: d for d in docs}
    missing = set(codes) - set(found)
    if missing:
        raise ValidationError(f"不良代碼不存在：{', '.join(sorted(missing))}")
    for code, doc in found.items():
        applicable = doc.get("op_codes") or []
        if applicable and op_code not in applicable:
            raise ValidationError(f"不良代碼 {code} 不適用於站別 {op_code}")


async def _record_defects(db, lot: dict, operation: dict, defects: list[dict], actor: str, now: datetime) -> None:
    codes = [d["defect_code"] for d in defects]
    docs = await db[COL_DEFECT_CODES].find({"code": {"$in": codes}}).to_list(length=len(codes))
    meta = {d["code"]: d for d in docs}
    rows = [
        {
            "lot_id": lot["lot_id"],
            "wo_no": lot.get("wo_no"),
            "device_id": lot.get("device_id"),
            "customer_code": lot.get("customer_code"),
            "op_code": operation["op_code"],
            "seq": lot["current_seq"],
            "eq_id": lot.get("eq_id"),
            "defect_code": d["defect_code"],
            "defect_name": meta.get(d["defect_code"], {}).get("name", ""),
            "category": meta.get(d["defect_code"], {}).get("category", ""),
            "disposition": meta.get(d["defect_code"], {}).get("default_disposition", "SCRAP"),
            "qty": int(d["qty"]),
            "remark": d.get("remark", ""),
            "operator": actor,
            "timestamp": now,
            "shift": shift_of(now),
        }
        for d in defects
    ]
    if rows:
        await db[COL_DEFECT_RECORDS].insert_many(rows)


# ── 扣留 / 放行 ─────────────────────────────────────────────
async def _apply_hold(db, lot: dict, reason: HoldReason | str, actor: str, remark: str) -> dict:
    now = utcnow()
    await db[COL_HOLDS].insert_one(
        {
            "lot_id": lot["lot_id"],
            "wo_no": lot.get("wo_no"),
            "device_id": lot.get("device_id"),
            "op_code": lot.get("current_op"),
            "seq": lot.get("current_seq"),
            "reason": str(reason),
            "remark": remark,
            "status": HoldStatus.OPEN.value,
            "held_by": actor,
            "held_at": now,
            "released_by": None,
            "released_at": None,
            "status_before_hold": lot["status"],
        }
    )
    await db[COL_LOTS].update_one(
        {"lot_id": lot["lot_id"]},
        {"$set": {"status": LotStatus.HOLD.value, "updated_at": now}},
    )
    return clean(await db[COL_LOTS].find_one({"lot_id": lot["lot_id"]}))


async def hold_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot = await get_lot(db, payload["lot_id"], raw=True)
    if lot["status"] not in {LotStatus.WAITING.value, LotStatus.RUNNING.value}:
        raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，不可扣留")
    if await _open_hold(db, lot["lot_id"]):
        raise StateError(f"批號 {lot['lot_id']} 已在扣留中")
    result = await _apply_hold(db, lot, payload.get("reason", HoldReason.QUALITY), actor, payload.get("remark", ""))
    await _write_history(db, lot, LotAction.HOLD, actor, remark=f"{payload.get('reason')} / {payload.get('remark', '')}")
    return result


async def release_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot = await get_lot(db, payload["lot_id"], raw=True)
    hold = await _open_hold(db, lot["lot_id"])
    if hold is None:
        raise StateError(f"批號 {lot['lot_id']} 目前沒有扣留紀錄")
    now = utcnow()
    await db[COL_HOLDS].update_one(
        {"_id": hold["_id"]},
        {
            "$set": {
                "status": HoldStatus.RELEASED.value,
                "released_by": actor,
                "released_at": now,
                "release_remark": payload.get("remark", ""),
            }
        },
    )
    restored = hold.get("status_before_hold") or LotStatus.WAITING.value
    if restored not in {LotStatus.WAITING.value, LotStatus.RUNNING.value}:
        restored = LotStatus.WAITING.value
    update: dict[str, Any] = {"status": restored, "updated_at": now}
    if hold.get("reason") == HoldReason.QTIME.value:
        # 品保已判定可用，對目前站別給予一次性特採
        update["qtime_waived_seq"] = lot["current_seq"]
    await db[COL_LOTS].update_one({"lot_id": lot["lot_id"]}, {"$set": update})
    await _write_history(db, lot, LotAction.RELEASE, actor, remark=payload.get("remark", ""))
    return clean(await db[COL_LOTS].find_one({"lot_id": lot["lot_id"]}))


# ── 拆批 / 併批 ─────────────────────────────────────────────
async def split_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot = await get_lot(db, payload["lot_id"], raw=True)
    if lot["status"] != LotStatus.WAITING.value:
        raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，僅待進站（WAITING）可拆批")
    quantities = [int(q) for q in payload["quantities"]]
    if sum(quantities) != int(lot["qty"]):
        raise ValidationError(f"子批合計 {sum(quantities)} 與母批現有量 {lot['qty']} 不符")

    now = utcnow()
    wafer_ids = lot.get("wafer_ids") or []
    can_slice = lot["unit_type"] == UnitType.WAFER.value and len(wafer_ids) == int(lot["qty"])

    children: list[dict] = []
    cursor_pos = 0
    for idx, qty in enumerate(quantities, start=1):
        child_id = f"{lot['lot_id']}.{idx}"
        if can_slice:
            child_wafers = wafer_ids[cursor_pos : cursor_pos + qty]
            cursor_pos += qty
        else:
            child_wafers = list(wafer_ids)  # 追溯用：子批同樣源自這些晶圓
        child = {
            **{k: v for k, v in lot.items() if k != "_id"},
            "lot_id": child_id,
            "qty": qty,
            "initial_qty": qty,
            "scrap_qty": 0,
            "status": LotStatus.WAITING.value,
            "parent_lot_id": lot["lot_id"],
            "child_lot_ids": [],
            "merged_from": [],
            "merged_into": None,
            "wafer_ids": child_wafers,
            "eq_id": None,
            "track_in_at": None,
            "qtime_waived_seq": None,
            "created_at": now,
            "created_by": actor,
            "updated_at": now,
            "completed_at": None,
            "remark": payload.get("reason", ""),
        }
        await db[COL_LOTS].insert_one(dict(child))
        await _write_history(
            db, child, LotAction.CREATE, actor,
            qty_in=qty, qty_good=qty, remark=f"由 {lot['lot_id']} 拆批產生",
        )
        children.append(child_id)

    await db[COL_LOTS].update_one(
        {"lot_id": lot["lot_id"]},
        {
            "$set": {
                "status": LotStatus.SPLIT.value,
                "child_lot_ids": children,
                "qty": 0,
                "updated_at": now,
                "completed_at": now,
            }
        },
    )
    await _write_history(
        db, lot, LotAction.SPLIT, actor,
        qty_in=int(lot["qty"]), remark=f"拆為 {', '.join(children)}；原因：{payload.get('reason', '')}",
    )
    return {
        "parent_lot_id": lot["lot_id"],
        "children": clean_all(
            await db[COL_LOTS].find({"lot_id": {"$in": children}}).to_list(length=len(children))
        ),
    }


async def merge_lots(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot_ids = list(dict.fromkeys(payload["lot_ids"]))
    if len(lot_ids) < 2:
        raise ValidationError("併批至少需要兩個不同批號")
    lots = await db[COL_LOTS].find({"lot_id": {"$in": lot_ids}}).to_list(length=len(lot_ids))
    missing = set(lot_ids) - {l["lot_id"] for l in lots}
    if missing:
        raise NotFoundError(f"找不到批號：{', '.join(sorted(missing))}")

    keys = {(l["device_id"], l["route_code"], l["current_seq"], l["unit_type"]) for l in lots}
    if len(keys) != 1:
        raise ValidationError("併批要求相同產品料號、流程、站序與計量單位")
    bad = [l["lot_id"] for l in lots if l["status"] != LotStatus.WAITING.value]
    if bad:
        raise StateError(f"以下批號非待進站狀態，不可併批：{', '.join(sorted(bad))}")

    now = utcnow()
    base = lots[0]
    new_id = await _gen_lot_id(db)
    merged = {
        **{k: v for k, v in base.items() if k != "_id"},
        "lot_id": new_id,
        "qty": sum(int(l["qty"]) for l in lots),
        "initial_qty": sum(int(l["initial_qty"]) for l in lots),
        "scrap_qty": sum(int(l.get("scrap_qty", 0)) for l in lots),
        "status": LotStatus.WAITING.value,
        "parent_lot_id": None,
        "child_lot_ids": [],
        "merged_from": sorted(lot_ids),
        "merged_into": None,
        "wafer_ids": sorted({w for l in lots for w in (l.get("wafer_ids") or [])}),
        "carrier_id": payload.get("carrier_id", ""),
        "priority": min(int(l.get("priority", 5)) for l in lots),
        "eq_id": None,
        "track_in_at": None,
        "last_track_out_at": max(
            (ensure_aware(l["last_track_out_at"]) for l in lots if l.get("last_track_out_at")),
            default=None,
        ),
        "qtime_violations": sum(int(l.get("qtime_violations", 0)) for l in lots),
        "rework_count": max(int(l.get("rework_count", 0)) for l in lots),
        "created_at": now,
        "created_by": actor,
        "updated_at": now,
        "completed_at": None,
        "remark": payload.get("reason", ""),
    }
    await db[COL_LOTS].insert_one(dict(merged))
    await db[COL_LOTS].update_many(
        {"lot_id": {"$in": lot_ids}},
        {
            "$set": {
                "status": LotStatus.MERGED.value,
                "merged_into": new_id,
                "qty": 0,
                "updated_at": now,
                "completed_at": now,
            }
        },
    )
    for src in lots:
        await _write_history(db, src, LotAction.MERGE, actor, remark=f"併入 {new_id}")
    await _write_history(
        db, merged, LotAction.CREATE, actor,
        qty_in=merged["qty"], qty_good=merged["qty"],
        remark=f"由 {', '.join(sorted(lot_ids))} 併批產生",
    )
    return clean(await db[COL_LOTS].find_one({"lot_id": new_id}))


# ── 報廢 / 重工 ─────────────────────────────────────────────
async def scrap_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot = await get_lot(db, payload["lot_id"], raw=True)
    if lot["status"] not in ACTIVE_LOT_STATUSES:
        raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，不可報廢")
    qty = int(payload["qty"])
    if qty > int(lot["qty"]):
        raise ValidationError(f"報廢量 {qty} 超過批號現有量 {lot['qty']}")

    _, _, operation, _ = await _context(db, lot)
    await _validate_defect_codes(db, [{"defect_code": payload["defect_code"], "qty": qty}], operation["op_code"])

    now = utcnow()
    remaining = int(lot["qty"]) - qty
    update: dict[str, Any] = {"qty": remaining, "updated_at": now}
    if remaining == 0:
        update["status"] = LotStatus.SCRAPPED.value
        update["completed_at"] = now
        update["eq_id"] = None
        update["track_in_at"] = None
    await db[COL_LOTS].update_one(
        {"lot_id": lot["lot_id"]}, {"$set": update, "$inc": {"scrap_qty": qty}}
    )
    await _record_defects(db, lot, operation, [{"defect_code": payload["defect_code"], "qty": qty}], actor, now)
    await _write_history(
        db, lot, LotAction.SCRAP, actor,
        qty_in=int(lot["qty"]), qty_good=remaining, qty_reject=qty,
        defects=[{"defect_code": payload["defect_code"], "qty": qty}],
        timestamp=now, remark=payload.get("remark", ""),
    )
    return clean(await db[COL_LOTS].find_one({"lot_id": lot["lot_id"]}))


async def ship_lots(db, payload: dict, user: dict) -> dict:
    """出貨：只有全流程完工的批號可出貨，並產生出貨單供追溯。"""
    actor = user["username"]
    lot_ids = list(dict.fromkeys(payload["lot_ids"]))
    lots = await db[COL_LOTS].find({"lot_id": {"$in": lot_ids}}).to_list(length=len(lot_ids))
    missing = set(lot_ids) - {l["lot_id"] for l in lots}
    if missing:
        raise NotFoundError(f"找不到批號：{', '.join(sorted(missing))}")
    not_done = [l["lot_id"] for l in lots if l["status"] != LotStatus.COMPLETED.value]
    if not_done:
        raise StateError(f"以下批號尚未完工，不可出貨：{', '.join(sorted(not_done))}")
    wrong_customer = [l["lot_id"] for l in lots if l.get("customer_code") != payload["customer_code"]]
    if wrong_customer:
        raise ValidationError(f"以下批號不屬於客戶 {payload['customer_code']}：{', '.join(sorted(wrong_customer))}")

    now = utcnow()
    ym = f"{to_local(now):%y%m}"
    shipment_no = f"SH{ym}{await next_sequence(db, f'shipment:{ym}'):04d}"
    total_qty = sum(int(l["qty"]) for l in lots)
    await db[COL_SHIPMENTS].insert_one(
        {
            "shipment_no": shipment_no,
            "customer_code": payload["customer_code"],
            "customer_po": payload.get("customer_po", ""),
            "lot_ids": sorted(lot_ids),
            "device_ids": sorted({l["device_id"] for l in lots}),
            "total_qty": total_qty,
            "remark": payload.get("remark", ""),
            "shipped_by": actor,
            "shipped_at": now,
        }
    )
    await db[COL_LOTS].update_many(
        {"lot_id": {"$in": lot_ids}},
        {"$set": {"status": LotStatus.SHIPPED.value, "shipment_no": shipment_no, "updated_at": now}},
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


async def rework_lot(db, payload: dict, user: dict) -> dict:
    actor = user["username"]
    lot = await get_lot(db, payload["lot_id"], raw=True)
    if lot["status"] not in {LotStatus.WAITING.value, LotStatus.HOLD.value}:
        raise StateError(f"批號 {lot['lot_id']} 狀態為 {lot['status']}，僅待進站或扣留中可重工")
    to_seq = int(payload["to_seq"])
    if to_seq >= int(lot["current_seq"]):
        raise ValidationError(f"重工站序 {to_seq} 必須小於目前站序 {lot['current_seq']}")

    route = await master_service.get_active_route(db, lot["route_code"], lot.get("route_version"))
    target = await master_service.get_route_step(route, to_seq)
    if target is None:
        raise ValidationError(f"流程 {route['route_code']} 沒有站序 {to_seq}")
    operation = await db[COL_OPERATIONS].find_one({"op_code": target["op_code"]})
    if operation is not None and not operation.get("allow_rework", True):
        raise StateError(f"站別 {target['op_code']} 不允許重工")

    now = utcnow()
    from_seq, from_op = lot["current_seq"], lot["current_op"]
    await db[COL_LOTS].update_one(
        {"lot_id": lot["lot_id"]},
        {
            "$set": {
                "current_seq": target["seq"],
                "current_op": target["op_code"],
                "status": LotStatus.WAITING.value,
                "last_track_out_at": now,  # 重工重新起算 Q-Time
                "updated_at": now,
            },
            "$inc": {"rework_count": 1},
        },
    )
    await _write_history(
        db, lot, LotAction.REWORK, actor,
        qty_in=int(lot["qty"]),
        remark=f"由 {from_seq}/{from_op} 退回 {target['seq']}/{target['op_code']}；原因：{payload.get('reason', '')}",
    )
    return clean(await db[COL_LOTS].find_one({"lot_id": lot["lot_id"]}))
