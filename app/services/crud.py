"""主檔共用的資料表存取（以業務主鍵為準，停用採軟刪除）。

欄位名稱一律取自各資料表宣告的白名單，不會把使用者輸入拼進 SQL，
所有值都以參數化查詢綁定。
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app.errors import DuplicateError, NotFoundError, ValidationError
from app.models.base import plain_values
from app.services import audit_service


class Table:
    """以業務主鍵（如 device_id）操作單一資料表。"""

    def __init__(self, name: str, key: str, label: str):
        self.name = name
        self.key = key
        self.label = label
        #: 欄位白名單於首次使用時自資料庫反射，避免與 schema.sql 漂移
        self.columns: tuple[str, ...] = ()

    # ── 內部工具 ────────────────────────────────────────────
    async def ensure_columns(self, db) -> tuple[str, ...]:
        if not self.columns:
            rows = await db.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1",
                self.name,
            )
            if not rows:
                raise ValidationError(f"資料表不存在：{self.name}")
            self.columns = tuple(r["column_name"] for r in rows)
        return self.columns

    def _known(self, payload: dict, *, skip_none: bool = False) -> dict:
        """只保留這張表真的有的欄位，避免多餘的鍵造成 SQL 失敗。"""
        data = plain_values(payload)
        return {
            k: v for k, v in data.items()
            if k in self.columns and not (skip_none and v is None)
        }

    def _column(self, name: str, default: str | None = None) -> str:
        if name in self.columns:
            return name
        if default is not None:
            return default
        raise ValidationError(f"{self.label}沒有欄位：{name}")

    # ── CRUD ────────────────────────────────────────────────
    async def create(self, db, payload: dict, actor: str = "system") -> dict:
        await self.ensure_columns(db)
        data = self._known(payload)
        key_value = data.get(self.key)
        if key_value is None:
            raise ValidationError(f"缺少{self.label}主鍵：{self.key}")
        if "created_by" in self.columns:
            data["created_by"] = actor
        if "updated_by" in self.columns:
            data["updated_by"] = actor

        cols = list(data)
        placeholders = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        sql = (
            f"INSERT INTO {self.name} ({', '.join(cols)}) "
            f"VALUES ({placeholders}) RETURNING *"
        )
        try:
            row = await db.fetchrow(sql, *data.values())
        except asyncpg.UniqueViolationError:
            raise DuplicateError(f"{self.label} {key_value} 已存在")
        except asyncpg.ForeignKeyViolationError as exc:
            raise ValidationError(f"{self.label} {key_value} 參照到不存在的資料：{exc.detail or exc}")
        except asyncpg.CheckViolationError as exc:
            raise ValidationError(f"{self.label} {key_value} 不符合欄位限制：{exc.constraint_name}")

        await audit_service.record_change(db, actor, "CREATE", self.name, str(key_value), payload)
        return dict(row)

    async def get(self, db, key: str, required: bool = True) -> dict | None:
        row = await db.fetchrow(f"SELECT * FROM {self.name} WHERE {self.key} = $1", key)
        if row is None and required:
            raise NotFoundError(f"找不到{self.label}：{key}")
        return dict(row) if row is not None else None

    #: 舊介面沿用名稱，PostgreSQL 下與 get 相同
    raw = get

    async def list(
        self,
        db,
        filters: dict[str, Any] | None = None,
        skip: int = 0,
        limit: int = 200,
        order_by: str | None = None,
        descending: bool = False,
    ) -> dict:
        await self.ensure_columns(db)
        clauses, args = [], []
        for column, value in (filters or {}).items():
            if value is None:
                continue
            if column not in self.columns:
                raise ValidationError(f"{self.label}沒有欄位：{column}")
            args.append(plain_values(value))
            clauses.append(f"{column} = ${len(args)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        total = await db.fetchval(f"SELECT count(*) FROM {self.name} {where}", *args)
        order = self._column(order_by or self.key)
        rows = await db.fetch(
            f"SELECT * FROM {self.name} {where} ORDER BY {order} {'DESC' if descending else 'ASC'} "
            f"OFFSET ${len(args) + 1} LIMIT ${len(args) + 2}",
            *args, skip, limit,
        )
        return {"items": [dict(r) for r in rows], "total": total, "skip": skip, "limit": limit}

    async def update(self, db, key: str, patch: dict, actor: str = "system") -> dict:
        await self.ensure_columns(db)
        data = self._known(patch, skip_none=True)
        data.pop(self.key, None)
        if not data:
            raise ValidationError("沒有需要更新的欄位")
        if "updated_by" in self.columns:
            data["updated_by"] = actor

        assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(data, start=1))
        if "updated_at" in self.columns:
            assignments += ", updated_at = now()"
        try:
            row = await db.fetchrow(
                f"UPDATE {self.name} SET {assignments} WHERE {self.key} = ${len(data) + 1} RETURNING *",
                *data.values(), key,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise ValidationError(f"更新{self.label} {key} 時參照到不存在的資料：{exc.detail or exc}")
        except asyncpg.CheckViolationError as exc:
            raise ValidationError(f"更新{self.label} {key} 不符合欄位限制：{exc.constraint_name}")
        if row is None:
            raise NotFoundError(f"找不到{self.label}：{key}")

        await audit_service.record_change(db, actor, "UPDATE", self.name, key, patch)
        return dict(row)

    async def deactivate(self, db, key: str, actor: str = "system") -> dict:
        """主檔一律軟刪除，避免破壞既有生產紀錄的追溯。"""
        return await self.update(db, key, {"active": False}, actor)
