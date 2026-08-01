"""e-SOP 電子作業指導書。

紙本 SOP 在 OSAT 現場的老問題是「牆上貼的是第幾版」——
換版後作業員還照舊版做，客戶稽核時也拿不出誰看過哪一版的證據。

這裡把三件事綁在一起：

* **版本控制與生效日** —— 一個 SOP 代碼多個版本，只有 RELEASED 且已到生效時間的那版會被取用
* **適用版本查詢** —— 依站別 + 料號自動挑版本，料號專用版優先於通用版
* **作業員簽認** —— 站別設定 ``require_sop_ack`` 後，未簽認新版就進不了站
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import T_OPERATIONS, T_SOP_ACKS, T_SOPS, T_USERS, fetch_all, fetch_one
from app.errors import NotFoundError, PermissionError_, StateError, ValidationError
from app.models.base import plain_values, utcnow
from app.services import audit_service

STATUS_DRAFT = "DRAFT"
STATUS_RELEASED = "RELEASED"
STATUS_OBSOLETE = "OBSOLETE"

EDITABLE_COLUMNS = (
    "title", "device_id", "summary", "steps", "hazards", "ppe", "attachments", "require_ack",
)


# ── 版本管理 ────────────────────────────────────────────────
async def _next_version(db, sop_code: str) -> int:
    return int(
        await db.fetchval(f"SELECT coalesce(max(version), 0) FROM {T_SOPS} WHERE sop_code = $1", sop_code)
        or 0
    ) + 1


async def create_sop(db, payload: dict, actor: str) -> dict:
    async with db.transaction():
        op = await fetch_one(
            db, f"SELECT op_code, active FROM {T_OPERATIONS} WHERE op_code = $1", payload["op_code"]
        )
        if op is None:
            raise ValidationError(f"站別不存在：{payload['op_code']}")

        existing_draft = await fetch_one(
            db,
            f"SELECT version FROM {T_SOPS} WHERE sop_code = $1 AND status = $2 ORDER BY version DESC",
            payload["sop_code"], STATUS_DRAFT,
        )
        if existing_draft is not None:
            raise StateError(
                f"SOP {payload['sop_code']} 已有草稿版本 v{existing_draft['version']}，請先發行或作廢"
            )

        version = await _next_version(db, payload["sop_code"])
        now = utcnow()
        doc = await fetch_one(
            db,
            f"""
            INSERT INTO {T_SOPS}
                (sop_code, version, title, op_code, device_id, summary, steps, hazards,
                 ppe, attachments, status, require_ack, created_at, created_by, updated_at, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $13, $14)
            RETURNING *
            """,
            payload["sop_code"], version, payload["title"], payload["op_code"],
            payload.get("device_id", ""), payload.get("summary", ""),
            plain_values(payload.get("steps") or []), payload.get("hazards", ""),
            list(payload.get("ppe") or []), plain_values(payload.get("attachments") or []),
            STATUS_DRAFT, bool(payload.get("require_ack", True)), now, actor,
        )
        await audit_service.record_change(
            db, actor, "CREATE", T_SOPS, f"{payload['sop_code']} v{version}", payload
        )
        return doc


async def revise_sop(db, sop_code: str, actor: str) -> dict:
    """以目前生效的版本為基礎複製出新草稿 —— 改版時不用重打一遍。"""
    async with db.transaction():
        latest = await fetch_one(
            db, f"SELECT * FROM {T_SOPS} WHERE sop_code = $1 ORDER BY version DESC", sop_code
        )
        if latest is None:
            raise NotFoundError(f"找不到 SOP：{sop_code}")
        if latest["status"] == STATUS_DRAFT:
            raise StateError(f"SOP {sop_code} 已有草稿版本 v{latest['version']}，請直接編輯")

        version = int(latest["version"]) + 1
        now = utcnow()
        doc = await fetch_one(
            db,
            f"""
            INSERT INTO {T_SOPS}
                (sop_code, version, title, op_code, device_id, summary, steps, hazards,
                 ppe, attachments, status, require_ack, created_at, created_by, updated_at, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $13, $14)
            RETURNING *
            """,
            sop_code, version, latest["title"], latest["op_code"], latest["device_id"],
            latest["summary"], latest["steps"], latest["hazards"], list(latest["ppe"] or []),
            latest["attachments"], STATUS_DRAFT, latest["require_ack"], now, actor,
        )
        await audit_service.record_change(
            db, actor, "REVISE", T_SOPS, f"{sop_code} v{version}", {"from_version": latest["version"]}
        )
        return doc


async def get_sop(db, sop_code: str, version: int | None = None) -> dict:
    if version is not None:
        doc = await fetch_one(
            db, f"SELECT * FROM {T_SOPS} WHERE sop_code = $1 AND version = $2", sop_code, version
        )
    else:
        doc = await fetch_one(
            db, f"SELECT * FROM {T_SOPS} WHERE sop_code = $1 ORDER BY version DESC", sop_code
        )
    if doc is None:
        raise NotFoundError(f"找不到 SOP：{sop_code}" + (f" v{version}" if version else ""))
    return doc


async def update_sop(db, sop_code: str, version: int, patch: dict, actor: str) -> dict:
    async with db.transaction():
        doc = await fetch_one(
            db,
            f"SELECT * FROM {T_SOPS} WHERE sop_code = $1 AND version = $2 FOR UPDATE",
            sop_code, version,
        )
        if doc is None:
            raise NotFoundError(f"找不到 SOP：{sop_code} v{version}")
        if doc["status"] != STATUS_DRAFT:
            raise StateError(f"SOP {sop_code} v{version} 狀態為 {doc['status']}，僅草稿可修改")

        fields = {k: plain_values(v) for k, v in patch.items() if k in EDITABLE_COLUMNS and v is not None}
        if not fields:
            raise ValidationError("沒有需要更新的欄位")
        fields["updated_at"] = utcnow()
        fields["updated_by"] = actor
        assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(fields, start=1))
        result = await fetch_one(
            db,
            f"UPDATE {T_SOPS} SET {assignments} "
            f"WHERE sop_code = ${len(fields) + 1} AND version = ${len(fields) + 2} RETURNING *",
            *fields.values(), sop_code, version,
        )
        await audit_service.record_change(
            db, actor, "UPDATE", T_SOPS, f"{sop_code} v{version}", patch
        )
        return result


async def release_sop(db, sop_code: str, version: int, payload: dict, actor: str) -> dict:
    """發行版本；同一個 SOP 代碼的舊版自動作廢。"""
    async with db.transaction():
        doc = await fetch_one(
            db,
            f"SELECT * FROM {T_SOPS} WHERE sop_code = $1 AND version = $2 FOR UPDATE",
            sop_code, version,
        )
        if doc is None:
            raise NotFoundError(f"找不到 SOP：{sop_code} v{version}")
        if doc["status"] != STATUS_DRAFT:
            raise StateError(f"SOP {sop_code} v{version} 狀態為 {doc['status']}，僅草稿可發行")
        if not (doc["steps"] or []):
            raise ValidationError("SOP 內容尚無任何步驟，不可發行")

        now = utcnow()
        effective = payload.get("effective_from") or now
        result = await fetch_one(
            db,
            f"""
            UPDATE {T_SOPS}
            SET status = $1, effective_from = $2, released_by = $3, released_at = $4,
                updated_at = $4, updated_by = $3
            WHERE sop_code = $5 AND version = $6
            RETURNING *
            """,
            STATUS_RELEASED, effective, actor, now, sop_code, version,
        )
        obsoleted = await fetch_all(
            db,
            f"""
            UPDATE {T_SOPS} SET status = $1, updated_at = $2, updated_by = $3
            WHERE sop_code = $4 AND version < $5 AND status = $6
            RETURNING version
            """,
            STATUS_OBSOLETE, now, actor, sop_code, version, STATUS_RELEASED,
        )
        await audit_service.record_change(
            db, actor, "RELEASE", T_SOPS, f"{sop_code} v{version}",
            {"effective_from": effective, "remark": payload.get("remark", "")},
        )
        return {**result, "obsoleted_versions": [int(o["version"]) for o in obsoleted]}


async def obsolete_sop(db, sop_code: str, version: int, actor: str) -> dict:
    async with db.transaction():
        doc = await get_sop(db, sop_code, version)
        if doc["status"] == STATUS_OBSOLETE:
            raise StateError(f"SOP {sop_code} v{version} 已作廢")
        result = await fetch_one(
            db,
            f"""
            UPDATE {T_SOPS} SET status = $1, updated_at = $2, updated_by = $3
            WHERE sop_code = $4 AND version = $5 RETURNING *
            """,
            STATUS_OBSOLETE, utcnow(), actor, sop_code, version,
        )
        await audit_service.record_change(db, actor, "OBSOLETE", T_SOPS, f"{sop_code} v{version}", None)
        return result


async def list_sops(
    db,
    op_code: str | None = None,
    device_id: str | None = None,
    status: str | None = None,
    latest_only: bool = False,
    limit: int = 200,
) -> list[dict]:
    where = """
        WHERE ($1::text IS NULL OR op_code = $1)
          AND ($2::text IS NULL OR device_id = $2)
          AND ($3::text IS NULL OR status = $3)
    """
    distinct = "DISTINCT ON (sop_code) " if latest_only else ""
    order = "sop_code, version DESC" if latest_only else "sop_code, version DESC"
    return await fetch_all(
        db,
        f"""
        SELECT {distinct}sop_code, version, title, op_code, device_id, summary, status,
               require_ack, effective_from, jsonb_array_length(steps) AS step_count,
               created_at, created_by, released_at, released_by
        FROM {T_SOPS} {where} ORDER BY {order} LIMIT $4
        """,
        op_code, device_id, status, limit,
    )


# ── 適用版本 ────────────────────────────────────────────────
async def applicable_sop(
    db, op_code: str, device_id: str = "", at: datetime | None = None
) -> dict | None:
    """取得某站別／料號目前生效的 SOP：料號專用版優先於通用版。"""
    return await fetch_one(
        db,
        f"""
        SELECT * FROM {T_SOPS}
        WHERE op_code = $1 AND status = $2
          AND (effective_from IS NULL OR effective_from <= $3)
          AND (device_id = $4 OR device_id = '')
        ORDER BY (device_id = $4) DESC, version DESC
        LIMIT 1
        """,
        op_code, STATUS_RELEASED, at or utcnow(), device_id or "",
    )


async def station_sop(db, op_code: str, device_id: str = "", username: str | None = None) -> dict:
    """現場終端機開站時呼叫：拿到該做什麼、以及自己簽認了沒。"""
    doc = await applicable_sop(db, op_code, device_id)
    if doc is None:
        return {
            "op_code": op_code, "device_id": device_id, "sop": None,
            "acknowledged": True, "message": f"站別 {op_code} 目前沒有生效中的 SOP",
        }
    acknowledged = True
    acknowledged_at = None
    if username and doc["require_ack"]:
        ack = await fetch_one(
            db,
            f"SELECT * FROM {T_SOP_ACKS} WHERE sop_code = $1 AND version = $2 AND username = $3",
            doc["sop_code"], doc["version"], username,
        )
        acknowledged = ack is not None
        acknowledged_at = ack["acknowledged_at"] if ack else None
    return {
        "op_code": op_code,
        "device_id": device_id,
        "sop": doc,
        "acknowledged": acknowledged,
        "acknowledged_at": acknowledged_at,
    }


# ── 簽認 ────────────────────────────────────────────────────
async def acknowledge(db, payload: dict, actor: str) -> dict:
    sop_code = payload["sop_code"]
    version = payload.get("version")
    doc = await get_sop(db, sop_code, version)
    if doc["status"] != STATUS_RELEASED:
        raise StateError(f"SOP {sop_code} v{doc['version']} 狀態為 {doc['status']}，僅生效版本可簽認")

    inserted = await fetch_one(
        db,
        f"""
        INSERT INTO {T_SOP_ACKS} (sop_code, version, username, acknowledged_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (sop_code, version, username) DO NOTHING
        RETURNING *
        """,
        sop_code, doc["version"], actor, utcnow(),
    )
    if inserted is None:
        # 重複簽認要回原本那一次的時間，稽核看的是「第一次確認是什麼時候」
        inserted = await fetch_one(
            db,
            f"SELECT * FROM {T_SOP_ACKS} WHERE sop_code = $1 AND version = $2 AND username = $3",
            sop_code, doc["version"], actor,
        )
        already = True
    else:
        already = False
    return {
        "sop_code": sop_code,
        "version": doc["version"],
        "title": doc["title"],
        "username": actor,
        "acknowledged_at": inserted["acknowledged_at"],
        "already_acknowledged": already,
    }


async def acknowledgements(db, sop_code: str, version: int | None = None) -> list[dict]:
    doc = await get_sop(db, sop_code, version)
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_SOP_ACKS} WHERE sop_code = $1 AND version = $2
        ORDER BY acknowledged_at
        """,
        sop_code, doc["version"],
    )


async def pending_for_user(db, username: str) -> list[dict]:
    """這位作業員還沒簽認、但已經生效的 SOP —— 上工前的待辦清單。"""
    return await fetch_all(
        db,
        f"""
        SELECT s.sop_code, s.version, s.title, s.op_code, s.device_id, s.effective_from
        FROM {T_SOPS} s
        WHERE s.status = $1 AND s.require_ack = TRUE
          AND (s.effective_from IS NULL OR s.effective_from <= $2)
          AND NOT EXISTS (
              SELECT 1 FROM {T_SOP_ACKS} a
              WHERE a.sop_code = s.sop_code AND a.version = s.version AND a.username = $3
          )
        ORDER BY s.effective_from NULLS FIRST, s.sop_code
        """,
        STATUS_RELEASED, utcnow(), username,
    )


async def ensure_acknowledged(db, op_code: str, device_id: str, username: str) -> dict | None:
    """進站前的把關：站別要求簽認時，未簽認就不放行。"""
    doc = await applicable_sop(db, op_code, device_id)
    if doc is None:
        raise StateError(f"站別 {op_code} 尚未發行 SOP，無法進站（該站已設定必須確認 SOP）")
    if not doc["require_ack"]:
        return doc
    ack = await fetch_one(
        db,
        f"SELECT 1 AS ok FROM {T_SOP_ACKS} WHERE sop_code = $1 AND version = $2 AND username = $3",
        doc["sop_code"], doc["version"], username,
    )
    if ack is None:
        raise PermissionError_(
            f"作業員 {username} 尚未確認 {op_code} 站的作業指導書 "
            f"{doc['sop_code']} v{doc['version']}（{doc['title']}），請先閱讀並簽認"
        )
    return doc


async def compliance(db, op_code: str | None = None) -> list[dict]:
    """簽認率報表：稽核時要交出來的那張表。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT s.sop_code, s.version, s.title, s.op_code, s.device_id, s.effective_from,
               count(a.id) AS acknowledged
        FROM {T_SOPS} s
        LEFT JOIN {T_SOP_ACKS} a ON a.sop_code = s.sop_code AND a.version = s.version
        WHERE s.status = $1 AND s.require_ack = TRUE
          AND ($2::text IS NULL OR s.op_code = $2)
        GROUP BY s.sop_code, s.version, s.title, s.op_code, s.device_id, s.effective_from
        ORDER BY s.op_code, s.sop_code
        """,
        STATUS_RELEASED, op_code,
    )
    operators = int(
        await db.fetchval(
            f"SELECT count(*) FROM {T_USERS} "
            f"WHERE active AND ('operator' = ANY(roles) OR 'engineer' = ANY(roles))"
        )
        or 0
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        acked = int(row["acknowledged"])
        out.append(
            {
                **row,
                "acknowledged": acked,
                "target_headcount": operators,
                "rate": round(acked / operators, 4) if operators else None,
            }
        )
    return out
