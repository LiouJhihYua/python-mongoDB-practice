"""e-SOP 電子作業指導書 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.sop import SOPAckIn, SOPIn, SOPReleaseIn, SOPUpdate
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import sop_service

router = APIRouter(prefix="/api/sops", tags=["e-SOP 作業指導書"])

CanEdit = Depends(require_roles(Role.ENGINEER))
CanRead = Depends(get_current_user)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立 SOP 草稿")
async def create(db: DB, payload: SOPIn, user: CurrentUser):
    return await sop_service.create_sop(db, payload.model_dump(), user["username"])


@router.get("", dependencies=[CanRead], summary="SOP 清單")
async def list_all(
    db: DB,
    op_code: str | None = None,
    device_id: str | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    latest_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await sop_service.list_sops(db, op_code, device_id, status_, latest_only, limit)


@router.get("/pending", dependencies=[CanRead], summary="我尚未簽認的 SOP")
async def pending(db: DB, user: CurrentUser):
    return await sop_service.pending_for_user(db, user["username"])


@router.get("/compliance", dependencies=[CanRead], summary="SOP 簽認率")
async def compliance(db: DB, op_code: str | None = None):
    return await sop_service.compliance(db, op_code)


@router.get("/station/{op_code}", dependencies=[CanRead], summary="站別目前適用的 SOP 與我的簽認狀態")
async def station(db: DB, op_code: str, user: CurrentUser, device_id: str = ""):
    return await sop_service.station_sop(db, op_code, device_id, user["username"])


@router.post("/acknowledge", dependencies=[CanRead], summary="簽認 SOP")
async def acknowledge(db: DB, payload: SOPAckIn, user: CurrentUser):
    return await sop_service.acknowledge(db, payload.model_dump(), user["username"])


@router.get("/{sop_code}", dependencies=[CanRead], summary="SOP 內容（預設取最新版）")
async def get_one(db: DB, sop_code: str, version: int | None = None):
    return await sop_service.get_sop(db, sop_code, version)


@router.post("/{sop_code}/revise", dependencies=[CanEdit], summary="以現行版本複製出新草稿")
async def revise(db: DB, sop_code: str, user: CurrentUser):
    return await sop_service.revise_sop(db, sop_code, user["username"])


@router.get("/{sop_code}/acknowledgements", dependencies=[CanRead], summary="某版本的簽認名單")
async def acks(db: DB, sop_code: str, version: int | None = None):
    return await sop_service.acknowledgements(db, sop_code, version)


@router.patch("/{sop_code}/{version}", dependencies=[CanEdit], summary="修改草稿內容")
async def update(db: DB, sop_code: str, version: int, payload: SOPUpdate, user: CurrentUser):
    return await sop_service.update_sop(
        db, sop_code, version, payload.model_dump(exclude_unset=True), user["username"]
    )


@router.post("/{sop_code}/{version}/release", dependencies=[CanEdit], summary="發行版本")
async def release(db: DB, sop_code: str, version: int, payload: SOPReleaseIn, user: CurrentUser):
    return await sop_service.release_sop(db, sop_code, version, payload.model_dump(), user["username"])


@router.post("/{sop_code}/{version}/obsolete", dependencies=[CanEdit], summary="作廢版本")
async def obsolete(db: DB, sop_code: str, version: int, user: CurrentUser):
    return await sop_service.obsolete_sop(db, sop_code, version, user["username"])
