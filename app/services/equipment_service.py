"""設備服務：SEMI E10 狀態機、狀態履歷、OEE、PM 保養。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.database import (
    COL_EQUIPMENT_LOGS,
    COL_EQUIPMENTS,
    COL_LOT_HISTORY,
    COL_PM_TASKS,
)
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import clean, clean_all, utcnow
from app.models.enums import (
    EquipmentState,
    LotAction,
    NON_SCHEDULED_STATES,
    PMStatus,
    UPTIME_STATES,
)


async def get_equipment(db, eq_id: str) -> dict:
    doc = await db[COL_EQUIPMENTS].find_one({"eq_id": eq_id})
    if doc is None:
        raise NotFoundError(f"找不到設備：{eq_id}")
    return doc


async def set_state(
    db,
    eq_id: str,
    state: EquipmentState | str,
    actor: str,
    reason_code: str = "",
    remark: str = "",
    lot_id: str | None = None,
) -> dict:
    """切換設備狀態：關閉前一段狀態履歷並開啟新的一段。"""
    eq = await get_equipment(db, eq_id)
    state = EquipmentState(state)
    now = utcnow()

    if eq.get("current_state") == state.value and lot_id == eq.get("current_lot_id"):
        return clean(eq)

    # 關閉前一段
    await db[COL_EQUIPMENT_LOGS].update_many(
        {"eq_id": eq_id, "end_time": None},
        {"$set": {"end_time": now}},
    )
    async for log in db[COL_EQUIPMENT_LOGS].find({"eq_id": eq_id, "duration_sec": None}):
        start = _aware(log["start_time"])
        await db[COL_EQUIPMENT_LOGS].update_one(
            {"_id": log["_id"]},
            {"$set": {"duration_sec": max(0.0, (now - start).total_seconds())}},
        )

    await db[COL_EQUIPMENT_LOGS].insert_one(
        {
            "eq_id": eq_id,
            "state": state.value,
            "reason_code": reason_code,
            "remark": remark,
            "lot_id": lot_id,
            "operator": actor,
            "start_time": now,
            "end_time": None,
            "duration_sec": None,
        }
    )
    await db[COL_EQUIPMENTS].update_one(
        {"eq_id": eq_id},
        {
            "$set": {
                "current_state": state.value,
                "state_since": now,
                "state_reason": reason_code or remark,
                "current_lot_id": lot_id,
                "updated_at": now,
                "updated_by": actor,
            }
        },
    )
    return clean(await db[COL_EQUIPMENTS].find_one({"eq_id": eq_id}))


def _aware(dt: datetime) -> datetime:
    from datetime import timezone

    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def state_history(db, eq_id: str, limit: int = 100) -> list[dict]:
    cursor = db[COL_EQUIPMENT_LOGS].find({"eq_id": eq_id}).sort([("start_time", -1)]).limit(limit)
    return clean_all(await cursor.to_list(length=limit))


async def state_summary(db, area: str | None = None) -> dict:
    """機台狀態即時分佈，供戰情看板使用。"""
    match: dict = {"active": True}
    if area:
        match["area"] = area
    pipeline = [
        {"$match": match},
        {"$group": {"_id": "$current_state", "count": {"$sum": 1}, "equipments": {"$push": "$eq_id"}}},
    ]
    rows = await db[COL_EQUIPMENTS].aggregate(pipeline).to_list(length=None)
    by_state = {r["_id"]: {"count": r["count"], "equipments": sorted(r["equipments"])} for r in rows}
    total = sum(v["count"] for v in by_state.values())
    up = sum(v["count"] for s, v in by_state.items() if s in {x.value for x in UPTIME_STATES})
    return {
        "total": total,
        "uptime_count": up,
        "uptime_ratio": round(up / total, 4) if total else 0.0,
        "by_state": by_state,
    }


# ── OEE ─────────────────────────────────────────────────────
async def calc_oee(db, eq_id: str, start: datetime, end: datetime) -> dict:
    """OEE = 稼動率 × 效能 × 良率（SEMI E10 時間分類）。"""
    if end <= start:
        raise ValidationError("結束時間必須晚於開始時間")
    eq = await get_equipment(db, eq_id)
    start, end = _aware(start), _aware(end)

    # 1) 依 E10 狀態彙總各段時間（截取落在區間內的部分）
    logs = await db[COL_EQUIPMENT_LOGS].find(
        {"eq_id": eq_id, "start_time": {"$lt": end}}
    ).to_list(length=None)
    seconds: dict[str, float] = {s.value: 0.0 for s in EquipmentState}
    for log in logs:
        lo = max(_aware(log["start_time"]), start)
        hi = min(_aware(log["end_time"]) if log.get("end_time") else end, end)
        if hi > lo:
            seconds[log["state"]] = seconds.get(log["state"], 0.0) + (hi - lo).total_seconds()

    total_sec = (end - start).total_seconds()
    non_scheduled = sum(seconds[s.value] for s in NON_SCHEDULED_STATES)
    # 未涵蓋到的時間視為非排程（例如設備建檔前）
    covered = sum(seconds.values())
    if covered < total_sec:
        non_scheduled += total_sec - covered
    scheduled = max(0.0, total_sec - non_scheduled)
    productive = seconds[EquipmentState.PRODUCTIVE.value]
    uptime = sum(seconds[s.value] for s in UPTIME_STATES)

    # 2) 產出：本區間內於此設備 Track-Out 的數量
    rows = await db[COL_LOT_HISTORY].aggregate(
        [
            {
                "$match": {
                    "eq_id": eq_id,
                    "action": LotAction.TRACK_OUT.value,
                    "timestamp": {"$gte": start, "$lt": end},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "good": {"$sum": "$qty_good"},
                    "reject": {"$sum": "$qty_reject"},
                    "lots": {"$sum": 1},
                    "process_sec": {"$sum": "$process_sec"},
                }
            },
        ]
    ).to_list(length=1)
    agg = rows[0] if rows else {"good": 0, "reject": 0, "lots": 0, "process_sec": 0}
    good, reject = agg["good"], agg["reject"]
    processed = good + reject

    ideal_ct = float(eq.get("ideal_cycle_time_sec") or 1.0)
    availability = productive / scheduled if scheduled else 0.0
    performance = (ideal_ct * processed) / productive if productive else 0.0
    performance = min(performance, 1.0)  # 超過 100% 視為標準工時需重新校正
    quality = good / processed if processed else 0.0

    return {
        "eq_id": eq_id,
        "name": eq.get("name", ""),
        "window": {"start": start, "end": end, "total_sec": total_sec},
        "time_breakdown_sec": {k: round(v, 1) for k, v in seconds.items()},
        "scheduled_sec": round(scheduled, 1),
        "productive_sec": round(productive, 1),
        "uptime_sec": round(uptime, 1),
        "uptime_ratio": round(uptime / scheduled, 4) if scheduled else 0.0,
        "utilization": round(productive / total_sec, 4) if total_sec else 0.0,
        "units_processed": processed,
        "units_good": good,
        "units_reject": reject,
        "lots_processed": agg["lots"],
        "availability": round(availability, 4),
        "performance": round(performance, 4),
        "quality": round(quality, 4),
        "oee": round(availability * performance * quality, 4),
    }


async def oee_overview(db, start: datetime, end: datetime, area: str | None = None) -> list[dict]:
    filt: dict = {"active": True}
    if area:
        filt["area"] = area
    eq_ids = [e["eq_id"] for e in await db[COL_EQUIPMENTS].find(filt, {"eq_id": 1}).to_list(length=None)]
    return [await calc_oee(db, eq_id, start, end) for eq_id in sorted(eq_ids)]


# ── PM 保養 ─────────────────────────────────────────────────
async def create_pm(db, eq_id: str, pm_type: str, due_date: datetime, actor: str, remark: str = "") -> dict:
    await get_equipment(db, eq_id)
    doc = {
        "eq_id": eq_id,
        "pm_type": pm_type,
        "due_date": due_date,
        "status": PMStatus.PLANNED.value,
        "remark": remark,
        "created_at": utcnow(),
        "created_by": actor,
        "done_at": None,
        "performed_by": None,
    }
    result = await db[COL_PM_TASKS].insert_one(doc)
    return clean(await db[COL_PM_TASKS].find_one({"_id": result.inserted_id}))


async def list_pm(db, eq_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    filt = {k: v for k, v in {"eq_id": eq_id, "status": status}.items() if v}
    cursor = db[COL_PM_TASKS].find(filt).sort([("due_date", 1)]).limit(limit)
    rows = clean_all(await cursor.to_list(length=limit))
    now = utcnow()
    for row in rows:
        if row["status"] == PMStatus.PLANNED.value and _aware(row["due_date"]) < now:
            row["status"] = PMStatus.OVERDUE.value
    return rows


async def complete_pm(db, pm_id: str, actor: str, remark: str = "") -> dict:
    from bson import ObjectId

    try:
        oid = ObjectId(pm_id)
    except Exception:
        raise ValidationError(f"PM 單號格式錯誤：{pm_id}")
    pm = await db[COL_PM_TASKS].find_one({"_id": oid})
    if pm is None:
        raise NotFoundError(f"找不到 PM 工單：{pm_id}")
    if pm["status"] == PMStatus.DONE.value:
        raise StateError("此 PM 工單已完成")
    await db[COL_PM_TASKS].update_one(
        {"_id": oid},
        {
            "$set": {
                "status": PMStatus.DONE.value,
                "done_at": utcnow(),
                "performed_by": actor,
                "remark": remark or pm.get("remark", ""),
            }
        },
    )
    # 保養完成 → 依保養週期自動排下一次
    eq = await get_equipment(db, pm["eq_id"])
    interval = int(eq.get("pm_interval_days") or 0)
    if interval > 0:
        await create_pm(
            db, pm["eq_id"], pm["pm_type"], utcnow() + timedelta(days=interval), actor, "系統自動排程"
        )
    return clean(await db[COL_PM_TASKS].find_one({"_id": oid}))
