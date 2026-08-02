"""認證與使用者管理。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordRequestForm

from app.config import settings
from app.models.user import LoginIn, Token, UserIn, UserOut, UserUpdate
from app.routers.deps import DB
from app.security import CurrentUser, create_access_token, require_roles
from app.services import user_service
from app.models.enums import Role

router = APIRouter(prefix="/api/auth", tags=["認證"])


async def _issue_token(db, username: str, password: str) -> Token:
    user = await user_service.authenticate(db, username, password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "帳號或密碼錯誤")
    return Token(
        access_token=create_access_token(user["username"], user.get("roles", [])),
        expires_in=settings.jwt_expire_minutes * 60,
        user=UserOut(**user),
    )


@router.post("/login", response_model=Token, summary="登入取得 Token")
async def login(db: DB, payload: LoginIn) -> Token:
    return await _issue_token(db, payload.username, payload.password)


@router.post("/token", response_model=Token, summary="OAuth2 表單登入（供 Swagger 使用）")
async def login_form(db: DB, form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> Token:
    return await _issue_token(db, form.username, form.password)


@router.get("/me", response_model=UserOut, summary="目前登入者")
async def me(user: CurrentUser) -> UserOut:
    return UserOut(**user)


@router.post("/refresh", response_model=Token, summary="換發 Token（延長班中登入）")
async def refresh(user: CurrentUser) -> Token:
    """憑仍然有效的 Token 換一張新的。

    一個班別 8 小時，跨班交接或加班時不該被迫重新登入；
    但 Token 一旦過期就必須重新驗證密碼，不提供離線續期。
    """
    return Token(
        access_token=create_access_token(user["username"], user.get("roles", [])),
        expires_in=settings.jwt_expire_minutes * 60,
        user=UserOut(**user),
    )


@router.get(
    "/locked", dependencies=[Depends(require_roles(Role.ADMIN))], summary="目前被鎖定的帳號"
)
async def locked(db: DB):
    return await user_service.locked_accounts(db)


@router.post(
    "/users/{username}/unlock",
    dependencies=[Depends(require_roles(Role.ADMIN))],
    summary="解鎖帳號",
)
async def unlock(db: DB, username: str, user: CurrentUser):
    return await user_service.unlock(db, username, user["username"])


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(Role.ADMIN))],
    summary="建立使用者",
)
async def create_user(db: DB, payload: UserIn, user: CurrentUser):
    return await user_service.create_user(db, payload.model_dump(), user["username"])


@router.get("/users", dependencies=[Depends(require_roles(Role.ADMIN))], summary="使用者清單")
async def list_users(
    db: DB,
    active: Annotated[bool | None, Query()] = None,
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    return await user_service.list_users(db, skip=skip, limit=limit, active=active)


@router.patch(
    "/users/{username}",
    dependencies=[Depends(require_roles(Role.ADMIN))],
    summary="更新使用者",
)
async def update_user(db: DB, username: str, payload: UserUpdate, user: CurrentUser):
    return await user_service.update_user(db, username, payload.model_dump(exclude_unset=True), user["username"])


@router.post("/change-password", summary="修改自己的密碼")
async def change_password(db: DB, user: CurrentUser, old_password: str, new_password: str):
    if await user_service.authenticate(db, user["username"], old_password) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密碼錯誤")
    if len(new_password) < 6:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "新密碼至少 6 碼")
    await user_service.update_user(db, user["username"], {"password": new_password}, user["username"])
    return {"ok": True, "message": "密碼已更新"}
