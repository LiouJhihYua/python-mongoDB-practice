"""派工與 Q-Time 預警 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.routers.deps import DB
from app.security import get_current_user
from app.services import dispatch_service

router = APIRouter(prefix="/api/dispatch", tags=["派工"])

CanRead = Depends(get_current_user)


@router.get("", dependencies=[CanRead], summary="派工清單（下一批做哪個）")
async def dispatch_list(
    db: DB,
    op_code: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await dispatch_service.dispatch_list(db, op_code, limit)


@router.get("/qtime-watch", dependencies=[CanRead], summary="Q-Time 預警（超時前先看見）")
async def qtime_watch(
    db: DB,
    warn_minutes: Annotated[int, Query(ge=1, le=1440)] = dispatch_service.QTIME_URGENT_MINUTES,
):
    return await dispatch_service.qtime_watch(db, warn_minutes)
