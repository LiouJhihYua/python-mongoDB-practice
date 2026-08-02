"""SPC 統計製程管制 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.spc import MeasurementIn, MeasurementItemIn
from app.routers.deps import DB, Window
from app.security import CurrentUser, get_current_user, require_roles
from app.services import attribute_spc_service, spc_service

router = APIRouter(prefix="/api/spc", tags=["SPC 統計製程管制"])

CanEdit = Depends(require_roles(Role.ENGINEER, Role.QC))
CanMeasure = Depends(require_roles(Role.OPERATOR, Role.QC, Role.ENGINEER))
CanRead = Depends(get_current_user)


@router.post("/items", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立量測項目")
async def create_item(db: DB, payload: MeasurementItemIn, user: CurrentUser):
    return await spc_service.create_item(db, payload.model_dump(), user["username"])


@router.get("/items", dependencies=[CanRead], summary="量測項目清單")
async def list_items(db: DB, op_code: str | None = None, active: bool | None = None, limit: int = 200):
    return await spc_service.items.list(db, {"op_code": op_code, "active": active}, 0, limit)


@router.post("/measurements", dependencies=[CanMeasure], summary="記錄量測（即時判定超規與判異）")
async def record(db: DB, payload: MeasurementIn, user: CurrentUser):
    return await spc_service.record_measurement(db, payload.model_dump(), user)


@router.get("/measurements", dependencies=[CanRead], summary="量測紀錄清單")
async def list_measurements(
    db: DB,
    item_code: str | None = None,
    lot_id: str | None = None,
    violations_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await spc_service.list_measurements(db, item_code, lot_id, violations_only, limit)


@router.get("/violations", dependencies=[CanRead], summary="判異統計")
async def violations(db: DB, window: Window):
    start, end = window
    return await spc_service.violation_summary(db, start, end)


@router.get("/items/{item_code}/chart", dependencies=[CanRead], summary="X-bar / R 管制圖")
async def chart(db: DB, item_code: str, window: Window, lot_id: str | None = None):
    start, end = window
    return await spc_service.control_chart(db, item_code, start, end, lot_id)


@router.get("/items/{item_code}/capability", dependencies=[CanRead], summary="製程能力 Cp / Cpk")
async def capability(db: DB, item_code: str, window: Window, device_id: str | None = None):
    start, end = window
    return await spc_service.capability(db, item_code, start, end, device_id)


# ── 屬性管制圖（計數值）────────────────────────────────────
@router.get("/attribute/overview", dependencies=[CanRead], summary="各站不良率 p 圖摘要")
async def attribute_overview(db: DB, window: Window):
    start, end = window
    return await attribute_spc_service.overview(db, start, end)


@router.get(
    "/attribute/{op_code}", dependencies=[CanRead],
    summary="屬性管制圖：p（不良率）/ np（不良數）/ c（缺點數）/ u（單位缺點數）/ ewma",
)
async def attribute_chart(
    db: DB,
    op_code: str,
    window: Window,
    chart: Annotated[str, Query(pattern="^(p|np|c|u|ewma)$")] = "p",
    device_id: str | None = None,
    limit: Annotated[int, Query(ge=10, le=1000)] = 200,
):
    start, end = window
    return await attribute_spc_service.build_chart(
        db, chart, op_code, start, end, device_id, limit
    )
