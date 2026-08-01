"""設備服務：SEMI E10 狀態機、狀態履歷、OEE、PM 保養。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.database import (
    T_EQUIPMENT_LOGS,
    T_EQUIPMENTS,
    T_LOT_HISTORY,
    T_PM_TASKS,
    fetch_all,
    fetch_one,
)
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import ensure_aware, utcnow
from app.models.enums import (
    EquipmentState,
    LotAction,
    NON_SCHEDULED_STATES,
    PMStatus,
    UPTIME_STATES,
)

UPTIME_VALUES = [s.value for s in UPTIME_STATES]
NON_SCHEDULED_VALUES = [s.value for s in NON_SCHEDULED_STATES]


async def get_equipment(db, eq_id: str) -> dict:
    row = await fetch_one(db, f"SELECT * FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id)
    if row is None:
        raise NotFoundError(f"找不到設備：{eq_id}")
    return row


async def lock_equipment(db, eq_id: str) -> dict:
    """取得設備並鎖住該列，避免兩批同時搶同一台機器。"""
    row = await fetch_one(db, f"SELECT * FROM {T_EQUIPMENTS} WHERE eq_id = $1 FOR UPDATE", eq_id)
    if row is None:
        raise NotFoundError(f"找不到設備：{eq_id}")
    return row


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

    if eq["current_state"] == state.value and lot_id == eq["current_lot_id"]:
        return eq

    await db.execute(
        f"""
        UPDATE {T_EQUIPMENT_LOGS}
        SET end_time = $1,
            duration_sec = GREATEST(0, EXTRACT(EPOCH FROM ($1 - start_time)))
        WHERE eq_id = $2 AND end_time IS NULL
        """,
        now, eq_id,
    )
    await db.execute(
        f"""
        INSERT INTO {T_EQUIPMENT_LOGS}
            (eq_id, state, reason_code, remark, lot_id, operator, start_time)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        eq_id, state.value, reason_code, remark, lot_id, actor, now,
    )
    return await fetch_one(
        db,
        f"""
        UPDATE {T_EQUIPMENTS}
        SET current_state = $1, state_since = $2, state_reason = $3, state_remark = $4,
            current_lot_id = $5, updated_at = $2, updated_by = $6
        WHERE eq_id = $7 RETURNING *
        """,
        state.value, now, reason_code or remark, remark, lot_id, actor, eq_id,
    )


async def state_history(db, eq_id: str, limit: int = 100) -> list[dict]:
    return await fetch_all(
        db,
        f"SELECT * FROM {T_EQUIPMENT_LOGS} WHERE eq_id = $1 ORDER BY start_time DESC, id DESC LIMIT $2",
        eq_id, limit,
    )


async def state_summary(db, area: str | None = None) -> dict:
    """機台狀態即時分佈，供戰情看板使用。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT current_state AS state, count(*) AS count,
               array_agg(eq_id ORDER BY eq_id) AS equipments
        FROM {T_EQUIPMENTS}
        WHERE active AND ($1::text IS NULL OR area = $1)
        GROUP BY current_state
        """,
        area,
    )
    by_state = {r["state"]: {"count": r["count"], "equipments": r["equipments"]} for r in rows}
    total = sum(v["count"] for v in by_state.values())
    up = sum(v["count"] for s, v in by_state.items() if s in UPTIME_VALUES)
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
    start, end = ensure_aware(start), ensure_aware(end)

    # 1) 依 E10 狀態彙總各段時間（只計落在區間內的部分）
    rows = await fetch_all(
        db,
        f"""
        SELECT state,
               sum(EXTRACT(EPOCH FROM (
                   LEAST(COALESCE(end_time, $3), $3) - GREATEST(start_time, $2)
               ))) AS seconds
        FROM {T_EQUIPMENT_LOGS}
        WHERE eq_id = $1
          AND start_time < $3
          AND (end_time IS NULL OR end_time > $2)
        GROUP BY state
        """,
        eq_id, start, end,
    )
    seconds = {s.value: 0.0 for s in EquipmentState}
    for row in rows:
        seconds[row["state"]] = seconds.get(row["state"], 0.0) + float(row["seconds"] or 0.0)

    total_sec = (end - start).total_seconds()
    non_scheduled = sum(seconds[s] for s in NON_SCHEDULED_VALUES)
    covered = sum(seconds.values())
    if covered < total_sec:
        non_scheduled += total_sec - covered  # 未涵蓋的時間視為非排程（例如設備建檔前）
    scheduled = max(0.0, total_sec - non_scheduled)
    productive = seconds[EquipmentState.PRODUCTIVE.value]
    uptime = sum(seconds[s] for s in UPTIME_VALUES)

    # 2) 產出：本區間內於此設備 Track-Out 的數量
    agg = await fetch_one(
        db,
        f"""
        SELECT COALESCE(sum(qty_good), 0)   AS good,
               COALESCE(sum(qty_reject), 0) AS reject,
               count(*)                     AS lots
        FROM {T_LOT_HISTORY}
        WHERE eq_id = $1 AND action = $2 AND timestamp >= $3 AND timestamp < $4
        """,
        eq_id, LotAction.TRACK_OUT.value, start, end,
    )
    good, reject = int(agg["good"]), int(agg["reject"])
    processed = good + reject

    ideal_ct = float(eq["ideal_cycle_time_sec"] or 1.0)
    availability = productive / scheduled if scheduled else 0.0
    performance = (ideal_ct * processed) / productive if productive else 0.0
    performance = min(performance, 1.0)  # 超過 100% 視為標準工時需重新校正
    quality = good / processed if processed else 0.0

    return {
        "eq_id": eq_id,
        "name": eq["name"],
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
    rows = await fetch_all(
        db,
        f"SELECT eq_id FROM {T_EQUIPMENTS} WHERE active AND ($1::text IS NULL OR area = $1) ORDER BY eq_id",
        area,
    )
    return [await calc_oee(db, r["eq_id"], start, end) for r in rows]


# ── PM 保養 ─────────────────────────────────────────────────
async def create_pm(db, eq_id: str, pm_type: str, due_date: datetime, actor: str, remark: str = "") -> dict:
    await get_equipment(db, eq_id)
    return await fetch_one(
        db,
        f"""
        INSERT INTO {T_PM_TASKS} (eq_id, pm_type, due_date, status, remark, created_by)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
        """,
        eq_id, pm_type, due_date, PMStatus.PLANNED.value, remark, actor,
    )


async def list_pm(db, eq_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    rows = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_PM_TASKS}
        WHERE ($1::text IS NULL OR eq_id = $1) AND ($2::text IS NULL OR status = $2)
        ORDER BY due_date ASC LIMIT $3
        """,
        eq_id, status, limit,
    )
    now = utcnow()
    for row in rows:
        if row["status"] == PMStatus.PLANNED.value and ensure_aware(row["due_date"]) < now:
            row["status"] = PMStatus.OVERDUE.value
    return rows


async def complete_pm(db, pm_id: int, actor: str, remark: str = "") -> dict:
    try:
        pm_id = int(pm_id)
    except (TypeError, ValueError):
        raise ValidationError(f"PM 單號格式錯誤：{pm_id}")

    async with db.transaction():
        pm = await fetch_one(db, f"SELECT * FROM {T_PM_TASKS} WHERE id = $1 FOR UPDATE", pm_id)
        if pm is None:
            raise NotFoundError(f"找不到 PM 工單：{pm_id}")
        if pm["status"] == PMStatus.DONE.value:
            raise StateError("此 PM 工單已完成")

        done = await fetch_one(
            db,
            f"""
            UPDATE {T_PM_TASKS}
            SET status = $1, done_at = $2, performed_by = $3, remark = $4
            WHERE id = $5 RETURNING *
            """,
            PMStatus.DONE.value, utcnow(), actor, remark or pm["remark"], pm_id,
        )
        # 保養完成 → 依保養週期自動排下一次
        eq = await get_equipment(db, pm["eq_id"])
        interval = int(eq["pm_interval_days"] or 0)
        if interval > 0:
            await create_pm(
                db, pm["eq_id"], pm["pm_type"], utcnow() + timedelta(days=interval), actor, "系統自動排程"
            )
        return done
