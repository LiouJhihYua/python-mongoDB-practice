"""使用者服務。"""

from __future__ import annotations

import logging

from app.config import settings
from app.database import COL_USERS
from app.errors import DuplicateError, NotFoundError, ValidationError
from app.models.base import clean, clean_all, mongo_encode, utcnow
from app.models.enums import Role
from app.security import hash_password, verify_password

logger = logging.getLogger(__name__)

PUBLIC_FIELDS = {
    "_id": 0,
    "hashed_password": 0,
}


async def create_user(db, payload: dict, actor: str = "system") -> dict:
    username = payload["username"]
    if await db[COL_USERS].find_one({"username": username}):
        raise DuplicateError(f"帳號 {username} 已存在")
    doc = mongo_encode({k: v for k, v in payload.items() if k != "password"})
    doc.update(
        hashed_password=hash_password(payload["password"]),
        created_at=utcnow(),
        created_by=actor,
        updated_at=utcnow(),
    )
    await db[COL_USERS].insert_one(doc)
    return await get_user(db, username)


async def get_user(db, username: str) -> dict:
    doc = await db[COL_USERS].find_one({"username": username}, PUBLIC_FIELDS)
    if doc is None:
        raise NotFoundError(f"找不到帳號：{username}")
    return clean(doc)


async def list_users(db, skip: int = 0, limit: int = 100, active: bool | None = None) -> dict:
    filt = {} if active is None else {"active": active}
    total = await db[COL_USERS].count_documents(filt)
    cursor = db[COL_USERS].find(filt, PUBLIC_FIELDS).sort([("username", 1)]).skip(skip).limit(limit)
    return {
        "items": clean_all(await cursor.to_list(length=limit)),
        "total": total,
        "skip": skip,
        "limit": limit,
    }


async def update_user(db, username: str, patch: dict, actor: str = "system") -> dict:
    update = {k: v for k, v in mongo_encode(patch).items() if v is not None and k != "password"}
    if patch.get("password"):
        update["hashed_password"] = hash_password(patch["password"])
    if not update:
        raise ValidationError("沒有需要更新的欄位")
    update.update(updated_at=utcnow(), updated_by=actor)
    result = await db[COL_USERS].update_one({"username": username}, {"$set": update})
    if result.matched_count == 0:
        raise NotFoundError(f"找不到帳號：{username}")
    return await get_user(db, username)


async def authenticate(db, username: str, password: str) -> dict | None:
    user = await db[COL_USERS].find_one({"username": username})
    if user is None or not verify_password(password, user.get("hashed_password", "")):
        return None
    if not user.get("active", True):
        raise ValidationError("帳號已停用")
    await db[COL_USERS].update_one({"username": username}, {"$set": {"last_login_at": utcnow()}})
    return user


def is_certified(user: dict, op_code: str) -> bool:
    """檢查作業員是否具備該站別資格；工程師與管理者視為全站合格。"""
    roles = set(user.get("roles", []))
    if roles & {Role.ADMIN.value, Role.ENGINEER.value}:
        return True
    return op_code in set(user.get("certifications", []))


async def ensure_bootstrap_admin(db) -> None:
    """首次啟動時建立預設管理者，方便立即登入。"""
    if await db[COL_USERS].count_documents({}) > 0:
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
    logger.warning(
        "已建立預設管理者帳號 %s，請立即修改密碼", settings.bootstrap_admin
    )
