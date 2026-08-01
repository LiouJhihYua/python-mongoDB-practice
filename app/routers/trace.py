"""追溯 API：客訴分析與異常圈選的主力工具。"""

from fastapi import APIRouter, Depends

from app.routers.deps import DB
from app.security import get_current_user
from app.services import trace_service

router = APIRouter(prefix="/api/trace", tags=["追溯"])

CanRead = Depends(get_current_user)


@router.get("/lots/{lot_id}/genealogy", dependencies=[CanRead], summary="批號族譜（拆／併關係）")
async def genealogy(db: DB, lot_id: str):
    return await trace_service.genealogy(db, lot_id)


@router.get("/lots/{lot_id}/backward", dependencies=[CanRead], summary="逆向追溯：這批貨怎麼做出來的")
async def backward(db: DB, lot_id: str):
    return await trace_service.backward_trace(db, lot_id)


@router.get("/wafers/{wafer_id}/forward", dependencies=[CanRead], summary="正向追溯：這片晶圓流向哪裡")
async def forward(db: DB, wafer_id: str):
    return await trace_service.forward_trace(db, wafer_id)


@router.get("/materials/{material_lot}/where-used", dependencies=[CanRead], summary="材料批號用在哪些生產批")
async def where_used(db: DB, material_lot: str):
    return await trace_service.where_used(db, material_lot)
