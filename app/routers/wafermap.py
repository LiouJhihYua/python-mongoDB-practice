"""晶圓 Map 與 Die 級追溯 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.wafermap import DieAssignIn, DieResultIn, WaferMapIn
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import wafermap_service

router = APIRouter(prefix="/api/wafer", tags=["晶圓 Map"])

CanEdit = Depends(require_roles(Role.ENGINEER))
CanOperate = Depends(require_roles(Role.OPERATOR, Role.ENGINEER))
CanRead = Depends(get_current_user)


# ── Map ─────────────────────────────────────────────────────
@router.post("/maps", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="上傳晶圓 Map")
async def upload(db: DB, payload: WaferMapIn, user: CurrentUser):
    return await wafermap_service.upload_map(db, payload.model_dump(), user["username"])


@router.get("/maps", dependencies=[CanRead], summary="晶圓 Map 清單")
async def list_maps(
    db: DB,
    wafer_lot_id: str | None = None,
    device_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await wafermap_service.list_maps(db, wafer_lot_id, device_id, limit)


@router.get("/maps/{wafer_id}", dependencies=[CanRead], summary="晶圓 Map 摘要")
async def map_detail(db: DB, wafer_id: str):
    return await wafermap_service.map_detail(db, wafer_id)


@router.get("/maps/{wafer_id}/grid", dependencies=[CanRead], summary="晶圓 Map 二維陣列（可縮圖）")
async def map_grid(
    db: DB,
    wafer_id: str,
    max_size: Annotated[int, Query(ge=0, le=512, description="0 表示不縮圖")] = 120,
):
    return await wafermap_service.map_grid(db, wafer_id, max_size)


@router.get("/maps/{wafer_id}/analysis", dependencies=[CanRead], summary="邊緣良率與群聚不良分析")
async def analysis(
    db: DB,
    wafer_id: str,
    rings: Annotated[int, Query(ge=1, le=10)] = wafermap_service.DEFAULT_EDGE_RINGS,
    min_cluster: Annotated[int, Query(ge=2, le=500)] = wafermap_service.DEFAULT_MIN_CLUSTER,
):
    return await wafermap_service.analyse(db, wafer_id, rings, min_cluster)


@router.get("/maps/{wafer_id}/ft-correlation", dependencies=[CanRead], summary="CP 與 FT 對照")
async def ft_correlation(db: DB, wafer_id: str):
    return await wafermap_service.wafer_ft_correlation(db, wafer_id)


@router.delete("/maps/{wafer_id}", dependencies=[CanEdit], summary="刪除晶圓 Map")
async def delete_map(db: DB, wafer_id: str, user: CurrentUser):
    return await wafermap_service.delete_map(db, wafer_id, user["username"])


# ── Die 綁定與追溯 ──────────────────────────────────────────
@router.post("/dies/assign", dependencies=[CanOperate], summary="綁定良品晶粒到批號成品序號")
async def assign(db: DB, payload: DieAssignIn, user: CurrentUser):
    return await wafermap_service.assign_dies(db, payload.model_dump(), user["username"])


@router.post("/dies/ft-results", dependencies=[CanOperate], summary="回寫成品測試 Bin 到晶粒")
async def ft_results(db: DB, payload: DieResultIn, user: CurrentUser):
    return await wafermap_service.record_ft_results(db, payload.model_dump(), user["username"])


@router.get("/lots/{lot_id}/map-summary", dependencies=[CanRead], summary="批號投入晶圓的 Map 總覽")
async def lot_map_summary(db: DB, lot_id: str):
    return await wafermap_service.lot_map_summary(db, lot_id)


@router.get("/lots/{lot_id}/dies", dependencies=[CanRead], summary="批號的晶粒綁定明細")
async def lot_dies(
    db: DB,
    lot_id: str,
    status_: Annotated[str | None, Query(alias="status")] = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    return await wafermap_service.lot_dies(db, lot_id, skip, limit, status_)


@router.get("/lots/{lot_id}/die-summary", dependencies=[CanRead], summary="批號晶粒來源與測試分布")
async def die_summary(db: DB, lot_id: str):
    return await wafermap_service.die_summary(db, lot_id)


@router.get("/lots/{lot_id}/units/{unit_seq}", dependencies=[CanRead], summary="成品序號 → 晶圓座標")
async def die_of_unit(db: DB, lot_id: str, unit_seq: int):
    return await wafermap_service.die_of_unit(db, lot_id, unit_seq)


@router.get("/maps/{wafer_id}/dies/{die_x}/{die_y}", dependencies=[CanRead], summary="晶圓座標 → 成品序號")
async def unit_of_die(db: DB, wafer_id: str, die_x: int, die_y: int):
    return await wafermap_service.unit_of_die(db, wafer_id, die_x, die_y)


@router.get("/maps/{wafer_id}/area", dependencies=[CanRead], summary="圈選晶圓區域，列出受影響批號")
async def area(db: DB, wafer_id: str, x_min: int, x_max: int, y_min: int, y_max: int):
    return await wafermap_service.dies_in_area(db, wafer_id, x_min, x_max, y_min, y_max)
