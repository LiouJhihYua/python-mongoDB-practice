"""工單服務。"""

from __future__ import annotations

from app.database import COL_DEVICES, COL_LOTS, COL_WORK_ORDERS, next_sequence
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import clean, clean_all, mongo_encode, to_local, utcnow
from app.models.enums import LotStatus, WorkOrderStatus


async def _gen_wo_no(db) -> str:
    ym = f"{to_local(utcnow()):%y%m}"
    return f"WO{ym}{await next_sequence(db, f'wo:{ym}'):04d}"


async def create_work_order(db, payload: dict, actor: str) -> dict:
    device = await db[COL_DEVICES].find_one({"device_id": payload["device_id"]})
    if device is None:
        raise ValidationError(f"產品料號不存在：{payload['device_id']}")
    if not device.get("active", True):
        raise ValidationError(f"產品料號已停用：{payload['device_id']}")

    wo_no = await _gen_wo_no(db)
    doc = mongo_encode(dict(payload))
    doc.update(
        wo_no=wo_no,
        customer_code=device["customer_code"],
        package_code=device["package_code"],
        route_code=device["route_code"],
        status=WorkOrderStatus.DRAFT.value,
        released_qty=0,
        lot_count=0,
        created_at=utcnow(),
        created_by=actor,
        updated_at=utcnow(),
    )
    await db[COL_WORK_ORDERS].insert_one(doc)
    return clean(await db[COL_WORK_ORDERS].find_one({"wo_no": wo_no}))


async def get_work_order(db, wo_no: str, raw: bool = False) -> dict:
    doc = await db[COL_WORK_ORDERS].find_one({"wo_no": wo_no})
    if doc is None:
        raise NotFoundError(f"找不到工單：{wo_no}")
    return doc if raw else clean(doc)


async def list_work_orders(
    db,
    status: str | None = None,
    device_id: str | None = None,
    customer_code: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> dict:
    filt = {
        k: v
        for k, v in {"status": status, "device_id": device_id, "customer_code": customer_code}.items()
        if v
    }
    total = await db[COL_WORK_ORDERS].count_documents(filt)
    cursor = (
        db[COL_WORK_ORDERS].find(filt).sort([("priority", 1), ("due_date", 1)]).skip(skip).limit(limit)
    )
    return {
        "items": clean_all(await cursor.to_list(length=limit)),
        "total": total,
        "skip": skip,
        "limit": limit,
    }


async def update_work_order(db, wo_no: str, patch: dict, actor: str) -> dict:
    wo = await get_work_order(db, wo_no, raw=True)
    if wo["status"] in {WorkOrderStatus.CLOSED.value, WorkOrderStatus.CANCELLED.value}:
        raise StateError(f"工單 {wo_no} 已{wo['status']}，不可修改")
    update = {k: v for k, v in mongo_encode(patch).items() if v is not None}
    if "plan_qty" in update and update["plan_qty"] < wo["released_qty"]:
        raise ValidationError(
            f"計畫量 {update['plan_qty']} 不可小於已投料量 {wo['released_qty']}"
        )
    update.update(updated_at=utcnow(), updated_by=actor)
    await db[COL_WORK_ORDERS].update_one({"wo_no": wo_no}, {"$set": update})
    return clean(await db[COL_WORK_ORDERS].find_one({"wo_no": wo_no}))


async def change_status(db, wo_no: str, target: WorkOrderStatus, actor: str) -> dict:
    wo = await get_work_order(db, wo_no, raw=True)
    current = WorkOrderStatus(wo["status"])
    allowed: dict[WorkOrderStatus, set[WorkOrderStatus]] = {
        WorkOrderStatus.DRAFT: {WorkOrderStatus.RELEASED, WorkOrderStatus.CANCELLED},
        WorkOrderStatus.RELEASED: {
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.CLOSED,
            WorkOrderStatus.CANCELLED,
        },
        WorkOrderStatus.IN_PROGRESS: {WorkOrderStatus.CLOSED},
        WorkOrderStatus.CLOSED: set(),
        WorkOrderStatus.CANCELLED: set(),
    }
    if target not in allowed[current]:
        raise StateError(f"工單狀態不可由 {current.value} 轉為 {target.value}")
    if target is WorkOrderStatus.CANCELLED:
        active = await db[COL_LOTS].count_documents(
            {"wo_no": wo_no, "status": {"$in": [s.value for s in (LotStatus.WAITING, LotStatus.RUNNING, LotStatus.HOLD)]}}
        )
        if active:
            raise StateError(f"工單 {wo_no} 尚有 {active} 個在製批號，無法取消")
    await db[COL_WORK_ORDERS].update_one(
        {"wo_no": wo_no},
        {"$set": {"status": target.value, "updated_at": utcnow(), "updated_by": actor}},
    )
    return clean(await db[COL_WORK_ORDERS].find_one({"wo_no": wo_no}))


async def work_order_progress(db, wo_no: str) -> dict:
    """工單達成進度：投料、在製、完工、報廢。"""
    wo = await get_work_order(db, wo_no, raw=True)
    rows = await db[COL_LOTS].aggregate(
        [
            {"$match": {"wo_no": wo_no}},
            {
                "$group": {
                    "_id": "$status",
                    "lots": {"$sum": 1},
                    "qty": {"$sum": "$qty"},
                    "scrap": {"$sum": "$scrap_qty"},
                }
            },
        ]
    ).to_list(length=None)
    by_status = {r["_id"]: {"lots": r["lots"], "qty": r["qty"], "scrap": r["scrap"]} for r in rows}
    wip = sum(
        v["qty"] for s, v in by_status.items() if s in {LotStatus.WAITING.value, LotStatus.RUNNING.value, LotStatus.HOLD.value}
    )
    done = sum(
        v["qty"] for s, v in by_status.items() if s in {LotStatus.COMPLETED.value, LotStatus.SHIPPED.value}
    )
    scrap = sum(v["scrap"] for v in by_status.values())
    plan = wo.get("plan_qty", 0) or 0
    return {
        "wo_no": wo_no,
        "device_id": wo["device_id"],
        "status": wo["status"],
        "plan_qty": plan,
        "released_qty": wo.get("released_qty", 0),
        "wip_qty": wip,
        "completed_qty": done,
        "scrap_qty": scrap,
        "release_rate": round(wo.get("released_qty", 0) / plan, 4) if plan else 0.0,
        "by_status": by_status,
    }
