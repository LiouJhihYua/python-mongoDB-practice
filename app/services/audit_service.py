"""稽核軌跡：記錄誰對系統做了什麼，供內稽與客戶稽核調閱。

批號履歷（lot_history）記的是「產品經歷了什麼」，稽核軌跡記的是
「誰動了系統」—— 客戶稽核時通常兩份都要。

紀錄分兩種：
* ``API``  —— 每一次異動類 HTTP 請求（誰、什麼時間、打了哪支 API、成功與否）
* ``DATA`` —— 主檔異動的欄位層級明細（敏感欄位會遮蔽）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import COL_AUDIT
from app.models.base import clean_all, shift_of, utcnow

#: 不得寫入稽核紀錄的敏感欄位
REDACTED_KEYS = frozenset(
    {"password", "new_password", "old_password", "access_token", "jwt_secret", "hashed_password"}
)
REDACTED = "***"

#: 不留稽核的路徑（避免把帶密碼的登入流量寫進來）
SKIP_PATHS = ("/api/auth/login", "/api/auth/token")

KIND_API = "API"
KIND_DATA = "DATA"


def redact(value: Any) -> Any:
    """遞迴遮蔽敏感欄位。"""
    if isinstance(value, dict):
        return {k: (REDACTED if k in REDACTED_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


async def record_api(
    db, actor: str | None, method: str, path: str, status_code: int, duration_ms: float, query: str = ""
) -> None:
    now = utcnow()
    await db[COL_AUDIT].insert_one(
        {
            "kind": KIND_API,
            "actor": actor or "(未登入)",
            "method": method,
            "path": path,
            "query": query,
            "status_code": status_code,
            "success": status_code < 400,
            "duration_ms": round(duration_ms, 1),
            "timestamp": now,
            "shift": shift_of(now),
        }
    )


async def record_change(
    db, actor: str, action: str, collection: str, key: str, payload: dict | None = None
) -> None:
    """主檔異動的欄位層級稽核。"""
    now = utcnow()
    await db[COL_AUDIT].insert_one(
        {
            "kind": KIND_DATA,
            "actor": actor,
            "action": action,
            "collection": collection,
            "key": key,
            "fields": sorted(payload) if payload else [],
            "payload": redact(payload) if payload else None,
            "success": True,
            "timestamp": now,
            "shift": shift_of(now),
        }
    )


async def list_audit(
    db,
    kind: str | None = None,
    actor: str | None = None,
    path: str | None = None,
    success: bool | None = None,
    limit: int = 200,
) -> list[dict]:
    filt: dict[str, Any] = {}
    if kind:
        filt["kind"] = kind
    if actor:
        filt["actor"] = actor
    if path:
        filt["path"] = {"$regex": path}
    if success is not None:
        filt["success"] = success
    cursor = db[COL_AUDIT].find(filt).sort([("timestamp", -1)]).limit(limit)
    return clean_all(await cursor.to_list(length=limit))


async def activity_summary(db, start: datetime, end: datetime) -> list[dict]:
    """各使用者的操作次數與失敗率。"""
    rows = await db[COL_AUDIT].aggregate(
        [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": "$actor",
                    "operations": {"$sum": 1},
                    "failures": {"$sum": {"$cond": ["$success", 0, 1]}},
                }
            },
            {"$sort": {"operations": -1}},
        ]
    ).to_list(length=None)
    return [
        {
            "actor": r["_id"],
            "operations": r["operations"],
            "failures": r["failures"],
            "failure_rate": round(r["failures"] / r["operations"], 4) if r["operations"] else 0.0,
        }
        for r in rows
    ]
