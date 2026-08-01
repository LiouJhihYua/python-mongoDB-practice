"""主檔維護 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.master import (
    CustomerIn,
    DefectCodeIn,
    DeviceIn,
    OperationIn,
    PackageIn,
    RouteIn,
    WaferIn,
)
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import master_service

router = APIRouter(prefix="/api/master", tags=["主檔"])

#: 主檔異動限工程／生管；查詢開放給所有登入者
CanEdit = Depends(require_roles(Role.ENGINEER, Role.PLANNER))
CanRead = Depends(get_current_user)


# ── 客戶 ────────────────────────────────────────────────────
@router.post("/customers", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立客戶")
async def create_customer(db: DB, payload: CustomerIn, user: CurrentUser):
    return await master_service.customers.create(db, payload.model_dump(), user["username"])


@router.get("/customers", dependencies=[CanRead], summary="客戶清單")
async def list_customers(db: DB, active: bool | None = None, skip: int = 0, limit: int = 100):
    return await master_service.customers.list(db, {"active": active}, skip, limit)


@router.get("/customers/{code}", dependencies=[CanRead], summary="客戶明細")
async def get_customer(db: DB, code: str):
    return await master_service.customers.get(db, code)


# ── 封裝型式 ────────────────────────────────────────────────
@router.post("/packages", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立封裝型式")
async def create_package(db: DB, payload: PackageIn, user: CurrentUser):
    return await master_service.packages.create(db, payload.model_dump(), user["username"])


@router.get("/packages", dependencies=[CanRead], summary="封裝型式清單")
async def list_packages(db: DB, active: bool | None = None, skip: int = 0, limit: int = 100):
    return await master_service.packages.list(db, {"active": active}, skip, limit)


# ── 產品料號 ────────────────────────────────────────────────
@router.post("/devices", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立產品料號")
async def create_device(db: DB, payload: DeviceIn, user: CurrentUser):
    return await master_service.create_device(db, payload.model_dump(), user["username"])


@router.get("/devices", dependencies=[CanRead], summary="產品料號清單")
async def list_devices(
    db: DB,
    customer_code: str | None = None,
    active: bool | None = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await master_service.devices.list(
        db, {"customer_code": customer_code, "active": active}, skip, limit
    )


@router.get("/devices/{device_id}", dependencies=[CanRead], summary="產品料號明細")
async def get_device(db: DB, device_id: str):
    return await master_service.devices.get(db, device_id)


@router.patch("/devices/{device_id}", dependencies=[CanEdit], summary="更新產品料號")
async def update_device(db: DB, device_id: str, payload: dict, user: CurrentUser):
    return await master_service.devices.update(db, device_id, payload, user["username"])


# ── 站別 ────────────────────────────────────────────────────
@router.post("/operations", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立站別")
async def create_operation(db: DB, payload: OperationIn, user: CurrentUser):
    return await master_service.operations.create(db, payload.model_dump(), user["username"])


@router.get("/operations", dependencies=[CanRead], summary="站別清單")
async def list_operations(db: DB, op_type: str | None = None, active: bool | None = None, limit: int = 200):
    return await master_service.operations.list(db, {"op_type": op_type, "active": active}, 0, limit)


@router.get("/operations/{op_code}", dependencies=[CanRead], summary="站別明細")
async def get_operation(db: DB, op_code: str):
    return await master_service.operations.get(db, op_code)


# ── 製程流程 ────────────────────────────────────────────────
@router.post("/routes", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立製程流程")
async def create_route(db: DB, payload: RouteIn, user: CurrentUser):
    return await master_service.create_route(db, payload.model_dump(), user["username"])


@router.get("/routes", dependencies=[CanRead], summary="製程流程清單")
async def list_routes(db: DB, active: bool | None = None, skip: int = 0, limit: int = 100):
    return await master_service.list_routes(db, skip, limit, active)


@router.get("/routes/{route_code}", dependencies=[CanRead], summary="流程明細（預設取最新啟用版本）")
async def get_route(db: DB, route_code: str, version: int | None = None):
    from app.models.base import clean

    return clean(await master_service.get_active_route(db, route_code, version))


# ── 不良代碼 ────────────────────────────────────────────────
@router.post("/defect-codes", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立不良代碼")
async def create_defect_code(db: DB, payload: DefectCodeIn, user: CurrentUser):
    return await master_service.create_defect_code(db, payload.model_dump(), user["username"])


@router.get("/defect-codes", dependencies=[CanRead], summary="不良代碼清單")
async def list_defect_codes(db: DB, category: str | None = None, active: bool | None = None, limit: int = 200):
    return await master_service.defect_codes.list(db, {"category": category, "active": active}, 0, limit)


# ── 晶圓來料 ────────────────────────────────────────────────
@router.post("/wafers", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="晶圓進料建檔")
async def create_wafer(db: DB, payload: WaferIn, user: CurrentUser):
    return await master_service.create_wafer(db, payload.model_dump(), user["username"])


@router.get("/wafers", dependencies=[CanRead], summary="晶圓清單")
async def list_wafers(
    db: DB,
    device_id: str | None = None,
    wafer_lot_id: str | None = None,
    consumed: bool | None = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await master_service.wafers.list(
        db, {"device_id": device_id, "wafer_lot_id": wafer_lot_id, "consumed": consumed}, skip, limit
    )
