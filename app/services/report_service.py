"""報表服務：WIP、良率、產出、週期時間、Q-Time、交接班、戰情看板。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.database import (
    T_DEFECT_RECORDS,
    T_EQUIPMENT_LOGS,
    T_HOLDS,
    T_LOT_HISTORY,
    T_LOTS,
    T_OPERATIONS,
    T_WORK_ORDERS,
    fetch_all,
    fetch_one,
)
from app.models.base import ensure_aware, shift_of, shift_window, utcnow
from app.models.enums import (
    ACTIVE_LOT_STATUSES,
    EquipmentState,
    LotAction,
    LotStatus,
    WorkOrderStatus,
)
from app.services import equipment_service, quality_service

WIP_STATUSES = [s.value for s in ACTIVE_LOT_STATUSES]
TRACK_OUT = LotAction.TRACK_OUT.value


# ── WIP ─────────────────────────────────────────────────────
async def wip_by_operation(db, device_id: str | None = None, customer_code: str | None = None) -> dict:
    """各站在製量 —— 生管每天最先看的一張表。"""
    items = await fetch_all(
        db,
        f"""
        SELECT l.current_seq AS seq, l.current_op AS op_code,
               COALESCE(max(o.name), '') AS op_name,
               count(*) AS lots, COALESCE(sum(l.qty), 0) AS qty,
               count(*) FILTER (WHERE l.status = 'WAITING') AS waiting,
               count(*) FILTER (WHERE l.status = 'RUNNING') AS running,
               count(*) FILTER (WHERE l.status = 'HOLD')    AS hold
        FROM {T_LOTS} l
        LEFT JOIN {T_OPERATIONS} o ON o.op_code = l.current_op
        WHERE l.status = ANY($1::text[])
          AND ($2::text IS NULL OR l.device_id = $2)
          AND ($3::text IS NULL OR l.customer_code = $3)
        GROUP BY l.current_seq, l.current_op
        ORDER BY l.current_seq
        """,
        WIP_STATUSES, device_id, customer_code,
    )
    for row in items:
        row["qty"] = int(row["qty"])
    return {
        "total_lots": sum(i["lots"] for i in items),
        "total_qty": sum(i["qty"] for i in items),
        "items": items,
    }


async def wip_aging(db, buckets_hours: tuple[int, ...] = (24, 72, 168)) -> dict:
    """在製品停留時間分佈，找出躺太久的批號。"""
    now = utcnow()
    lots = await fetch_all(
        db,
        f"""
        SELECT lot_id, device_id, current_op, qty, status, priority,
               COALESCE(last_track_out_at, created_at) AS since
        FROM {T_LOTS} WHERE status = ANY($1::text[])
        """,
        WIP_STATUSES,
    )

    labels = [f"<{buckets_hours[0]}h"]
    labels += [f"{buckets_hours[i - 1]}-{buckets_hours[i]}h" for i in range(1, len(buckets_hours))]
    labels.append(f">{buckets_hours[-1]}h")
    dist = {label: 0 for label in labels}
    aged: list[dict] = []

    for lot in lots:
        hours = (now - ensure_aware(lot.pop("since"))).total_seconds() / 3600
        idx = len(buckets_hours)
        for i, edge in enumerate(buckets_hours):
            if hours < edge:
                idx = i
                break
        dist[labels[idx]] += 1
        lot["aging_hours"] = round(hours, 1)
        if idx == len(buckets_hours):
            aged.append(lot)

    aged.sort(key=lambda x: -x["aging_hours"])
    return {"distribution": dist, "aged_lots": aged[:50], "total_wip_lots": len(lots)}


# ── 良率 ────────────────────────────────────────────────────
async def yield_by_operation(
    db,
    start: datetime,
    end: datetime,
    device_id: str | None = None,
    route_code: str | None = None,
) -> dict:
    """各站良率。

    累計良率只有在資料屬於「同一條製程流程」時才成立 —— 不同流程的站別代碼
    會重疊（例如導線架與基板流程都有 FT），硬串起來相乘沒有物理意義。
    因此混流時 ``cumulative_yield`` 一律回傳 None，請改用
    :func:`final_yield_by_route` 或加上 device_id / route_code 篩選。
    """
    rows = await fetch_all(
        db,
        f"""
        SELECT seq, op_code,
               COALESCE(sum(qty_expected), 0) AS qty_expected,
               COALESCE(sum(qty_good), 0)     AS qty_good,
               COALESCE(sum(qty_reject), 0)   AS qty_reject,
               count(*) AS lots,
               array_agg(DISTINCT route_code) AS routes
        FROM {T_LOT_HISTORY}
        WHERE action = $1 AND timestamp >= $2 AND timestamp < $3
          AND ($4::text IS NULL OR device_id = $4)
          AND ($5::text IS NULL OR route_code = $5)
        GROUP BY seq, op_code
        ORDER BY seq
        """,
        TRACK_OUT, start, end, device_id, route_code,
    )

    routes = sorted({r for row in rows for r in (row["routes"] or []) if r})
    single_route = len(routes) <= 1

    items, cumulative = [], 1.0
    for r in rows:
        expected = int(r["qty_expected"])
        good = int(r["qty_good"])
        step_yield = good / expected if expected else 0.0
        cumulative *= step_yield if expected else 1.0
        items.append({
            "seq": r["seq"],
            "op_code": r["op_code"],
            "lots": r["lots"],
            "qty_expected": expected,
            "qty_good": good,
            "qty_reject": int(r["qty_reject"]),
            "step_yield": round(step_yield, 4),
            "cumulative_yield": round(cumulative, 4) if single_route else None,
        })
    return {
        "window": {"start": start, "end": end},
        "items": items,
        "routes": routes,
        "final_yield": round(cumulative, 4) if single_route else None,
    }


async def final_yield_by_route(db, start: datetime, end: datetime) -> list[dict]:
    """各製程流程各自的累計良率（站良率連乘），混流時的正確算法。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT COALESCE(route_code, '(未指定流程)') AS route_code, seq,
               COALESCE(sum(qty_expected), 0) AS qty_expected,
               COALESCE(sum(qty_good), 0)     AS qty_good
        FROM {T_LOT_HISTORY}
        WHERE action = $1 AND timestamp >= $2 AND timestamp < $3
        GROUP BY route_code, seq
        ORDER BY seq
        """,
        TRACK_OUT, start, end,
    )
    chains: dict[str, dict] = {}
    for r in rows:
        entry = chains.setdefault(r["route_code"], {"route_code": r["route_code"], "final_yield": 1.0, "steps": 0})
        expected = int(r["qty_expected"])
        if expected:
            entry["final_yield"] *= int(r["qty_good"]) / expected
            entry["steps"] += 1
    return sorted(
        ({**v, "final_yield": round(v["final_yield"], 4)} for v in chains.values()),
        key=lambda x: x["final_yield"],
    )


async def yield_by_device(db, start: datetime, end: datetime, top_n: int = 20) -> list[dict]:
    rows = await fetch_all(
        db,
        f"""
        SELECT device_id, max(customer_code) AS customer_code,
               COALESCE(sum(qty_expected), 0) AS qty_expected,
               COALESCE(sum(qty_good), 0)     AS qty_good,
               COALESCE(sum(qty_reject), 0)   AS qty_reject,
               count(*) AS moves
        FROM {T_LOT_HISTORY}
        WHERE action = $1 AND timestamp >= $2 AND timestamp < $3
        GROUP BY device_id
        ORDER BY qty_reject DESC
        LIMIT $4
        """,
        TRACK_OUT, start, end, top_n,
    )
    return [
        {
            "device_id": r["device_id"],
            "customer_code": r["customer_code"],
            "moves": r["moves"],
            "qty_expected": int(r["qty_expected"]),
            "qty_good": int(r["qty_good"]),
            "qty_reject": int(r["qty_reject"]),
            "yield": round(int(r["qty_good"]) / int(r["qty_expected"]), 4) if r["qty_expected"] else 0.0,
        }
        for r in rows
    ]


# ── 產出與週期時間 ──────────────────────────────────────────
async def throughput(db, start: datetime, end: datetime, group_by: str = "shift") -> list[dict]:
    """產出（Moves）統計，可依班別、站別或料號彙總。"""
    column = {"shift": "shift", "operation": "op_code", "device": "device_id"}.get(group_by, "shift")
    rows = await fetch_all(
        db,
        f"""
        SELECT {column} AS key, count(*) AS moves,
               COALESCE(sum(qty_good), 0)   AS qty_good,
               COALESCE(sum(qty_reject), 0) AS qty_reject
        FROM {T_LOT_HISTORY}
        WHERE action = $1 AND timestamp >= $2 AND timestamp < $3
        GROUP BY {column}
        ORDER BY {column}
        """,
        TRACK_OUT, start, end,
    )
    return [
        {
            "key": r["key"],
            "moves": r["moves"],
            "qty_good": int(r["qty_good"]),
            "qty_reject": int(r["qty_reject"]),
            "yield": round(int(r["qty_good"]) / (int(r["qty_good"]) + int(r["qty_reject"])), 4)
            if (int(r["qty_good"]) + int(r["qty_reject"])) else 0.0,
        }
        for r in rows
    ]


async def cycle_time_by_operation(db, start: datetime, end: datetime) -> list[dict]:
    """各站加工／等待時間，找瓶頸站。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT out.seq, out.op_code, out.moves, out.avg_process_sec, out.max_process_sec,
               out.std_sec, COALESCE(q.avg_queue_sec, 0) AS avg_queue_sec
        FROM (
            SELECT seq, op_code, count(*) AS moves,
                   avg(process_sec) AS avg_process_sec,
                   max(process_sec) AS max_process_sec,
                   avg(standard_cycle_time_sec) AS std_sec
            FROM {T_LOT_HISTORY}
            WHERE action = $1 AND timestamp >= $2 AND timestamp < $3
            GROUP BY seq, op_code
        ) out
        LEFT JOIN (
            SELECT op_code, avg(queue_sec) AS avg_queue_sec
            FROM {T_LOT_HISTORY}
            WHERE action = $4 AND timestamp >= $2 AND timestamp < $3
            GROUP BY op_code
        ) q ON q.op_code = out.op_code
        ORDER BY out.seq
        """,
        TRACK_OUT, start, end, LotAction.TRACK_IN.value,
    )
    items = []
    for r in rows:
        avg = float(r["avg_process_sec"] or 0.0)
        std = float(r["std_sec"] or 0.0)
        queue = float(r["avg_queue_sec"] or 0.0)
        items.append({
            "seq": r["seq"],
            "op_code": r["op_code"],
            "moves": r["moves"],
            "avg_process_sec": round(avg, 1),
            "max_process_sec": round(float(r["max_process_sec"] or 0.0), 1),
            "standard_cycle_time_sec": round(std, 1),
            "vs_standard": round(avg / std, 3) if std else None,
            "avg_queue_sec": round(queue, 1),
            "avg_total_sec": round(avg + queue, 1),
        })
    return items


async def qtime_violations(db, start: datetime, end: datetime, limit: int = 100) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_LOT_HISTORY}
        WHERE qtime_violation AND timestamp >= $1 AND timestamp < $2
        ORDER BY timestamp DESC, id DESC LIMIT $3
        """,
        start, end, limit,
    )


# ── 交接班 ──────────────────────────────────────────────────
async def shift_handover(db, shift: str | None = None) -> dict:
    """交接班報表：這一班做了多少、出了什麼事、下一班要接手什麼。"""
    from app.services import dispatch_service, spc_service, tool_service

    label = shift or shift_of(utcnow())
    start, end = shift_window(label)

    agg = await fetch_one(
        db,
        f"""
        SELECT count(*) AS moves, count(DISTINCT lot_id) AS lots,
               COALESCE(sum(qty_good), 0) AS good, COALESCE(sum(qty_reject), 0) AS reject
        FROM {T_LOT_HISTORY} WHERE action = $1 AND shift = $2
        """,
        TRACK_OUT, label,
    )
    good, reject = int(agg["good"]), int(agg["reject"])

    new_holds = await fetch_all(
        db, f"SELECT * FROM {T_HOLDS} WHERE held_at >= $1 AND held_at < $2 ORDER BY held_at", start, end
    )
    downtime = await fetch_all(
        db,
        f"""
        SELECT eq_id, state, count(*) AS events,
               COALESCE(sum(duration_sec), 0) AS seconds,
               array_agg(DISTINCT reason_code) FILTER (WHERE reason_code <> '') AS reasons
        FROM {T_EQUIPMENT_LOGS}
        WHERE state = ANY($1::text[]) AND start_time >= $2 AND start_time < $3
        GROUP BY eq_id, state
        ORDER BY seconds DESC
        """,
        [EquipmentState.UNSCHEDULED_DOWN.value, EquipmentState.SCHEDULED_DOWN.value], start, end,
    )

    spc_issues = await spc_service.violation_summary(db, start, end)
    qtime = await dispatch_service.qtime_watch(db)
    tools_to_change = await tool_service.attention_list(db)
    dispatch = await dispatch_service.dispatch_list(db, limit=10)

    return {
        "shift": label,
        "window": {"start": start, "end": end},
        "output": {
            "moves": agg["moves"],
            "lots_processed": agg["lots"],
            "qty_good": good,
            "qty_reject": reject,
            "yield": round(good / (good + reject), 4) if (good + reject) else 0.0,
        },
        "new_holds": new_holds,
        "equipment_downtime": [
            {
                "eq_id": r["eq_id"],
                "state": r["state"],
                "events": r["events"],
                "minutes": round(float(r["seconds"]) / 60, 1),
                "reasons": sorted(r["reasons"] or []),
            }
            for r in downtime
        ],
        "spc_violations": spc_issues,
        "qtime_watch": {
            "expired": qtime["expired_count"],
            "at_risk": qtime["at_risk_count"],
            "items": qtime["items"][:10],
        },
        "tools_to_change": tools_to_change[:10],
        "next_up": dispatch["items"],
    }


# ── 戰情看板 ────────────────────────────────────────────────
async def dashboard(db, hours: int = 24) -> dict:
    """單一 API 餵完整個看板，減少前端往返。"""
    from app.services import dispatch_service, spc_service, tool_service

    now = utcnow()
    start = now - timedelta(hours=hours)

    wip = await wip_by_operation(db)
    holds = await quality_service.open_hold_summary(db)
    eq_states = await equipment_service.state_summary(db)
    moves = await throughput(db, start, now, group_by="shift")
    yields = await yield_by_operation(db, start, now)
    route_yields = await final_yield_by_route(db, start, now)
    pareto = await quality_service.defect_pareto(db, start, now, top_n=8)
    aging = await wip_aging(db)
    qtime = await dispatch_service.qtime_watch(db)
    spc_issues = await spc_service.violation_summary(db, start, now)
    tools_to_change = await tool_service.attention_list(db)

    lot_status = await fetch_all(
        db,
        f"SELECT status, count(*) AS lots, COALESCE(sum(qty), 0) AS qty FROM {T_LOTS} GROUP BY status ORDER BY status",
    )
    open_wo = await db.fetchval(
        f"SELECT count(*) FROM {T_WORK_ORDERS} WHERE status = ANY($1::text[])",
        [WorkOrderStatus.RELEASED.value, WorkOrderStatus.IN_PROGRESS.value],
    )
    violations = await db.fetchval(
        f"SELECT count(*) FROM {T_LOT_HISTORY} WHERE qtime_violation AND timestamp >= $1 AND timestamp < $2",
        start, now,
    )
    defect_qty = await db.fetchval(
        f"SELECT COALESCE(sum(qty), 0) FROM {T_DEFECT_RECORDS} WHERE timestamp >= $1 AND timestamp < $2",
        start, now,
    )

    total_good = sum(m["qty_good"] for m in moves)
    total_reject = sum(m["qty_reject"] for m in moves)

    return {
        "generated_at": now,
        "window_hours": hours,
        "kpi": {
            "wip_lots": wip["total_lots"],
            "wip_qty": wip["total_qty"],
            "open_work_orders": open_wo,
            "lots_on_hold": holds["total"],
            "moves": sum(m["moves"] for m in moves),
            "output_good": total_good,
            "output_reject": total_reject,
            "overall_yield": round(total_good / (total_good + total_reject), 4)
            if (total_good + total_reject) else 0.0,
            # 混流時單一累計良率沒有意義，取表現最差的流程當警示指標
            "worst_route_yield": route_yields[0]["final_yield"] if route_yields else None,
            "equipment_uptime_ratio": eq_states["uptime_ratio"],
            "qtime_violations": violations,
            "qtime_at_risk": qtime["expired_count"] + qtime["at_risk_count"],
            "spc_violations": sum(i["violations"] for i in spc_issues),
            "tools_to_change": len(tools_to_change),
            "defect_qty": int(defect_qty or 0),
        },
        "wip_by_operation": wip["items"],
        "lot_status": [
            {"status": r["status"], "lots": r["lots"], "qty": int(r["qty"])} for r in lot_status
        ],
        "equipment_states": eq_states,
        "throughput_by_shift": moves,
        "yield_by_operation": yields["items"],
        "final_yield_by_route": route_yields,
        "defect_pareto": pareto["items"],
        "hold_summary": holds["by_reason"],
        "wip_aging": aging["distribution"],
        "aged_lots": aging["aged_lots"][:10],
        "qtime_watch": qtime["items"][:10],
        "spc_violations": spc_issues[:8],
        "tools_to_change": tools_to_change[:8],
    }
