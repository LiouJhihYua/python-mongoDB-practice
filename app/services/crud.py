"""主檔共用的 CRUD 行為（以業務主鍵為準，停用採軟刪除）。"""

from __future__ import annotations

from typing import Any

from app.errors import DuplicateError, NotFoundError
from app.models.base import clean, clean_all, mongo_encode, utcnow
from app.services import audit_service


class CRUD:
    """以業務主鍵（如 device_id）操作單一 collection。"""

    def __init__(self, collection: str, key_field: str, label: str):
        self.collection = collection
        self.key_field = key_field
        self.label = label

    async def create(self, db, payload: dict, actor: str = "system") -> dict:
        key = payload[self.key_field]
        if await db[self.collection].find_one({self.key_field: key}):
            raise DuplicateError(f"{self.label} {key} 已存在")
        doc = mongo_encode(dict(payload))
        doc.update(created_at=utcnow(), created_by=actor, updated_at=utcnow(), updated_by=actor)
        await db[self.collection].insert_one(doc)
        await audit_service.record_change(db, actor, "CREATE", self.collection, key, payload)
        return clean(await db[self.collection].find_one({self.key_field: key}))

    async def get(self, db, key: str, required: bool = True) -> dict | None:
        doc = await db[self.collection].find_one({self.key_field: key})
        if doc is None and required:
            raise NotFoundError(f"找不到{self.label}：{key}")
        return clean(doc) if doc else None

    async def raw(self, db, key: str, required: bool = True) -> dict | None:
        """取原始文件（不轉換 _id），供內部服務使用。"""
        doc = await db[self.collection].find_one({self.key_field: key})
        if doc is None and required:
            raise NotFoundError(f"找不到{self.label}：{key}")
        return doc

    async def list(
        self,
        db,
        filt: dict[str, Any] | None = None,
        skip: int = 0,
        limit: int = 200,
        sort: list[tuple[str, int]] | None = None,
    ) -> dict:
        filt = {k: v for k, v in (filt or {}).items() if v is not None}
        total = await db[self.collection].count_documents(filt)
        cursor = db[self.collection].find(filt).sort(sort or [(self.key_field, 1)]).skip(skip).limit(limit)
        return {
            "items": clean_all(await cursor.to_list(length=limit)),
            "total": total,
            "skip": skip,
            "limit": limit,
        }

    async def update(self, db, key: str, patch: dict, actor: str = "system") -> dict:
        patch = {k: v for k, v in mongo_encode(patch).items() if v is not None}
        patch.update(updated_at=utcnow(), updated_by=actor)
        result = await db[self.collection].update_one({self.key_field: key}, {"$set": patch})
        if result.matched_count == 0:
            raise NotFoundError(f"找不到{self.label}：{key}")
        await audit_service.record_change(db, actor, "UPDATE", self.collection, key, patch)
        return clean(await db[self.collection].find_one({self.key_field: key}))

    async def deactivate(self, db, key: str, actor: str = "system") -> dict:
        """主檔一律軟刪除，避免破壞既有生產紀錄的追溯。"""
        return await self.update(db, key, {"active": False}, actor)
