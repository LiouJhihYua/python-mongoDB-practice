"""治具壽命管理 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.master import ToolIn, ToolMountIn
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import tool_service

router = APIRouter(prefix="/api/tools", tags=["治具"])

CanEdit = Depends(require_roles(Role.ENGINEER))
CanOperate = Depends(require_roles(Role.OPERATOR, Role.ENGINEER))
CanRead = Depends(get_current_user)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="治具建檔")
async def create(db: DB, payload: ToolIn, user: CurrentUser):
    return await tool_service.create_tool(db, payload.model_dump(), user["username"])


@router.get("", dependencies=[CanRead], summary="治具清單（含使用率與剩餘壽命）")
async def list_all(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    eq_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await tool_service.list_tools(db, status_, eq_id, limit)


@router.get("/attention", dependencies=[CanRead], summary="需更換或接近壽命的治具")
async def attention(db: DB):
    return await tool_service.attention_list(db)


@router.post("/mount", dependencies=[CanOperate], summary="治具上機")
async def mount(db: DB, payload: ToolMountIn, user: CurrentUser):
    return await tool_service.mount(db, payload.tool_id, payload.eq_id, user["username"], payload.remark)


@router.post("/{tool_id}/unmount", dependencies=[CanOperate], summary="治具下機")
async def unmount(db: DB, tool_id: str, user: CurrentUser, scrap: bool = False, remark: str = ""):
    return await tool_service.unmount(db, tool_id, user["username"], scrap, remark)


@router.post("/replace", dependencies=[CanOperate], summary="換刀（舊治具下機、新治具上機並復機）")
async def replace(db: DB, old_tool_id: str, new_tool_id: str, user: CurrentUser, remark: str = ""):
    return await tool_service.replace(db, old_tool_id, new_tool_id, user["username"], remark)


@router.get("/{tool_id}/logs", dependencies=[CanRead], summary="治具履歷")
async def logs(db: DB, tool_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return await tool_service.tool_logs(db, tool_id, limit)
