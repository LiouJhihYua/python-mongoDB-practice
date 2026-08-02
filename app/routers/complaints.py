"""客訴（RMA）與載具管理 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.quality_ops import (
    CarrierAssignIn,
    CarrierIn,
    CarrierStatusIn,
    ComplaintIn,
    ComplaintNoteIn,
    ComplaintStatusIn,
    D8StepIn,
)
from app.routers.deps import DB, Window
from app.security import CurrentUser, get_current_user, require_roles
from app.services import carrier_service, complaint_service

router = APIRouter(prefix="/api/quality-ops", tags=["客訴與載具"])

CanHandle = Depends(require_roles(Role.QC, Role.ENGINEER))
CanOperate = Depends(require_roles(Role.OPERATOR, Role.QC, Role.ENGINEER))
CanRead = Depends(get_current_user)


# ── 客訴 ────────────────────────────────────────────────────
@router.post(
    "/complaints", status_code=status.HTTP_201_CREATED, dependencies=[CanHandle],
    summary="開立客訴單（自動圈選受影響範圍）",
)
async def create_complaint(db: DB, payload: ComplaintIn, user: CurrentUser):
    return await complaint_service.create_complaint(db, payload.model_dump(), user["username"])


@router.get("/complaints", dependencies=[CanRead], summary="客訴清單")
async def list_complaints(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    customer_code: str | None = None,
    severity: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await complaint_service.list_complaints(db, status_, customer_code, severity, limit)


@router.get("/complaints/summary", dependencies=[CanRead], summary="客訴看板")
async def complaint_summary(db: DB, window: Window):
    start, end = window
    return await complaint_service.summary(db, start, end)


@router.post("/complaints/impact-preview", dependencies=[CanRead], summary="先試算影響範圍（不建單）")
async def impact_preview(db: DB, payload: ComplaintIn):
    return await complaint_service.analyse_impact(db, payload.lot_ids, payload.unit_seqs)


@router.get("/complaints/{complaint_no}", dependencies=[CanRead], summary="客訴明細（含 8D 與歷程）")
async def get_complaint(db: DB, complaint_no: str):
    return await complaint_service.get_complaint(db, complaint_no)


@router.post("/complaints/{complaint_no}/refresh-impact", dependencies=[CanHandle], summary="重跑影響分析")
async def refresh_impact(db: DB, complaint_no: str, user: CurrentUser):
    return await complaint_service.refresh_impact(db, complaint_no, user["username"])


@router.put("/complaints/{complaint_no}/d8/{step}", dependencies=[CanHandle], summary="填寫 8D 步驟")
async def update_d8(db: DB, complaint_no: str, step: str, payload: D8StepIn, user: CurrentUser):
    return await complaint_service.update_d8(
        db, complaint_no, step, payload.model_dump(), user["username"]
    )


@router.post("/complaints/{complaint_no}/status", dependencies=[CanHandle], summary="變更客訴狀態")
async def change_status(db: DB, complaint_no: str, payload: ComplaintStatusIn, user: CurrentUser):
    return await complaint_service.change_status(
        db, complaint_no, payload.status, payload.model_dump(), user["username"]
    )


@router.post("/complaints/{complaint_no}/notes", dependencies=[CanHandle], summary="加註處理紀錄")
async def add_note(db: DB, complaint_no: str, payload: ComplaintNoteIn, user: CurrentUser):
    return await complaint_service.add_note(db, complaint_no, payload.content, user["username"])


# ── 載具 ────────────────────────────────────────────────────
@router.post(
    "/carriers", status_code=status.HTTP_201_CREATED, dependencies=[CanHandle], summary="建立載具"
)
async def create_carrier(db: DB, payload: CarrierIn, user: CurrentUser):
    return await carrier_service.create_carrier(db, payload.model_dump(), user["username"])


@router.get("/carriers", dependencies=[CanRead], summary="載具清單")
async def list_carriers(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    carrier_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await carrier_service.list_carriers(db, status_, carrier_type, limit)


@router.get("/carriers/overview", dependencies=[CanRead], summary="載具看板")
async def carrier_overview(db: DB):
    return await carrier_service.overview(db)


@router.post("/carriers/assign", dependencies=[CanOperate], summary="指派載具給批號")
async def assign(db: DB, payload: CarrierAssignIn, user: CurrentUser):
    return await carrier_service.assign(
        db, payload.carrier_id, payload.lot_id, user["username"], payload.remark
    )


@router.get("/carriers/{carrier_id}", dependencies=[CanRead], summary="載具明細")
async def get_carrier(db: DB, carrier_id: str):
    return await carrier_service.get_carrier(db, carrier_id)


@router.get("/carriers/{carrier_id}/history", dependencies=[CanRead], summary="載具使用履歷")
async def carrier_history(db: DB, carrier_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return await carrier_service.carrier_history(db, carrier_id, limit)


@router.post("/carriers/{carrier_id}/release", dependencies=[CanOperate], summary="釋放載具")
async def release(db: DB, carrier_id: str, user: CurrentUser, remark: str = ""):
    return await carrier_service.release(db, carrier_id, user["username"], remark)


@router.post("/carriers/{carrier_id}/clean", dependencies=[CanOperate], summary="完成清洗")
async def clean(db: DB, carrier_id: str, user: CurrentUser, remark: str = ""):
    return await carrier_service.clean(db, carrier_id, user["username"], remark)


@router.post("/carriers/{carrier_id}/status", dependencies=[CanHandle], summary="調整載具狀態")
async def set_status(db: DB, carrier_id: str, payload: CarrierStatusIn, user: CurrentUser):
    return await carrier_service.set_status(
        db, carrier_id, payload.status, user["username"], payload.remark
    )
