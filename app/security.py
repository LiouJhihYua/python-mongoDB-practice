"""認證與授權：PBKDF2 密碼雜湊 + HS256 JWT（全部使用標準函式庫，無原生相依）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Annotated, Iterable

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.config import settings
from app.database import COL_USERS, get_db
from app.models.base import utcnow
from app.models.enums import Role

PBKDF2_ROUNDS = 240_000
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ── 密碼 ────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = hashed.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), digest_hex)


# ── JWT（HS256） ────────────────────────────────────────────
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def create_access_token(subject: str, roles: Iterable[str], expires_minutes: int | None = None) -> str:
    now = utcnow()
    exp = now + timedelta(minutes=expires_minutes or settings.jwt_expire_minutes)
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64e(
        json.dumps(
            {"sub": subject, "roles": list(roles), "iat": int(now.timestamp()), "exp": int(exp.timestamp())},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(settings.jwt_secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64e(sig)}"


def decode_access_token(token: str) -> dict:
    try:
        header, payload, sig = token.split(".")
    except ValueError:
        raise _credentials_error("Token 格式錯誤")
    expected = hmac.new(
        settings.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64d(sig), expected):
        raise _credentials_error("Token 簽章驗證失敗")
    data = json.loads(_b64d(payload))
    if data.get("exp", 0) < int(utcnow().timestamp()):
        raise _credentials_error("Token 已過期，請重新登入")
    return data


def _credentials_error(msg: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=msg,
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── FastAPI 相依 ────────────────────────────────────────────
async def get_current_user(token: Annotated[str | None, Depends(oauth2_scheme)]) -> dict:
    if not token:
        raise _credentials_error("需要登入")
    data = decode_access_token(token)
    user = await get_db()[COL_USERS].find_one({"username": data["sub"]})
    if user is None:
        raise _credentials_error("使用者不存在")
    if not user.get("active", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "帳號已停用")
    return user


CurrentUser = Annotated[dict, Depends(get_current_user)]


def require_roles(*roles: Role):
    """產生角色檢查相依；admin 一律放行。"""
    allowed = {str(r) for r in roles}

    async def _check(user: CurrentUser) -> dict:
        user_roles = set(user.get("roles", []))
        if Role.ADMIN in user_roles or user_roles & allowed:
            return user
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"權限不足：需要 {sorted(allowed)} 其中之一，目前為 {sorted(user_roles)}",
        )

    return _check


#: 常用角色組合
RequireAdmin = Depends(require_roles(Role.ADMIN))
RequireEngineer = Depends(require_roles(Role.ENGINEER))
RequirePlanner = Depends(require_roles(Role.PLANNER))
RequireOperator = Depends(require_roles(Role.OPERATOR, Role.ENGINEER))
RequireQC = Depends(require_roles(Role.QC, Role.ENGINEER))
RequireAnyUser = Depends(get_current_user)
