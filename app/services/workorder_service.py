"""工單服務。"""

from __future__ import annotations

from app.database import T_DEVICES, T_LOTS, T_WORK_ORDERS, fetch_all, fetch_one, next_sequence
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import plain_values, to_local, utcnow
from app.models.enums import ACTIVE_LOT_STATUSES, WorkOrderStatus

ACTIVE_STATUS_VALUES = [s.value for s in ACTIVE_LOT_STATUSES]


async def _gen_wo_no(db) -> str:
    ym = f"{to_local(utcnow()):%y%m}"
    return f"WO{ym}{await next_sequence(db, f'wo:{ym}'):04d}"


async def create_work_order(db, payload: dict, actor: str) -> dict:
    device = await fetch_one(db, f"SELECT * FROM {T_DEVICES} WHERE device_id = $1", payload["device_id"])
    if device is None:
        raise ValidationError(f"產品料號不存在：{payload['device_id']}")
    if not device["active"]:
        raise ValidationError(f"產品料號已停用：{payload['device_id']}")

    data = plain_values(payload)
    wo_no = await _gen_wo_no(db)
    return await fetch_one(
        db,
        f"""
        INSERT INTO {T_WORK_ORDERS}
            (wo_no, device_id, customer_code, package_code, route_code, plan_qty, unit_type,
             due_date, priority, customer_po, remark, status, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $13)
        RETURNING *
        """,
        wo_no, data["device_id"], device["customer_code"], device["package_code"],
        device["route_code"], data["plan_qty"], data.get("unit_type", "WAFER"),
        data["due_date"], data.get("priority", 5), data.get("customer_po", ""),
        data.get("remark", ""), WorkOrderStatus.DRAFT.value, actor,
    )


async def get_work_order(db, wo_no: str, raw: bool = False) -> dict:
    row = await fetch_one(db, f"SELECT * FROM {T_WORK_ORDERS} WHERE wo_no = $1", wo_no)
    if row is None:
        raise NotFoundError(f"找不到工單：{wo_no}")
    return row


async def list_work_orders(
    db,
    status: str | None = None,
    device_id: str | None = None,
    customer_code: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> dict:
    where = """
        WHERE ($1::text IS NULL OR status = $1)
          AND ($2::text IS NULL OR device_id = $2)
          AND ($3::text IS NULL OR customer_code = $3)
    """
    total = await db.fetchval(
        f"SELECT count(*) FROM {T_WORK_ORDERS} {where}", status, device_id, customer_code
    )
    items = await fetch_all(
        db,
        f"SELECT * FROM {T_WORK_ORDERS} {where} ORDER BY priority ASC, due_date ASC OFFSET $4 LIMIT $5",
        status, device_id, customer_code, skip, limit,
    )
    return {"items": items, "total": total, "skip": skip, "limit": limit}


async def update_work_order(db, wo_no: str, patch: dict, actor: str) -> dict:
    wo = await get_work_order(db, wo_no)
    if wo["status"] in {WorkOrderStatus.CLOSED.value, WorkOrderStatus.CANCELLED.value}:
        raise StateError(f"工單 {wo_no} 已{wo['status']}，不可修改")

    data = plain_values({k: v for k, v in patch.items() if v is not None})
    allowed = {"plan_qty", "due_date", "priority", "remark"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        raise ValidationError("沒有需要更新的欄位")
    if "plan_qty" in updates and updates["plan_qty"] < wo["released_qty"]:
        raise ValidationError(f"計畫量 {updates['plan_qty']} 不可小於已投料量 {wo['released_qty']}")

    assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(updates, start=1))
    return await fetch_one(
        db,
        f"""
        UPDATE {T_WORK_ORDERS} SET {assignments}, updated_at = now(), updated_by = ${len(updates) + 1}
        WHERE wo_no = ${len(updates) + 2} RETURNING *
        """,
        *updates.values(), actor, wo_no,
    )


#: 工單狀態機
ALLOWED_TRANSITIONS: dict[WorkOrderStatus, set[WorkOrderStatus]] = {
    WorkOrderStatus.DRAFT: {WorkOrderStatus.RELEASED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.RELEASED: {
        WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED,
    },
    WorkOrderStatus.IN_PROGRESS: {WorkOrderStatus.CLOSED},
    WorkOrderStatus.CLOSED: set(),
    WorkOrderStatus.CANCELLED: set(),
}


async def change_status(db, wo_no: str, target: WorkOrderStatus, actor: str) -> dict:
    wo = await get_work_order(db, wo_no)
    current = WorkOrderStatus(wo["status"])
    if target not in ALLOWED_TRANSITIONS[current]:
        raise StateError(f"工單狀態不可由 {current.value} 轉為 {target.value}")

    if target is WorkOrderStatus.CANCELLED:
        active = await db.fetchval(
            f"SELECT count(*) FROM {T_LOTS} WHERE wo_no = $1 AND status = ANY($2::text[])",
            wo_no, ACTIVE_STATUS_VALUES,
        )
        if active:
            raise StateError(f"工單 {wo_no} 尚有 {active} 個在製批號，無法取消")

    return await fetch_one(
        db,
        f"""
        UPDATE {T_WORK_ORDERS} SET status = $1, updated_at = now(), updated_by = $2
        WHERE wo_no = $3 RETURNING *
        """,
        target.value, actor, wo_no,
    )


async def work_order_progress(db, wo_no: str) -> dict:
    """工單達成進度：投料、在製、完工、報廢。"""
    wo = await get_work_order(db, wo_no)
    rows = await fetch_all(
        db,
        f"""
        SELECT status, count(*) AS lots, sum(qty) AS qty, sum(scrap_qty) AS scrap
        FROM {T_LOTS} WHERE wo_no = $1 GROUP BY status
        """,
        wo_no,
    )
    by_status = {
        r["status"]: {"lots": r["lots"], "qty": int(r["qty"] or 0), "scrap": int(r["scrap"] or 0)}
        for r in rows
    }
    wip = sum(v["qty"] for s, v in by_status.items() if s in set(ACTIVE_STATUS_VALUES))
    done = sum(v["qty"] for s, v in by_status.items() if s in {"COMPLETED", "SHIPPED"})
    plan = wo["plan_qty"] or 0
    return {
        "wo_no": wo_no,
        "device_id": wo["device_id"],
        "status": wo["status"],
        "plan_qty": plan,
        "released_qty": wo["released_qty"],
        "wip_qty": wip,
        "completed_qty": done,
        "scrap_qty": sum(v["scrap"] for v in by_status.values()),
        "release_rate": round(wo["released_qty"] / plan, 4) if plan else 0.0,
        "by_status": by_status,
    }
