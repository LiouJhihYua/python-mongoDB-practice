"""材料 API：主檔、進料、庫存異動、低水位警示。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.master import MaterialIn, MaterialReceiptIn
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import master_service, material_service

router = APIRouter(prefix="/api/materials", tags=["材料"])

CanEdit = Depends(require_roles(Role.ENGINEER, Role.PLANNER))
CanRead = Depends(get_current_user)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="材料建檔")
async def create(db: DB, payload: MaterialIn, user: CurrentUser):
    return await master_service.materials.create(db, payload.model_dump(), user["username"])


@router.get("", dependencies=[CanRead], summary="材料清單")
async def list_all(
    db: DB,
    material_type: str | None = None,
    active: bool | None = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await master_service.materials.list(
        db, {"material_type": material_type, "active": active}, skip, limit
    )


@router.get("/shortage", dependencies=[CanRead], summary="低於安全庫存的材料")
async def shortage(db: DB):
    return await material_service.below_safety_stock(db)


@router.get("/transactions", dependencies=[CanRead], summary="庫存異動履歷")
async def transactions(
    db: DB,
    material_id: str | None = None,
    lot_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await material_service.transactions(db, material_id, lot_id, limit)


@router.post("/receive", dependencies=[CanEdit], summary="材料進料")
async def receive(db: DB, payload: MaterialReceiptIn, user: CurrentUser):
    return await material_service.receive(db, payload.model_dump(), user["username"])


@router.get("/{material_id}", dependencies=[CanRead], summary="材料明細")
async def get_one(db: DB, material_id: str):
    return await master_service.materials.get(db, material_id)
