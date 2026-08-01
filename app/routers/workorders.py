"""工單 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role, WorkOrderStatus
from app.models.production import WorkOrderIn, WorkOrderUpdate
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import workorder_service

router = APIRouter(prefix="/api/work-orders", tags=["工單"])

CanPlan = Depends(require_roles(Role.PLANNER))
CanRead = Depends(get_current_user)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[CanPlan], summary="建立工單")
async def create(db: DB, payload: WorkOrderIn, user: CurrentUser):
    return await workorder_service.create_work_order(db, payload.model_dump(), user["username"])


@router.get("", dependencies=[CanRead], summary="工單清單")
async def list_all(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    device_id: str | None = None,
    customer_code: str | None = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    return await workorder_service.list_work_orders(db, status_, device_id, customer_code, skip, limit)


@router.get("/{wo_no}", dependencies=[CanRead], summary="工單明細")
async def get_one(db: DB, wo_no: str):
    return await workorder_service.get_work_order(db, wo_no)


@router.get("/{wo_no}/progress", dependencies=[CanRead], summary="工單進度")
async def progress(db: DB, wo_no: str):
    return await workorder_service.work_order_progress(db, wo_no)


@router.patch("/{wo_no}", dependencies=[CanPlan], summary="更新工單")
async def update(db: DB, wo_no: str, payload: WorkOrderUpdate, user: CurrentUser):
    return await workorder_service.update_work_order(
        db, wo_no, payload.model_dump(exclude_unset=True), user["username"]
    )


@router.post("/{wo_no}/release", dependencies=[CanPlan], summary="下達工單（可開批）")
async def release(db: DB, wo_no: str, user: CurrentUser):
    return await workorder_service.change_status(db, wo_no, WorkOrderStatus.RELEASED, user["username"])


@router.post("/{wo_no}/close", dependencies=[CanPlan], summary="工單結案")
async def close(db: DB, wo_no: str, user: CurrentUser):
    return await workorder_service.change_status(db, wo_no, WorkOrderStatus.CLOSED, user["username"])


@router.post("/{wo_no}/cancel", dependencies=[CanPlan], summary="取消工單")
async def cancel(db: DB, wo_no: str, user: CurrentUser):
    return await workorder_service.change_status(db, wo_no, WorkOrderStatus.CANCELLED, user["username"])
