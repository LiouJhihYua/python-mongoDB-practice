"""使用者服務。"""

from __future__ import annotations

import logging

import asyncpg

from app.config import settings
from app.database import T_USERS, fetch_all, fetch_one
from app.errors import DuplicateError, NotFoundError, ValidationError
from app.models.base import plain_values, utcnow
from app.models.enums import Role
from app.security import hash_password, verify_password

logger = logging.getLogger(__name__)

#: 對外回傳的欄位，永遠不含密碼雜湊
PUBLIC_COLUMNS = (
    "id, username, full_name, employee_no, department, roles, certifications, "
    "active, last_login_at, created_at, created_by, updated_at, updated_by"
)


async def create_user(db, payload: dict, actor: str = "system") -> dict:
    data = plain_values(payload)
    try:
        row = await fetch_one(
            db,
            f"""
            INSERT INTO {T_USERS}
                (username, hashed_password, full_name, employee_no, department,
                 roles, certifications, active, created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
            RETURNING {PUBLIC_COLUMNS}
            """,
            data["username"], hash_password(data["password"]), data.get("full_name", ""),
            data.get("employee_no", ""), data.get("department", ""),
            data.get("roles") or [], data.get("certifications") or [],
            data.get("active", True), actor,
        )
    except asyncpg.UniqueViolationError:
        raise DuplicateError(f"帳號 {data['username']} 已存在")
    return row


async def get_user(db, username: str) -> dict:
    row = await fetch_one(db, f"SELECT {PUBLIC_COLUMNS} FROM {T_USERS} WHERE username = $1", username)
    if row is None:
        raise NotFoundError(f"找不到帳號：{username}")
    return row


async def list_users(db, skip: int = 0, limit: int = 100, active: bool | None = None) -> dict:
    total = await db.fetchval(
        f"SELECT count(*) FROM {T_USERS} WHERE ($1::boolean IS NULL OR active = $1)", active
    )
    items = await fetch_all(
        db,
        f"""
        SELECT {PUBLIC_COLUMNS} FROM {T_USERS}
        WHERE ($1::boolean IS NULL OR active = $1)
        ORDER BY username OFFSET $2 LIMIT $3
        """,
        active, skip, limit,
    )
    return {"items": items, "total": total, "skip": skip, "limit": limit}


async def update_user(db, username: str, patch: dict, actor: str = "system") -> dict:
    data = plain_values({k: v for k, v in patch.items() if v is not None})
    password = data.pop("password", None)

    allowed = {"full_name", "employee_no", "department", "roles", "certifications", "active"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if password:
        updates["hashed_password"] = hash_password(password)
    if not updates:
        raise ValidationError("沒有需要更新的欄位")

    assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(updates, start=1))
    row = await fetch_one(
        db,
        f"""
        UPDATE {T_USERS} SET {assignments}, updated_at = now(), updated_by = ${len(updates) + 1}
        WHERE username = ${len(updates) + 2}
        RETURNING {PUBLIC_COLUMNS}
        """,
        *updates.values(), actor, username,
    )
    if row is None:
        raise NotFoundError(f"找不到帳號：{username}")
    return row


async def authenticate(db, username: str, password: str) -> dict | None:
    user = await fetch_one(db, f"SELECT * FROM {T_USERS} WHERE username = $1", username)
    if user is None or not verify_password(password, user.get("hashed_password", "")):
        return None
    if not user.get("active", True):
        raise ValidationError("帳號已停用")
    await db.execute(f"UPDATE {T_USERS} SET last_login_at = $1 WHERE username = $2", utcnow(), username)
    return user


def is_certified(user: dict, op_code: str) -> bool:
    """檢查作業員是否具備該站別資格；工程師與管理者視為全站合格。"""
    roles = set(user.get("roles") or [])
    if roles & {Role.ADMIN.value, Role.ENGINEER.value}:
        return True
    return op_code in set(user.get("certifications") or [])


async def ensure_bootstrap_admin(db) -> None:
    """首次啟動時建立預設管理者，方便立即登入。"""
    if await db.fetchval(f"SELECT count(*) FROM {T_USERS}") > 0:
        return
    await create_user(
        db,
        {
            "username": settings.bootstrap_admin,
            "password": settings.bootstrap_admin_password,
            "full_name": "系統管理者",
            "employee_no": "ADM001",
            "department": "IT",
            "roles": [Role.ADMIN.value],
            "certifications": [],
            "active": True,
        },
        actor="bootstrap",
    )
    logger.warning("已建立預設管理者帳號 %s，請立即修改密碼", settings.bootstrap_admin)
