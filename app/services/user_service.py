"""使用者服務。"""

from __future__ import annotations

import logging
from datetime import timedelta

import asyncpg

from app.config import settings
from app.database import T_USERS, fetch_all, fetch_one
from app.errors import DuplicateError, NotFoundError, ValidationError
from app.models.base import ensure_aware, plain_values, utcnow
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


class AccountLocked(ValidationError):
    """連續失敗次數過多，帳號暫時鎖定。"""

    status_code = 429
    code = "ACCOUNT_LOCKED"


async def _register_failure(db, username: str) -> None:
    """累計失敗次數；達上限就鎖一段時間。

    帳號不存在時什麼也不做 —— 否則回應時間的差異會洩漏「這個帳號存在」。
    """
    now = utcnow()
    row = await fetch_one(
        db,
        f"""
        UPDATE {T_USERS}
        SET failed_logins = failed_logins + 1, last_failed_at = $1
        WHERE username = $2
        RETURNING failed_logins
        """,
        now, username,
    )
    if row is None or int(row["failed_logins"]) < settings.login_max_attempts:
        return
    await db.execute(
        f"UPDATE {T_USERS} SET locked_until = $1 WHERE username = $2",
        now + timedelta(minutes=settings.login_lockout_minutes), username,
    )
    logger.warning(
        "帳號 %s 連續失敗 %s 次，鎖定 %s 分鐘",
        username, row["failed_logins"], settings.login_lockout_minutes,
    )


async def authenticate(db, username: str, password: str) -> dict | None:
    user = await fetch_one(db, f"SELECT * FROM {T_USERS} WHERE username = $1", username)

    locked_until = (user or {}).get("locked_until")
    if locked_until is not None and ensure_aware(locked_until) > utcnow():
        remaining = int((ensure_aware(locked_until) - utcnow()).total_seconds() // 60) + 1
        raise AccountLocked(
            f"帳號已因連續登入失敗鎖定，請於 {remaining} 分鐘後再試，或聯絡管理者解鎖"
        )

    if user is None or not verify_password(password, user.get("hashed_password", "")):
        await _register_failure(db, username)
        return None
    if not user.get("active", True):
        raise ValidationError("帳號已停用")

    await db.execute(
        f"""
        UPDATE {T_USERS}
        SET last_login_at = $1, failed_logins = 0, locked_until = NULL
        WHERE username = $2
        """,
        utcnow(), username,
    )
    return user


async def unlock(db, username: str, actor: str) -> dict:
    """管理者手動解鎖。"""
    row = await fetch_one(
        db,
        f"""
        UPDATE {T_USERS} SET failed_logins = 0, locked_until = NULL,
                             updated_at = now(), updated_by = $1
        WHERE username = $2 RETURNING {PUBLIC_COLUMNS}
        """,
        actor, username,
    )
    if row is None:
        raise NotFoundError(f"找不到帳號：{username}")
    logger.info("帳號 %s 已由 %s 解鎖", username, actor)
    return row


async def locked_accounts(db) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT username, full_name, failed_logins, locked_until, last_failed_at
        FROM {T_USERS} WHERE locked_until IS NOT NULL AND locked_until > $1
        ORDER BY locked_until DESC
        """,
        utcnow(),
    )


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
