"""報表服務：WIP、良率、產出、週期時間、Q-Time、戰情看板彙總。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.database import (
    COL_DEFECT_RECORDS,
    COL_EQUIPMENT_LOGS,
    COL_HOLDS,
    COL_LOT_HISTORY,
    COL_LOTS,
    COL_OPERATIONS,
    COL_WORK_ORDERS,
)
from app.models.base import clean_all, ensure_aware, shift_of, shift_window, utcnow
from app.models.enums import (
    ACTIVE_LOT_STATUSES,
    EquipmentState,
    LotAction,
    LotStatus,
    WorkOrderStatus,
)
from app.services import equipment_service, quality_service

WIP_STATUSES = [s.value for s in ACTIVE_LOT_STATUSES]


# ── WIP ─────────────────────────────────────────────────────
async def wip_by_operation(db, device_id: str | None = None, customer_code: str | None = None) -> dict:
    """各站在製量 —— 生管每天最先看的一張表。"""
    match: dict = {"status": {"$in": WIP_STATUSES}}
    if device_id:
        match["device_id"] = device_id
    if customer_code:
        match["customer_code"] = customer_code

    rows = await db[COL_LOTS].aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": {"seq": "$current_seq", "op": "$current_op"},
                    "lots": {"$sum": 1},
                    "qty": {"$sum": "$qty"},
                    "waiting": {"$sum": {"$cond": [{"$eq": ["$status", LotStatus.WAITING.value]}, 1, 0]}},
                    "running": {"$sum": {"$cond": [{"$eq": ["$status", LotStatus.RUNNING.value]}, 1, 0]}},
                    "hold": {"$sum": {"$cond": [{"$eq": ["$status", LotStatus.HOLD.value]}, 1, 0]}},
                }
            },
            {"$sort": {"_id.seq": 1}},
        ]
    ).to_list(length=None)

    op_names = {
        o["op_code"]: o.get("name", "")
        for o in await db[COL_OPERATIONS].find({}, {"_id": 0, "op_code": 1, "name": 1}).to_list(length=None)
    }
    items = [
        {
            "seq": r["_id"]["seq"],
            "op_code": r["_id"]["op"],
            "op_name": op_names.get(r["_id"]["op"], ""),
            "lots": r["lots"],
            "qty": r["qty"],
            "waiting": r["waiting"],
            "running": r["running"],
            "hold": r["hold"],
        }
        for r in rows
    ]
    return {
        "total_lots": sum(i["lots"] for i in items),
        "total_qty": sum(i["qty"] for i in items),
        "items": items,
    }


async def wip_aging(db, buckets_hours: tuple[int, ...] = (24, 72, 168)) -> dict:
    """在製品停留時間分佈，找出躺太久的批號。"""
    now = utcnow()
    lots = await db[COL_LOTS].find(
        {"status": {"$in": WIP_STATUSES}},
        {"_id": 0, "lot_id": 1, "device_id": 1, "current_op": 1, "qty": 1, "status": 1,
         "last_track_out_at": 1, "created_at": 1, "priority": 1},
    ).to_list(length=None)

    labels = [f"<{buckets_hours[0]}h"]
    labels += [f"{buckets_hours[i - 1]}-{buckets_hours[i]}h" for i in range(1, len(buckets_hours))]
    labels.append(f">{buckets_hours[-1]}h")
    dist = {label: 0 for label in labels}
    aged: list[dict] = []

    for lot in lots:
        since = ensure_aware(lot.get("last_track_out_at") or lot["created_at"])
        hours = (now - since).total_seconds() / 3600
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
    match: dict = {
        "action": LotAction.TRACK_OUT.value,
        "timestamp": {"$gte": start, "$lt": end},
    }
    if device_id:
        match["device_id"] = device_id
    if route_code:
        match["route_code"] = route_code

    rows = await db[COL_LOT_HISTORY].aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": {"seq": "$seq", "op": "$op_code"},
                    "qty_expected": {"$sum": "$qty_expected"},
                    "qty_good": {"$sum": "$qty_good"},
                    "qty_reject": {"$sum": "$qty_reject"},
                    "lots": {"$sum": 1},
                    "routes": {"$addToSet": "$route_code"},
                }
            },
            {"$sort": {"_id.seq": 1}},
        ]
    ).to_list(length=None)

    routes = sorted({r for row in rows for r in (row.get("routes") or []) if r})
    single_route = len(routes) <= 1

    items, cumulative = [], 1.0
    for r in rows:
        expected = r["qty_expected"] or 0
        step_yield = r["qty_good"] / expected if expected else 0.0
        cumulative *= step_yield if expected else 1.0
        items.append(
            {
                "seq": r["_id"]["seq"],
                "op_code": r["_id"]["op"],
                "lots": r["lots"],
                "qty_expected": expected,
                "qty_good": r["qty_good"],
                "qty_reject": r["qty_reject"],
                "step_yield": round(step_yield, 4),
                "cumulative_yield": round(cumulative, 4) if single_route else None,
            }
        )
    return {
        "window": {"start": start, "end": end},
        "items": items,
        "routes": routes,
        "final_yield": round(cumulative, 4) if single_route else None,
    }


async def final_yield_by_route(db, start: datetime, end: datetime) -> list[dict]:
    """各製程流程各自的累計良率（站良率連乘），混流時的正確算法。"""
    rows = await db[COL_LOT_HISTORY].aggregate(
        [
            {"$match": {"action": LotAction.TRACK_OUT.value, "timestamp": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": {"route": "$route_code", "seq": "$seq", "op": "$op_code"},
                    "qty_expected": {"$sum": "$qty_expected"},
                    "qty_good": {"$sum": "$qty_good"},
                }
            },
            {"$sort": {"_id.seq": 1}},
        ]
    ).to_list(length=None)

    chains: dict[str, dict] = {}
    for r in rows:
        route = r["_id"]["route"] or "(未指定流程)"
        entry = chains.setdefault(route, {"route_code": route, "final_yield": 1.0, "steps": 0})
        expected = r["qty_expected"] or 0
        if expected:
            entry["final_yield"] *= r["qty_good"] / expected
            entry["steps"] += 1
    return sorted(
        ({**v, "final_yield": round(v["final_yield"], 4)} for v in chains.values()),
        key=lambda x: x["final_yield"],
    )


async def yield_by_device(db, start: datetime, end: datetime, top_n: int = 20) -> list[dict]:
    rows = await db[COL_LOT_HISTORY].aggregate(
        [
            {"$match": {"action": LotAction.TRACK_OUT.value, "timestamp": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": {"device": "$device_id", "customer": "$customer_code"},
                    "qty_expected": {"$sum": "$qty_expected"},
                    "qty_good": {"$sum": "$qty_good"},
                    "qty_reject": {"$sum": "$qty_reject"},
                    "moves": {"$sum": 1},
                }
            },
            {"$sort": {"qty_reject": -1}},
            {"$limit": top_n},
        ]
    ).to_list(length=top_n)
    return [
        {
            "device_id": r["_id"]["device"],
            "customer_code": r["_id"]["customer"],
            "moves": r["moves"],
            "qty_expected": r["qty_expected"],
            "qty_good": r["qty_good"],
            "qty_reject": r["qty_reject"],
            "yield": round(r["qty_good"] / r["qty_expected"], 4) if r["qty_expected"] else 0.0,
        }
        for r in rows
    ]


# ── 產出與週期時間 ──────────────────────────────────────────
async def throughput(db, start: datetime, end: datetime, group_by: str = "shift") -> list[dict]:
    """產出（Moves）統計，可依班別或站別彙總。"""
    key = {"shift": "$shift", "operation": "$op_code", "device": "$device_id"}.get(group_by, "$shift")
    rows = await db[COL_LOT_HISTORY].aggregate(
        [
            {"$match": {"action": LotAction.TRACK_OUT.value, "timestamp": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": key,
                    "moves": {"$sum": 1},
                    "qty_good": {"$sum": "$qty_good"},
                    "qty_reject": {"$sum": "$qty_reject"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
    ).to_list(length=None)
    return [
        {
            "key": r["_id"],
            "moves": r["moves"],
            "qty_good": r["qty_good"],
            "qty_reject": r["qty_reject"],
            "yield": round(r["qty_good"] / (r["qty_good"] + r["qty_reject"]), 4)
            if (r["qty_good"] + r["qty_reject"])
            else 0.0,
        }
        for r in rows
    ]


async def cycle_time_by_operation(db, start: datetime, end: datetime) -> list[dict]:
    """各站加工／等待時間，找瓶頸站。"""
    rows = await db[COL_LOT_HISTORY].aggregate(
        [
            {"$match": {"action": LotAction.TRACK_OUT.value, "timestamp": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": {"seq": "$seq", "op": "$op_code"},
                    "avg_process_sec": {"$avg": "$process_sec"},
                    "max_process_sec": {"$max": "$process_sec"},
                    "std_sec": {"$avg": "$standard_cycle_time_sec"},
                    "moves": {"$sum": 1},
                }
            },
            {"$sort": {"_id.seq": 1}},
        ]
    ).to_list(length=None)

    queue_rows = await db[COL_LOT_HISTORY].aggregate(
        [
            {"$match": {"action": LotAction.TRACK_IN.value, "timestamp": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": "$op_code", "avg_queue_sec": {"$avg": "$queue_sec"}}},
        ]
    ).to_list(length=None)
    queue = {r["_id"]: r["avg_queue_sec"] for r in queue_rows}

    items = []
    for r in rows:
        op = r["_id"]["op"]
        avg = r["avg_process_sec"] or 0.0
        std = r["std_sec"] or 0.0
        items.append(
            {
                "seq": r["_id"]["seq"],
                "op_code": op,
                "moves": r["moves"],
                "avg_process_sec": round(avg, 1),
                "max_process_sec": round(r["max_process_sec"] or 0.0, 1),
                "standard_cycle_time_sec": round(std, 1),
                "vs_standard": round(avg / std, 3) if std else None,
                "avg_queue_sec": round(queue.get(op, 0.0), 1),
                "avg_total_sec": round(avg + queue.get(op, 0.0), 1),
            }
        )
    return items


async def qtime_violations(db, start: datetime, end: datetime, limit: int = 100) -> list[dict]:
    cursor = (
        db[COL_LOT_HISTORY]
        .find({"qtime_violation": True, "timestamp": {"$gte": start, "$lt": end}})
        .sort([("timestamp", -1)])
        .limit(limit)
    )
    return clean_all(await cursor.to_list(length=limit))


# ── 交接班 ──────────────────────────────────────────────────
async def shift_handover(db, shift: str | None = None) -> dict:
    """交接班報表：這一班做了多少、出了什麼事、下一班要接手什麼。"""
    from app.services import dispatch_service, spc_service, tool_service

    label = shift or shift_of(utcnow())
    start, end = shift_window(label)

    output = await db[COL_LOT_HISTORY].aggregate(
        [
            {"$match": {"action": LotAction.TRACK_OUT.value, "shift": label}},
            {
                "$group": {
                    "_id": None,
                    "moves": {"$sum": 1},
                    "good": {"$sum": "$qty_good"},
                    "reject": {"$sum": "$qty_reject"},
                    "lots": {"$addToSet": "$lot_id"},
                }
            },
        ]
    ).to_list(length=1)
    agg = output[0] if output else {"moves": 0, "good": 0, "reject": 0, "lots": []}

    new_holds = clean_all(
        await db[COL_HOLDS].find({"held_at": {"$gte": start, "$lt": end}}).to_list(length=None)
    )
    downtime = await db[COL_EQUIPMENT_LOGS].aggregate(
        [
            {
                "$match": {
                    "state": {"$in": [EquipmentState.UNSCHEDULED_DOWN.value, EquipmentState.SCHEDULED_DOWN.value]},
                    "start_time": {"$gte": start, "$lt": end},
                }
            },
            {
                "$group": {
                    "_id": {"eq": "$eq_id", "state": "$state"},
                    "events": {"$sum": 1},
                    "seconds": {"$sum": {"$ifNull": ["$duration_sec", 0]}},
                    "reasons": {"$addToSet": "$reason_code"},
                }
            },
            {"$sort": {"seconds": -1}},
        ]
    ).to_list(length=None)

    spc_issues = await spc_service.violation_summary(db, start, end)
    qtime = await dispatch_service.qtime_watch(db)
    tools_to_change = await tool_service.attention_list(db)
    dispatch = await dispatch_service.dispatch_list(db, limit=10)

    return {
        "shift": label,
        "window": {"start": start, "end": end},
        "output": {
            "moves": agg["moves"],
            "lots_processed": len(agg.get("lots") or []),
            "qty_good": agg["good"],
            "qty_reject": agg["reject"],
            "yield": round(agg["good"] / (agg["good"] + agg["reject"]), 4)
            if (agg["good"] + agg["reject"]) else 0.0,
        },
        "new_holds": new_holds,
        "equipment_downtime": [
            {
                "eq_id": r["_id"]["eq"],
                "state": r["_id"]["state"],
                "events": r["events"],
                "minutes": round(r["seconds"] / 60, 1),
                "reasons": sorted(x for x in r["reasons"] if x),
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
    now = utcnow()
    start = now - timedelta(hours=hours)

    wip = await wip_by_operation(db)
    holds = await quality_service.open_hold_summary(db)
    eq_states = await equipment_service.state_summary(db)
    moves = await throughput(db, start, now, group_by="shift")
    yields = await yield_by_operation(db, start, now)
    route_yields = await final_yield_by_route(db, start, now)

    from app.services import dispatch_service, spc_service, tool_service

    qtime = await dispatch_service.qtime_watch(db)
    spc_issues = await spc_service.violation_summary(db, start, now)
    tools_to_change = await tool_service.attention_list(db)
    pareto = await quality_service.defect_pareto(db, start, now, top_n=8)
    aging = await wip_aging(db)

    lot_status_rows = await db[COL_LOTS].aggregate(
        [{"$group": {"_id": "$status", "lots": {"$sum": 1}, "qty": {"$sum": "$qty"}}}]
    ).to_list(length=None)

    open_wo = await db[COL_WORK_ORDERS].count_documents(
        {"status": {"$in": [WorkOrderStatus.RELEASED.value, WorkOrderStatus.IN_PROGRESS.value]}}
    )
    violations = await db[COL_LOT_HISTORY].count_documents(
        {"qtime_violation": True, "timestamp": {"$gte": start, "$lt": now}}
    )
    defect_qty = await db[COL_DEFECT_RECORDS].aggregate(
        [
            {"$match": {"timestamp": {"$gte": start, "$lt": now}}},
            {"$group": {"_id": None, "qty": {"$sum": "$qty"}}},
        ]
    ).to_list(length=1)

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
            if (total_good + total_reject)
            else 0.0,
            # 混流時單一累計良率沒有意義，取表現最差的流程當警示指標
            "worst_route_yield": route_yields[0]["final_yield"] if route_yields else None,
            "equipment_uptime_ratio": eq_states["uptime_ratio"],
            "qtime_violations": violations,
            "qtime_at_risk": qtime["expired_count"] + qtime["at_risk_count"],
            "spc_violations": sum(i["violations"] for i in spc_issues),
            "tools_to_change": len(tools_to_change),
            "defect_qty": (defect_qty[0]["qty"] if defect_qty else 0),
        },
        "wip_by_operation": wip["items"],
        "lot_status": [
            {"status": r["_id"], "lots": r["lots"], "qty": r["qty"]} for r in lot_status_rows
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
