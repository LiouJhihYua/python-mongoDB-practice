"""派工與 Q-Time 預警。

現場最常問的問題是「下一批做哪個」。這裡把待進站批號依
Q-Time 剩餘 → 急單優先序 → 交期 → 等待時間排出順序，並附上排序理由，
讓作業員不必自己判斷，也讓 Q-Time 在超時「之前」就被看見。
"""

from __future__ import annotations

from app.database import (
    COL_EQUIPMENTS,
    COL_LOTS,
    COL_OPERATIONS,
    COL_WORK_ORDERS,
)
from app.models.base import ensure_aware, utcnow
from app.models.enums import LotStatus, RUNNABLE_EQUIPMENT_STATES

#: Q-Time 剩餘低於此分鐘數即視為緊急，插到隊伍最前面
QTIME_URGENT_MINUTES = 60
#: 排序時「沒有 Q-Time 管制」用的哨兵值
NO_QTIME = 10**9


async def _operation_map(db) -> dict[str, dict]:
    rows = await db[COL_OPERATIONS].find({}).to_list(length=None)
    return {r["op_code"]: r for r in rows}


async def _free_equipment_map(db) -> dict[str, list[str]]:
    """各站別目前可接單的機台。"""
    runnable = [s.value for s in RUNNABLE_EQUIPMENT_STATES]
    rows = await db[COL_EQUIPMENTS].find(
        {"active": True, "current_lot_id": None, "current_state": {"$in": runnable}}
    ).to_list(length=None)
    result: dict[str, list[str]] = {}
    for eq in rows:
        for op_code in eq.get("op_codes") or []:
            result.setdefault(op_code, []).append(eq["eq_id"])
    return {k: sorted(v) for k, v in result.items()}


async def _due_date_map(db, wo_nos: list[str]) -> dict[str, object]:
    if not wo_nos:
        return {}
    rows = await db[COL_WORK_ORDERS].find(
        {"wo_no": {"$in": wo_nos}}, {"_id": 0, "wo_no": 1, "due_date": 1}
    ).to_list(length=None)
    return {r["wo_no"]: r.get("due_date") for r in rows}


async def dispatch_list(db, op_code: str | None = None, limit: int = 100) -> dict:
    """派工清單：待進站批號的建議加工順序。"""
    now = utcnow()
    filt: dict = {"status": LotStatus.WAITING.value}
    if op_code:
        filt["current_op"] = op_code

    lots = await db[COL_LOTS].find(filt).to_list(length=None)
    operations = await _operation_map(db)
    free_equipment = await _free_equipment_map(db)
    due_dates = await _due_date_map(db, sorted({l["wo_no"] for l in lots if l.get("wo_no")}))

    rows: list[dict] = []
    for lot in lots:
        operation = operations.get(lot["current_op"], {})
        waiting_min = (now - ensure_aware(lot.get("last_track_out_at") or lot["created_at"])).total_seconds() / 60
        limit_min = int(operation.get("max_queue_minutes", 0) or 0)
        remaining = round(limit_min - waiting_min, 1) if limit_min else None

        due = due_dates.get(lot.get("wo_no"))
        days_to_due = round((ensure_aware(due) - now).total_seconds() / 86400, 2) if due else None

        needs_eq = operation.get("requires_equipment", True)
        available = free_equipment.get(lot["current_op"], []) if needs_eq else []

        rows.append({
            "lot_id": lot["lot_id"],
            "device_id": lot["device_id"],
            "customer_code": lot.get("customer_code"),
            "wo_no": lot.get("wo_no"),
            "seq": lot["current_seq"],
            "op_code": lot["current_op"],
            "op_name": operation.get("name", ""),
            "qty": lot["qty"],
            "unit_type": lot["unit_type"],
            "priority": int(lot.get("priority", 5)),
            "waiting_minutes": round(waiting_min, 1),
            "qtime_limit_min": limit_min or None,
            "qtime_remaining_min": remaining,
            "qtime_expired": remaining is not None and remaining < 0,
            "days_to_due": days_to_due,
            "available_equipments": available,
            "ready": (not needs_eq) or bool(available),
            "carrier_id": lot.get("carrier_id", ""),
        })

    for row in rows:
        row["urgency"], row["reason"] = _urgency(row)
    rows.sort(key=_sort_key)

    return {
        "generated_at": now,
        "op_code": op_code,
        "total": len(rows),
        "items": rows[:limit],
        "truncated": len(rows) > limit,
    }


def _urgency(row: dict) -> tuple[str, str]:
    """排序理由，讓現場知道為什麼這批要先做。"""
    remaining = row["qtime_remaining_min"]
    if remaining is not None and remaining < 0:
        return "CRITICAL", f"Q-Time 已逾時 {abs(remaining):.0f} 分，進站將自動扣留"
    if remaining is not None and remaining <= QTIME_URGENT_MINUTES:
        return "URGENT", f"Q-Time 剩 {remaining:.0f} 分"
    if row["priority"] <= 2:
        return "HIGH", f"急單（優先序 {row['priority']}）"
    if row["days_to_due"] is not None and row["days_to_due"] <= 2:
        return "HIGH", f"交期剩 {row['days_to_due']:.1f} 天"
    if not row["ready"]:
        return "BLOCKED", "無可用機台，等待設備釋出"
    return "NORMAL", f"已等待 {row['waiting_minutes']:.0f} 分"


def _sort_key(row: dict) -> tuple:
    remaining = row["qtime_remaining_min"]
    urgent = remaining is not None and remaining <= QTIME_URGENT_MINUTES
    return (
        0 if urgent else 1,                       # Q-Time 緊急者一律插隊
        remaining if urgent else NO_QTIME,        # 緊急群內依剩餘時間
        0 if row["ready"] else 1,                 # 沒機台的往後排
        row["priority"],
        row["days_to_due"] if row["days_to_due"] is not None else NO_QTIME,
        -row["waiting_minutes"],
    )


async def qtime_watch(db, warn_minutes: int = QTIME_URGENT_MINUTES) -> dict:
    """Q-Time 預警：即將超時或已超時的待進站批號。

    原本的機制是進站時才發現逾時並自動扣留 —— 那時已經來不及了。
    """
    data = await dispatch_list(db, limit=10**6)
    watch = [
        row for row in data["items"]
        if row["qtime_remaining_min"] is not None and row["qtime_remaining_min"] <= warn_minutes
    ]
    expired = [row for row in watch if row["qtime_expired"]]
    return {
        "generated_at": data["generated_at"],
        "warn_minutes": warn_minutes,
        "expired_count": len(expired),
        "at_risk_count": len(watch) - len(expired),
        "items": watch,
    }
