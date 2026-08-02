"""稽核軌跡：記錄誰對系統做了什麼，供內稽與客戶稽核調閱。

批號履歷（lot_history）記的是「產品經歷了什麼」，稽核軌跡記的是
「誰動了系統」—— 客戶稽核時通常兩份都要。

紀錄分兩種：
* ``API``  —— 每一次異動類 HTTP 請求（誰、什麼時間、打了哪支 API、成功與否）
* ``DATA`` —— 主檔異動的欄位層級明細（敏感欄位會遮蔽）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.database import T_AUDIT, fetch_all
from app.models.base import shift_of, utcnow

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


def _serialisable(value: Any) -> Any:
    """稽核內容要能存成 JSONB，時間類物件先轉字串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialisable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialisable(v) for v in value]
    return value


async def record_api(
    db, actor: str | None, method: str, path: str, status_code: int, duration_ms: float, query: str = ""
) -> None:
    now = utcnow()
    await db.execute(
        f"""
        INSERT INTO {T_AUDIT}
            (kind, actor, method, path, query, status_code, success, duration_ms, timestamp, shift)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        KIND_API, actor or "(未登入)", method, path, query,
        status_code, status_code < 400, round(duration_ms, 1), now, shift_of(now),
    )


async def record_change(
    db, actor: str, action: str, table_name: str, key: str, payload: dict | None = None
) -> None:
    """主檔異動的欄位層級稽核。"""
    now = utcnow()
    cleaned = _serialisable(redact(payload)) if payload else None
    await db.execute(
        f"""
        INSERT INTO {T_AUDIT}
            (kind, actor, action, table_name, key, fields, payload, success, timestamp, shift)
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8, $9)
        """,
        KIND_DATA, actor, action, table_name, key,
        sorted(payload) if payload else [], cleaned, now, shift_of(now),
    )


async def list_audit(
    db,
    kind: str | None = None,
    actor: str | None = None,
    path: str | None = None,
    success: bool | None = None,
    limit: int = 200,
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_AUDIT}
        WHERE ($1::text IS NULL OR kind = $1)
          AND ($2::text IS NULL OR actor = $2)
          AND ($3::text IS NULL OR path LIKE '%' || $3 || '%')
          AND ($4::boolean IS NULL OR success = $4)
        ORDER BY timestamp DESC, id DESC
        LIMIT $5
        """,
        kind, actor, path, success, limit,
    )


async def export_jsonl(db, before: datetime, limit: int = 100_000) -> str:
    """把保存期限外的稽核紀錄輸出成 JSON Lines，歸檔後才好清除。

    一行一筆，可以直接 gzip 起來丟冷儲存，需要時再 grep。
    """
    rows = await fetch_all(
        db,
        f"SELECT * FROM {T_AUDIT} WHERE timestamp < $1 ORDER BY timestamp, id LIMIT $2",
        before, limit,
    )
    return "\n".join(json.dumps(_serialisable(dict(r)), ensure_ascii=False) for r in rows)


async def purge(db, before: datetime, dry_run: bool = True) -> dict:
    """清除保存期限外的稽核紀錄。

    預設只試算不刪除 —— 稽核紀錄刪掉就回不來了，
    正式清除前應該先用 ``export_jsonl`` 歸檔。
    """
    count = int(
        await db.fetchval(f"SELECT count(*) FROM {T_AUDIT} WHERE timestamp < $1", before) or 0
    )
    oldest = await db.fetchval(f"SELECT min(timestamp) FROM {T_AUDIT}")
    if dry_run or count == 0:
        return {"before": before, "matched": count, "deleted": 0,
                "dry_run": True, "oldest": oldest}

    await db.execute(f"DELETE FROM {T_AUDIT} WHERE timestamp < $1", before)
    return {"before": before, "matched": count, "deleted": count,
            "dry_run": False, "oldest": oldest}


async def retention_status(db) -> dict:
    """保存概況：目前留了幾筆、最舊的是什麼時候、有多少已超過保存期限。"""
    cutoff = utcnow() - timedelta(days=settings.audit_retention_days)
    total = int(await db.fetchval(f"SELECT count(*) FROM {T_AUDIT}") or 0)
    expired = int(
        await db.fetchval(f"SELECT count(*) FROM {T_AUDIT} WHERE timestamp < $1", cutoff) or 0
    )
    oldest = await db.fetchval(f"SELECT min(timestamp) FROM {T_AUDIT}")
    return {
        "retention_days": settings.audit_retention_days,
        "cutoff": cutoff,
        "total": total,
        "expired": expired,
        "oldest": oldest,
    }


async def activity_summary(db, start: datetime, end: datetime) -> list[dict]:
    """各使用者的操作次數與失敗率。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT actor,
               count(*)                                    AS operations,
               count(*) FILTER (WHERE NOT success)         AS failures
        FROM {T_AUDIT}
        WHERE timestamp >= $1 AND timestamp < $2
        GROUP BY actor
        ORDER BY operations DESC
        """,
        start, end,
    )
    return [
        {
            "actor": r["actor"],
            "operations": r["operations"],
            "failures": r["failures"],
            "failure_rate": round(r["failures"] / r["operations"], 4) if r["operations"] else 0.0,
        }
        for r in rows
    ]
