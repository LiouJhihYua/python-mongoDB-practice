"""ERP 介接 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import PlainTextResponse

from app.models.enums import Role
from app.models.erp import (
    ERPCsvIn,
    ERPInboundBatchIn,
    ERPInboundIn,
    ERPOutboundAckIn,
    ERPOutboundBuildIn,
    ERPOutboundFailIn,
    ERPProcessIn,
)
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import erp_service

router = APIRouter(prefix="/api/erp", tags=["ERP 介接"])

#: 介接作業視同生管職權；查詢開放給所有登入者
CanIntegrate = Depends(require_roles(Role.PLANNER, Role.ENGINEER))
CanRead = Depends(get_current_user)


# ── 下行（ERP → MES）────────────────────────────────────────
@router.post(
    "/inbound", status_code=status.HTTP_202_ACCEPTED, dependencies=[CanIntegrate],
    summary="接收 ERP 單據（同一 external_id 重送不會重複建檔）",
)
async def inbound(db: DB, payload: ERPInboundIn, user: CurrentUser):
    return await erp_service.receive(db, payload.model_dump(), user["username"])


@router.post(
    "/inbound/batch", status_code=status.HTTP_202_ACCEPTED, dependencies=[CanIntegrate],
    summary="批次接收 ERP 單據",
)
async def inbound_batch(db: DB, payload: ERPInboundBatchIn, user: CurrentUser):
    return await erp_service.receive_batch(
        db, [d.model_dump() for d in payload.documents], user["username"]
    )


@router.post(
    "/inbound/csv", status_code=status.HTTP_202_ACCEPTED, dependencies=[CanIntegrate],
    summary="以 CSV 匯入 ERP 單據",
)
async def inbound_csv(db: DB, payload: ERPCsvIn, user: CurrentUser):
    return await erp_service.import_csv(
        db, str(payload.doc_type), payload.content, payload.external_id_column, user["username"]
    )


@router.get("/inbound", dependencies=[CanRead], summary="下行單據清單")
async def list_inbound(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    doc_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await erp_service.list_inbound(db, status_, doc_type, limit)


@router.post("/inbound/process", dependencies=[CanIntegrate], summary="套用待處理的下行單據")
async def process(db: DB, payload: ERPProcessIn, user: CurrentUser):
    doc_type = str(payload.doc_type) if payload.doc_type else None
    return await erp_service.process_pending(db, doc_type, payload.limit, user["username"])


@router.post("/inbound/{doc_id}/retry", dependencies=[CanIntegrate], summary="重試失敗的下行單據")
async def retry(db: DB, doc_id: int, user: CurrentUser):
    return await erp_service.retry_inbound(db, doc_id, user["username"])


# ── 上行（MES → ERP）────────────────────────────────────────
@router.post("/outbound/build", dependencies=[CanIntegrate], summary="彙整期間內的完工／領料／出貨／報廢")
async def build(db: DB, payload: ERPOutboundBuildIn, user: CurrentUser):
    return await erp_service.build_outbound(db, payload.model_dump(), user["username"])


@router.get("/outbound", dependencies=[CanRead], summary="上行單據清單")
async def list_outbound(
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    doc_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await erp_service.list_outbound(db, status_, doc_type, limit)


@router.post("/outbound/fetch", dependencies=[CanIntegrate], summary="ERP 拉單（取出待送並標記已送）")
async def fetch(
    db: DB,
    doc_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    mark_sent: bool = True,
):
    return await erp_service.fetch_outbound(db, doc_type, limit, mark_sent)


@router.post("/outbound/ack", dependencies=[CanIntegrate], summary="ERP 回覆已收單")
async def ack(db: DB, payload: ERPOutboundAckIn, user: CurrentUser):
    return await erp_service.ack_outbound(db, payload.ids, user["username"], payload.remark)


@router.post("/outbound/fail", dependencies=[CanIntegrate], summary="標記上行單據送出失敗")
async def fail(db: DB, payload: ERPOutboundFailIn, user: CurrentUser):
    return await erp_service.fail_outbound(db, payload.ids, payload.error, user["username"])


@router.post("/outbound/requeue", dependencies=[CanIntegrate], summary="重新排入待送佇列")
async def requeue(db: DB, payload: ERPOutboundAckIn, user: CurrentUser):
    return await erp_service.requeue_outbound(db, payload.ids, user["username"])


@router.get(
    "/outbound/export.csv", dependencies=[CanRead], response_class=PlainTextResponse,
    summary="上行單據匯出 CSV",
)
async def export_csv(
    db: DB,
    doc_type: str,
    status_: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
):
    content = await erp_service.export_csv(db, doc_type, status_, limit)
    return PlainTextResponse(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{doc_type.lower()}.csv"'},
    )


@router.get("/summary", dependencies=[CanRead], summary="介接看板")
async def summary(db: DB):
    return await erp_service.summary(db)
