"""設備管理 API：建檔、E10 狀態、OEE、PM 保養。"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.master import EquipmentIn, EquipmentStateIn
from app.routers.deps import DB, Window
from app.security import CurrentUser, get_current_user, require_roles
from app.services import equipment_service, master_service

router = APIRouter(prefix="/api/equipments", tags=["設備"])

CanEdit = Depends(require_roles(Role.ENGINEER))
CanOperate = Depends(require_roles(Role.OPERATOR, Role.ENGINEER))
CanRead = Depends(get_current_user)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="設備建檔")
async def create(db: DB, payload: EquipmentIn, user: CurrentUser):
    return await master_service.create_equipment(db, payload.model_dump(), user["username"])


@router.get("", dependencies=[CanRead], summary="設備清單")
async def list_all(
    db: DB,
    area: str | None = None,
    current_state: str | None = None,
    active: bool | None = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await master_service.equipments.list(
        db, {"area": area, "current_state": current_state, "active": active}, skip, limit
    )


@router.get("/summary", dependencies=[CanRead], summary="設備狀態即時分佈")
async def summary(db: DB, area: str | None = None):
    return await equipment_service.state_summary(db, area)


@router.get("/oee", dependencies=[CanRead], summary="全廠 OEE 總覽")
async def oee_overview(db: DB, window: Window, area: str | None = None):
    start, end = window
    return await equipment_service.oee_overview(db, start, end, area)


@router.get("/pm", dependencies=[CanRead], summary="PM 保養工單清單")
async def list_pm(db: DB, eq_id: str | None = None, status_: Annotated[str | None, Query(alias="status")] = None):
    return await equipment_service.list_pm(db, eq_id, status_)


@router.post("/pm", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立 PM 保養工單")
async def create_pm(db: DB, eq_id: str, pm_type: str, due_date: datetime, user: CurrentUser, remark: str = ""):
    return await equipment_service.create_pm(db, eq_id, pm_type, due_date, user["username"], remark)


@router.post("/pm/{pm_id}/complete", dependencies=[CanOperate], summary="完成 PM 保養")
async def complete_pm(db: DB, pm_id: int, user: CurrentUser, remark: str = ""):
    return await equipment_service.complete_pm(db, pm_id, user["username"], remark)


@router.get("/{eq_id}", dependencies=[CanRead], summary="設備明細")
async def get_one(db: DB, eq_id: str):
    return await master_service.equipments.get(db, eq_id)


@router.post("/{eq_id}/state", dependencies=[CanOperate], summary="變更設備狀態（SEMI E10）")
async def set_state(db: DB, eq_id: str, payload: EquipmentStateIn, user: CurrentUser):
    return await equipment_service.set_state(
        db, eq_id, payload.state, user["username"], payload.reason_code, payload.remark
    )


@router.get("/{eq_id}/state-history", dependencies=[CanRead], summary="設備狀態履歷")
async def state_history(db: DB, eq_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return await equipment_service.state_history(db, eq_id, limit)


@router.get("/{eq_id}/oee", dependencies=[CanRead], summary="單一設備 OEE")
async def oee(db: DB, eq_id: str, window: Window):
    start, end = window
    return await equipment_service.calc_oee(db, eq_id, start, end)
