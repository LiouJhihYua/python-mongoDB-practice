"""批號生產執行 API —— 現場人員最常打的一組。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.production import (
    HoldIn,
    LotCreateIn,
    MergeIn,
    ReleaseIn,
    ReworkIn,
    ScrapIn,
    ShipmentIn,
    SplitIn,
    TrackInIn,
    TrackOutIn,
)
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import lot_service

router = APIRouter(prefix="/api/lots", tags=["批號生產"])

CanOperate = Depends(require_roles(Role.OPERATOR, Role.ENGINEER))
CanPlan = Depends(require_roles(Role.PLANNER))
CanQC = Depends(require_roles(Role.QC, Role.ENGINEER))
CanRead = Depends(get_current_user)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[CanPlan], summary="開批（自工單投料）")
async def create(db: DB, payload: LotCreateIn, user: CurrentUser):
    return await lot_service.create_lot(db, payload.model_dump(), user["username"])


@router.get("", dependencies=[CanRead], summary="批號清單")
async def list_all(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    op_code: str | None = None,
    device_id: str | None = None,
    wo_no: str | None = None,
    customer_code: str | None = None,
    on_hold: bool | None = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
):
    return await lot_service.list_lots(
        db,
        {
            "status": status_,
            "op_code": op_code,
            "device_id": device_id,
            "wo_no": wo_no,
            "customer_code": customer_code,
            "on_hold": on_hold,
            "skip": skip,
            "limit": limit,
        },
    )


@router.get("/{lot_id}", dependencies=[CanRead], summary="批號明細")
async def get_one(db: DB, lot_id: str):
    return await lot_service.get_lot(db, lot_id)


@router.get("/{lot_id}/history", dependencies=[CanRead], summary="批號加工履歷")
async def history(db: DB, lot_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 200):
    return await lot_service.lot_history(db, lot_id, limit)


@router.post("/track-in", dependencies=[CanOperate], summary="進站（Track-In）")
async def track_in(db: DB, payload: TrackInIn, user: CurrentUser):
    return await lot_service.track_in(db, payload.model_dump(), user)


@router.post("/track-out", dependencies=[CanOperate], summary="出站（Track-Out）並回報良率")
async def track_out(db: DB, payload: TrackOutIn, user: CurrentUser):
    return await lot_service.track_out(db, payload.model_dump(), user)


@router.post("/hold", dependencies=[CanQC], summary="扣留批號")
async def hold(db: DB, payload: HoldIn, user: CurrentUser):
    return await lot_service.hold_lot(db, payload.model_dump(), user)


@router.post("/release", dependencies=[CanQC], summary="放行批號")
async def release(db: DB, payload: ReleaseIn, user: CurrentUser):
    return await lot_service.release_lot(db, payload.model_dump(), user)


@router.post("/split", dependencies=[CanPlan], summary="拆批")
async def split(db: DB, payload: SplitIn, user: CurrentUser):
    return await lot_service.split_lot(db, payload.model_dump(), user)


@router.post("/merge", dependencies=[CanPlan], summary="併批")
async def merge(db: DB, payload: MergeIn, user: CurrentUser):
    return await lot_service.merge_lots(db, payload.model_dump(), user)


@router.post("/scrap", dependencies=[CanQC], summary="報廢（可部分報廢）")
async def scrap(db: DB, payload: ScrapIn, user: CurrentUser):
    return await lot_service.scrap_lot(db, payload.model_dump(), user)


@router.post("/rework", dependencies=[CanQC], summary="重工（退回前站）")
async def rework(db: DB, payload: ReworkIn, user: CurrentUser):
    return await lot_service.rework_lot(db, payload.model_dump(), user)


@router.post("/ship", dependencies=[CanPlan], summary="出貨")
async def ship(db: DB, payload: ShipmentIn, user: CurrentUser):
    return await lot_service.ship_lots(db, payload.model_dump(), user)
