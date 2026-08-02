"""配方管理 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.recipe import (
    RecipeDownloadIn,
    RecipeIn,
    RecipeLoadIn,
    RecipeReleaseIn,
    RecipeSelectIn,
    RecipeUpdate,
)
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import recipe_service, secs_service

router = APIRouter(prefix="/api/recipes", tags=["配方管理"])

CanEdit = Depends(require_roles(Role.ENGINEER))
CanOperate = Depends(require_roles(Role.OPERATOR, Role.ENGINEER))
CanRead = Depends(get_current_user)


# ── 配方主檔 ────────────────────────────────────────────────
@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立配方草稿")
async def create(db: DB, payload: RecipeIn, user: CurrentUser):
    return await recipe_service.create_recipe(db, payload.model_dump(), user["username"])


@router.get("", dependencies=[CanRead], summary="配方清單")
async def list_all(
    db: DB,
    op_code: str | None = None,
    device_id: str | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await recipe_service.list_recipes(db, op_code, device_id, status_, limit)


@router.get("/status", dependencies=[CanRead], summary="配方看板：各機台載了什麼")
async def overview(db: DB):
    return await recipe_service.status_overview(db)


@router.get("/approved", dependencies=[CanRead], summary="某站別／料號核可可用的配方")
async def approved(db: DB, op_code: str, device_id: str = "", eq_model: str = ""):
    return await recipe_service.approved_recipes(db, op_code, device_id, eq_model)


@router.get("/checks", dependencies=[CanRead], summary="進站配方比對紀錄")
async def checks(
    db: DB,
    lot_id: str | None = None,
    eq_id: str | None = None,
    passed: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await recipe_service.list_checks(db, lot_id, eq_id, passed, limit)


@router.get("/{ppid}", dependencies=[CanRead], summary="配方內容（預設取最新版）")
async def get_one(db: DB, ppid: str, version: int | None = None):
    return await recipe_service.get_recipe(db, ppid, version)


@router.post("/{ppid}/revise", dependencies=[CanEdit], summary="以現行版本複製出新草稿")
async def revise(db: DB, ppid: str, user: CurrentUser):
    return await recipe_service.revise_recipe(db, ppid, user["username"])


@router.patch("/{ppid}/{version}", dependencies=[CanEdit], summary="修改草稿內容")
async def update(db: DB, ppid: str, version: int, payload: RecipeUpdate, user: CurrentUser):
    return await recipe_service.update_recipe(
        db, ppid, version, payload.model_dump(exclude_unset=True), user["username"]
    )


@router.post("/{ppid}/{version}/release", dependencies=[CanEdit], summary="發行配方")
async def release(db: DB, ppid: str, version: int, payload: RecipeReleaseIn, user: CurrentUser):
    return await recipe_service.release_recipe(db, ppid, version, payload.model_dump(), user["username"])


@router.post("/{ppid}/{version}/obsolete", dependencies=[CanEdit], summary="作廢配方版本")
async def obsolete(db: DB, ppid: str, version: int, user: CurrentUser):
    return await recipe_service.obsolete_recipe(db, ppid, version, user["username"])


# ── 機台配方 ────────────────────────────────────────────────
@router.get("/equipments/{eq_id}/loaded", dependencies=[CanRead], summary="機台目前載入的配方")
async def loaded(db: DB, eq_id: str):
    return await recipe_service.loaded_recipe(db, eq_id)


@router.put("/equipments/{eq_id}/loaded", dependencies=[CanOperate], summary="人工回報機台配方")
async def set_loaded(db: DB, eq_id: str, payload: RecipeLoadIn, user: CurrentUser):
    return await recipe_service.set_loaded(
        db, eq_id, payload.model_dump(), user["username"], recipe_service.SOURCE_MANUAL
    )


@router.get("/equipments/{eq_id}/ppids", dependencies=[CanEdit], summary="查詢機台上有哪些配方（S7F19）")
async def equipment_ppids(db: DB, eq_id: str, user: CurrentUser):
    return await secs_service.list_equipment_recipes(db, eq_id, user["username"])


@router.get("/equipments/{eq_id}/upload", dependencies=[CanEdit], summary="取回機台上的配方內容（S7F5）")
async def upload(db: DB, eq_id: str, ppid: str, user: CurrentUser):
    return await secs_service.upload_recipe(db, eq_id, ppid, user["username"])


@router.post("/equipments/{eq_id}/download", dependencies=[CanEdit], summary="下發配方到機台（S7F3）")
async def download(db: DB, eq_id: str, payload: RecipeDownloadIn, user: CurrentUser):
    return await secs_service.download_recipe(
        db, eq_id, payload.ppid, payload.version, user["username"]
    )


@router.post("/equipments/{eq_id}/select", dependencies=[CanOperate], summary="切換機台配方（S2F41）")
async def select(db: DB, eq_id: str, payload: RecipeSelectIn, user: CurrentUser):
    return await secs_service.select_recipe(db, eq_id, payload.ppid, user["username"])
