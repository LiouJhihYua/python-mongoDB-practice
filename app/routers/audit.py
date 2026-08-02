"""稽核軌跡 API（限管理者與品保調閱）。"""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.models.base import utcnow
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


@router.get("/retention", dependencies=[CanAudit], summary="稽核紀錄保存概況")
async def retention(db: DB):
    return await audit_service.retention_status(db)


@router.get(
    "/export.jsonl", dependencies=[CanAudit], response_class=PlainTextResponse,
    summary="歸檔匯出（JSON Lines）",
)
async def export_jsonl(
    db: DB,
    days: Annotated[int | None, Query(ge=1, description="匯出幾天前的資料，預設用保存期限")] = None,
    limit: Annotated[int, Query(ge=1, le=200_000)] = 100_000,
):
    before = utcnow() - timedelta(days=days or settings.audit_retention_days)
    content = await audit_service.export_jsonl(db, before, limit)
    return PlainTextResponse(
        content,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-before-{before:%Y%m%d}.jsonl"'},
    )


@router.delete(
    "", dependencies=[Depends(require_roles(Role.ADMIN))],
    summary="清除保存期限外的稽核紀錄（預設只試算）",
)
async def purge(
    db: DB,
    days: Annotated[int | None, Query(ge=1, description="清除幾天前的資料，預設用保存期限")] = None,
    confirm: Annotated[bool, Query(description="true 才會真的刪除；請先用匯出端點歸檔")] = False,
):
    before = utcnow() - timedelta(days=days or settings.audit_retention_days)
    return await audit_service.purge(db, before, dry_run=not confirm)
