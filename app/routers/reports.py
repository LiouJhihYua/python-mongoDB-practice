"""報表與戰情看板 API。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.routers.deps import DB, Window
from app.security import get_current_user
from app.services import report_service

router = APIRouter(prefix="/api/reports", tags=["報表"])

CanRead = Depends(get_current_user)


@router.get("/dashboard", dependencies=[CanRead], summary="戰情看板總覽（單一 API 餵滿整面牆）")
async def dashboard(db: DB, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24):
    return await report_service.dashboard(db, hours)


@router.get("/wip", dependencies=[CanRead], summary="各站在製量")
async def wip(db: DB, device_id: str | None = None, customer_code: str | None = None):
    return await report_service.wip_by_operation(db, device_id, customer_code)


@router.get("/wip/aging", dependencies=[CanRead], summary="在製品停留時間分佈")
async def wip_aging(db: DB):
    return await report_service.wip_aging(db)


@router.get(
    "/yield/by-operation",
    dependencies=[CanRead],
    summary="各站良率（累計良率需限定單一流程）",
)
async def yield_by_operation(
    db: DB, window: Window, device_id: str | None = None, route_code: str | None = None
):
    start, end = window
    return await report_service.yield_by_operation(db, start, end, device_id, route_code)


@router.get("/yield/by-route", dependencies=[CanRead], summary="各製程流程的累計良率")
async def yield_by_route(db: DB, window: Window):
    start, end = window
    return await report_service.final_yield_by_route(db, start, end)


@router.get("/yield/by-device", dependencies=[CanRead], summary="各料號良率")
async def yield_by_device(db: DB, window: Window, top_n: Annotated[int, Query(ge=1, le=100)] = 20):
    start, end = window
    return await report_service.yield_by_device(db, start, end, top_n)


@router.get("/throughput", dependencies=[CanRead], summary="產出統計（Moves）")
async def throughput(
    db: DB,
    window: Window,
    group_by: Literal["shift", "operation", "device"] = "shift",
):
    start, end = window
    return await report_service.throughput(db, start, end, group_by)


@router.get("/cycle-time", dependencies=[CanRead], summary="各站週期時間與瓶頸")
async def cycle_time(db: DB, window: Window):
    start, end = window
    return await report_service.cycle_time_by_operation(db, start, end)


@router.get("/qtime-violations", dependencies=[CanRead], summary="Q-Time 逾時紀錄")
async def qtime_violations(db: DB, window: Window, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    start, end = window
    return await report_service.qtime_violations(db, start, end, limit)
