"""現場終端機 API。

給站在機台旁邊的人用的一組聚合端點：掃描解析、進站前置檢查、站別工作台。
真正的異動仍然走 ``/api/lots/*`` —— 這裡不重複實作生產邏輯。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from app.models.base import MESModel
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user
from app.services import lot_service, terminal_service

router = APIRouter(prefix="/api/terminal", tags=["現場終端機"])

CanRead = Depends(get_current_user)


class ScanIn(MESModel):
    """掃描槍就是鍵盤模擬器：掃到什麼就送什麼過來，由後端判斷是什麼。"""

    code: str = Field(min_length=1)
    eq_id: str = Field(default="", description="目前選定的機台，用於進站前置檢查")


@router.post("/scan", dependencies=[CanRead], summary="掃描解析：這是什麼、能做什麼")
async def scan(db: DB, payload: ScanIn, user: CurrentUser):
    return await terminal_service.resolve(db, payload.code, user, payload.eq_id)


@router.get("/stations", dependencies=[CanRead], summary="可選的機台清單")
async def stations(db: DB, area: str | None = None):
    return await terminal_service.stations(db, area)


@router.get("/station/{eq_id}", dependencies=[CanRead], summary="站別工作台（一次餵滿整個畫面）")
async def station(
    db: DB,
    eq_id: str,
    user: CurrentUser,
    queue_limit: Annotated[int, Query(ge=1, le=50)] = 12,
):
    return await terminal_service.station(db, eq_id, user, queue_limit)


@router.get("/lots/{lot_id}/preflight", dependencies=[CanRead], summary="進站前置檢查")
async def preflight(db: DB, lot_id: str, user: CurrentUser, eq_id: str = ""):
    lot = await lot_service.get_lot(db, lot_id)
    return await terminal_service.preflight(db, lot, user, eq_id)
