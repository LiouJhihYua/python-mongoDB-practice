"""抽樣檢驗 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.models.enums import Role
from app.models.sampling import InspectionJudgeIn, SamplingPlanIn, SamplingPlanUpdate
from app.routers.deps import DB, Window
from app.security import CurrentUser, get_current_user, require_roles
from app.services import sampling_service

router = APIRouter(prefix="/api/sampling", tags=["抽樣檢驗"])

CanEdit = Depends(require_roles(Role.QC, Role.ENGINEER))
CanInspect = Depends(require_roles(Role.QC, Role.OPERATOR, Role.ENGINEER))
CanRead = Depends(get_current_user)


# ── 抽樣計畫 ────────────────────────────────────────────────
@router.post("/plans", status_code=status.HTTP_201_CREATED, dependencies=[CanEdit], summary="建立抽樣計畫")
async def create_plan(db: DB, payload: SamplingPlanIn, user: CurrentUser):
    return await sampling_service.create_plan(db, payload.model_dump(), user["username"])


@router.get("/plans", dependencies=[CanRead], summary="抽樣計畫清單")
async def list_plans(db: DB, op_code: str | None = None, active: bool | None = None):
    return await sampling_service.list_plans(db, op_code, active)


@router.get("/plans/{plan_code}", dependencies=[CanRead], summary="抽樣計畫明細（含批量級距）")
async def get_plan(db: DB, plan_code: str):
    return await sampling_service.get_plan(db, plan_code)


@router.patch("/plans/{plan_code}", dependencies=[CanEdit], summary="更新抽樣計畫")
async def update_plan(db: DB, plan_code: str, payload: SamplingPlanUpdate, user: CurrentUser):
    return await sampling_service.update_plan(
        db, plan_code, payload.model_dump(exclude_unset=True), user["username"]
    )


@router.delete("/plans/{plan_code}", dependencies=[CanEdit], summary="刪除抽樣計畫")
async def delete_plan(db: DB, plan_code: str, user: CurrentUser):
    return await sampling_service.delete_plan(db, plan_code, user["username"])


@router.get("/plans/{plan_code}/lookup", dependencies=[CanRead], summary="查某批量該抽幾顆")
async def lookup(db: DB, plan_code: str, lot_size: Annotated[int, Query(ge=1)]):
    plan = await sampling_service.get_plan(db, plan_code)
    level = sampling_service.level_for(plan["levels"], lot_size)
    return {
        "plan_code": plan_code, "lot_size": lot_size, "level": level,
        "z14_suggestion": sampling_service.z14_code_letter(lot_size),
    }


@router.get("/z14", dependencies=[CanRead], summary="Z1.4 樣本大小代字對照（建表輔助）")
async def z14(lot_size: Annotated[int, Query(ge=1)]):
    return sampling_service.z14_code_letter(lot_size)


# ── 檢驗紀錄 ────────────────────────────────────────────────
@router.get("/inspections", dependencies=[CanRead], summary="檢驗紀錄")
async def list_inspections(
    db: DB,
    lot_id: str | None = None,
    op_code: str | None = None,
    result: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return await sampling_service.list_inspections(db, lot_id, op_code, result, limit)


@router.get("/inspections/pending", dependencies=[CanRead], summary="待判定的檢驗")
async def pending(db: DB, op_code: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return await sampling_service.pending_inspections(db, op_code, limit)


@router.post("/inspections/judge", dependencies=[CanInspect], summary="回報檢出不良並判定允收／拒收")
async def judge(db: DB, payload: InspectionJudgeIn, user: CurrentUser):
    return await sampling_service.judge(db, payload.model_dump(), user["username"])


@router.get("/summary", dependencies=[CanRead], summary="抽檢概況（驗了幾批、拒收率）")
async def summary(db: DB, window: Window):
    start, end = window
    return await sampling_service.summary(db, start, end)
