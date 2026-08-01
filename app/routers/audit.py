"""稽核軌跡 API（限管理者與品保調閱）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.models.enums import Role
from app.routers.deps import DB, Window
from app.security import require_roles
from app.services import audit_service

router = APIRouter(prefix="/api/audit", tags=["稽核"])

CanAudit = Depends(require_roles(Role.ADMIN, Role.QC))


@router.get("", dependencies=[CanAudit], summary="稽核紀錄查詢")
async def list_audit(
    db: DB,
    kind: Annotated[str | None, Query(description="API（介面操作）或 DATA（主檔異動）")] = None,
    actor: str | None = None,
    path: str | None = None,
    success: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await audit_service.list_audit(db, kind, actor, path, success, limit)


@router.get("/summary", dependencies=[CanAudit], summary="各使用者操作統計")
async def summary(db: DB, window: Window):
    start, end = window
    return await audit_service.activity_summary(db, start, end)
