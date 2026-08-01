"""品質 API：扣留清單、不良分析、Bin 統計、不良判定。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.models.enums import DispositionType, Role
from app.routers.deps import DB, Window
from app.security import CurrentUser, get_current_user, require_roles
from app.services import quality_service

router = APIRouter(prefix="/api/quality", tags=["品質"])

CanQC = Depends(require_roles(Role.QC, Role.ENGINEER))
CanRead = Depends(get_current_user)


@router.get("/holds", dependencies=[CanRead], summary="扣留清單")
async def holds(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    lot_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await quality_service.list_holds(db, status_, lot_id, limit)


@router.get("/holds/summary", dependencies=[CanRead], summary="扣留中批號統計")
async def holds_summary(db: DB):
    return await quality_service.open_hold_summary(db)


@router.get("/defects", dependencies=[CanRead], summary="不良紀錄清單")
async def defects(
    db: DB,
    lot_id: str | None = None,
    op_code: str | None = None,
    defect_code: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await quality_service.list_defect_records(db, lot_id, op_code, defect_code, limit)


@router.get("/defects/pareto", dependencies=[CanRead], summary="不良柏拉圖")
async def pareto(
    db: DB,
    window: Window,
    op_code: str | None = None,
    device_id: str | None = None,
    top_n: Annotated[int, Query(ge=1, le=50)] = 20,
):
    start, end = window
    return await quality_service.defect_pareto(db, start, end, op_code, device_id, top_n)


@router.get("/bins", dependencies=[CanRead], summary="測試站 Bin 分佈")
async def bins(db: DB, window: Window, op_code: str | None = None, device_id: str | None = None):
    start, end = window
    return await quality_service.bin_summary(db, start, end, op_code, device_id)


@router.post("/defects/{record_id}/disposition", dependencies=[CanQC], summary="不良品判定")
async def disposition(db: DB, record_id: int, disposition: DispositionType, user: CurrentUser, remark: str = ""):
    return await quality_service.disposition_defect(db, record_id, disposition.value, user["username"], remark)
