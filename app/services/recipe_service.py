"""配方管理（Recipe / PPID）。

封測廠最常見的品質事故之一是「機台載錯配方」：料號換了、配方沒換，
整批做完才發現。這裡把兩件事分開存放再交叉比對：

* **核可清單** —— 哪些配方經過發行、可用於哪個站別／料號／機型（``recipes``）
* **機台現況** —— 每台機器現在載的是哪一支配方（``equipment_recipes``），
  由 SECS 事件、S7 查詢或人工回報更新

進站時比對兩者，不符就擋下來，並把比對結果寫進 ``recipe_checks`` ——
客戶稽核時要拿得出「每一批都比對過」的證據，而不是靠作業員記得。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import (
    T_EQUIPMENT_RECIPES,
    T_EQUIPMENTS,
    T_OPERATIONS,
    T_RECIPE_CHECKS,
    T_RECIPES,
    fetch_all,
    fetch_one,
)
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import json_safe, utcnow
from app.services import audit_service

STATUS_DRAFT = "DRAFT"
STATUS_RELEASED = "RELEASED"
STATUS_OBSOLETE = "OBSOLETE"

SOURCE_MANUAL = "MANUAL"
SOURCE_SECS = "SECS"
SOURCE_DOWNLOAD = "DOWNLOAD"

EDITABLE_COLUMNS = ("name", "device_id", "eq_model", "parameters", "checksum", "remark")


# ── 版本管理 ────────────────────────────────────────────────
async def _next_version(db, ppid: str) -> int:
    return int(
        await db.fetchval(f"SELECT coalesce(max(version), 0) FROM {T_RECIPES} WHERE ppid = $1", ppid)
        or 0
    ) + 1


async def create_recipe(db, payload: dict, actor: str) -> dict:
    async with db.transaction():
        op = await fetch_one(
            db, f"SELECT op_code FROM {T_OPERATIONS} WHERE op_code = $1", payload["op_code"]
        )
        if op is None:
            raise ValidationError(f"站別不存在：{payload['op_code']}")

        draft = await fetch_one(
            db,
            f"SELECT version FROM {T_RECIPES} WHERE ppid = $1 AND status = $2 ORDER BY version DESC",
            payload["ppid"], STATUS_DRAFT,
        )
        if draft is not None:
            raise StateError(f"配方 {payload['ppid']} 已有草稿版本 v{draft['version']}，請先發行或作廢")

        version = await _next_version(db, payload["ppid"])
        now = utcnow()
        doc = await fetch_one(
            db,
            f"""
            INSERT INTO {T_RECIPES}
                (ppid, version, name, op_code, device_id, eq_model, parameters, checksum,
                 status, remark, created_at, created_by, updated_at, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $11, $12)
            RETURNING *
            """,
            payload["ppid"], version, payload["name"], payload["op_code"],
            payload.get("device_id", ""), payload.get("eq_model", ""),
            json_safe(payload.get("parameters") or {}), payload.get("checksum", ""),
            STATUS_DRAFT, payload.get("remark", ""), now, actor,
        )
        await audit_service.record_change(
            db, actor, "CREATE", T_RECIPES, f"{payload['ppid']} v{version}", payload
        )
        return doc


async def revise_recipe(db, ppid: str, actor: str) -> dict:
    """以現行版本複製出新草稿 —— 調參數時不用重打一遍。"""
    async with db.transaction():
        latest = await fetch_one(
            db, f"SELECT * FROM {T_RECIPES} WHERE ppid = $1 ORDER BY version DESC", ppid
        )
        if latest is None:
            raise NotFoundError(f"找不到配方：{ppid}")
        if latest["status"] == STATUS_DRAFT:
            raise StateError(f"配方 {ppid} 已有草稿版本 v{latest['version']}，請直接編輯")

        version = int(latest["version"]) + 1
        now = utcnow()
        doc = await fetch_one(
            db,
            f"""
            INSERT INTO {T_RECIPES}
                (ppid, version, name, op_code, device_id, eq_model, parameters, checksum,
                 status, remark, created_at, created_by, updated_at, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $11, $12)
            RETURNING *
            """,
            ppid, version, latest["name"], latest["op_code"], latest["device_id"],
            latest["eq_model"], latest["parameters"], latest["checksum"],
            STATUS_DRAFT, latest["remark"], now, actor,
        )
        await audit_service.record_change(
            db, actor, "REVISE", T_RECIPES, f"{ppid} v{version}", {"from_version": latest["version"]}
        )
        return doc


async def get_recipe(db, ppid: str, version: int | None = None) -> dict:
    if version is not None:
        doc = await fetch_one(
            db, f"SELECT * FROM {T_RECIPES} WHERE ppid = $1 AND version = $2", ppid, version
        )
    else:
        doc = await fetch_one(
            db, f"SELECT * FROM {T_RECIPES} WHERE ppid = $1 ORDER BY version DESC", ppid
        )
    if doc is None:
        raise NotFoundError(f"找不到配方：{ppid}" + (f" v{version}" if version else ""))
    return doc


async def update_recipe(db, ppid: str, version: int, patch: dict, actor: str) -> dict:
    async with db.transaction():
        doc = await fetch_one(
            db, f"SELECT * FROM {T_RECIPES} WHERE ppid = $1 AND version = $2 FOR UPDATE", ppid, version
        )
        if doc is None:
            raise NotFoundError(f"找不到配方：{ppid} v{version}")
        if doc["status"] != STATUS_DRAFT:
            raise StateError(f"配方 {ppid} v{version} 狀態為 {doc['status']}，僅草稿可修改")

        fields = {
            k: (json_safe(v) if k == "parameters" else v)
            for k, v in patch.items() if k in EDITABLE_COLUMNS and v is not None
        }
        if not fields:
            raise ValidationError("沒有需要更新的欄位")
        fields["updated_at"] = utcnow()
        fields["updated_by"] = actor
        assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(fields, start=1))
        result = await fetch_one(
            db,
            f"UPDATE {T_RECIPES} SET {assignments} "
            f"WHERE ppid = ${len(fields) + 1} AND version = ${len(fields) + 2} RETURNING *",
            *fields.values(), ppid, version,
        )
        await audit_service.record_change(db, actor, "UPDATE", T_RECIPES, f"{ppid} v{version}", patch)
        return result


async def release_recipe(db, ppid: str, version: int, payload: dict, actor: str) -> dict:
    """發行配方；同一 PPID 的舊版自動作廢。"""
    async with db.transaction():
        doc = await fetch_one(
            db, f"SELECT * FROM {T_RECIPES} WHERE ppid = $1 AND version = $2 FOR UPDATE", ppid, version
        )
        if doc is None:
            raise NotFoundError(f"找不到配方：{ppid} v{version}")
        if doc["status"] != STATUS_DRAFT:
            raise StateError(f"配方 {ppid} v{version} 狀態為 {doc['status']}，僅草稿可發行")
        if not (doc["parameters"] or {}):
            raise ValidationError("配方尚無任何參數，不可發行")

        now = utcnow()
        effective = payload.get("effective_from") or now
        result = await fetch_one(
            db,
            f"""
            UPDATE {T_RECIPES}
            SET status = $1, effective_from = $2, released_by = $3, released_at = $4,
                updated_at = $4, updated_by = $3
            WHERE ppid = $5 AND version = $6 RETURNING *
            """,
            STATUS_RELEASED, effective, actor, now, ppid, version,
        )
        obsoleted = await fetch_all(
            db,
            f"""
            UPDATE {T_RECIPES} SET status = $1, updated_at = $2, updated_by = $3
            WHERE ppid = $4 AND version < $5 AND status = $6 RETURNING version
            """,
            STATUS_OBSOLETE, now, actor, ppid, version, STATUS_RELEASED,
        )
        await audit_service.record_change(
            db, actor, "RELEASE", T_RECIPES, f"{ppid} v{version}",
            {"effective_from": effective, "remark": payload.get("remark", "")},
        )
        return {**result, "obsoleted_versions": [int(o["version"]) for o in obsoleted]}


async def obsolete_recipe(db, ppid: str, version: int, actor: str) -> dict:
    async with db.transaction():
        doc = await get_recipe(db, ppid, version)
        if doc["status"] == STATUS_OBSOLETE:
            raise StateError(f"配方 {ppid} v{version} 已作廢")
        result = await fetch_one(
            db,
            f"""
            UPDATE {T_RECIPES} SET status = $1, updated_at = $2, updated_by = $3
            WHERE ppid = $4 AND version = $5 RETURNING *
            """,
            STATUS_OBSOLETE, utcnow(), actor, ppid, version,
        )
        await audit_service.record_change(db, actor, "OBSOLETE", T_RECIPES, f"{ppid} v{version}", None)
        return result


async def list_recipes(
    db, op_code: str | None = None, device_id: str | None = None,
    status: str | None = None, limit: int = 200,
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT ppid, version, name, op_code, device_id, eq_model, status, checksum,
               effective_from, created_at, created_by, released_at, released_by,
               (SELECT count(*) FROM jsonb_object_keys(parameters)) AS param_count
        FROM {T_RECIPES}
        WHERE ($1::text IS NULL OR op_code = $1)
          AND ($2::text IS NULL OR device_id = $2)
          AND ($3::text IS NULL OR status = $3)
        ORDER BY op_code, ppid, version DESC
        LIMIT $4
        """,
        op_code, device_id, status, limit,
    )


# ── 核可清單與比對 ──────────────────────────────────────────
async def approved_recipes(
    db, op_code: str, device_id: str = "", eq_model: str = "", at: datetime | None = None
) -> list[dict]:
    """某站別／料號／機型目前核可可用的配方；料號與機型留空表示通用。"""
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_RECIPES}
        WHERE op_code = $1 AND status = $2
          AND (effective_from IS NULL OR effective_from <= $3)
          AND (device_id = $4 OR device_id = '')
          AND (eq_model = $5 OR eq_model = '')
        ORDER BY (device_id = $4) DESC, (eq_model = $5) DESC, ppid, version DESC
        """,
        op_code, STATUS_RELEASED, at or utcnow(), device_id or "", eq_model or "",
    )


async def loaded_recipe(db, eq_id: str) -> dict | None:
    return await fetch_one(db, f"SELECT * FROM {T_EQUIPMENT_RECIPES} WHERE eq_id = $1", eq_id)


async def set_loaded(db, eq_id: str, payload: dict, actor: str, source: str = SOURCE_MANUAL) -> dict:
    """記錄機台目前載入的配方。

    來源可能是作業員回報、SECS 事件，或 MES 主動下發後的確認 ——
    三者都寫進同一張表，比對時才有單一事實來源。
    """
    eq = await fetch_one(db, f"SELECT eq_id FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id)
    if eq is None:
        raise ValidationError(f"設備不存在：{eq_id}")

    now = utcnow()
    row = await fetch_one(
        db,
        f"""
        INSERT INTO {T_EQUIPMENT_RECIPES}
            (eq_id, ppid, version, checksum, source, loaded_at, loaded_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (eq_id) DO UPDATE SET
            ppid = EXCLUDED.ppid, version = EXCLUDED.version, checksum = EXCLUDED.checksum,
            source = EXCLUDED.source, loaded_at = EXCLUDED.loaded_at, loaded_by = EXCLUDED.loaded_by
        RETURNING *
        """,
        eq_id, str(payload.get("ppid", "")), payload.get("version"),
        payload.get("checksum", ""), source, now, actor,
    )
    await audit_service.record_change(
        db, actor, "LOAD_RECIPE", T_EQUIPMENT_RECIPES, eq_id,
        {"ppid": payload.get("ppid"), "source": source},
    )
    return row


async def _record_check(
    db, lot: dict, eq_id: str, op_code: str, expected: list[str],
    loaded: str, passed: bool, reason: str, actor: str,
) -> dict:
    return await fetch_one(
        db,
        f"""
        INSERT INTO {T_RECIPE_CHECKS}
            (lot_id, eq_id, op_code, device_id, expected_ppids, loaded_ppid,
             passed, reason, operator, timestamp)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        lot.get("lot_id", ""), eq_id, op_code, lot.get("device_id", ""),
        expected, loaded, passed, reason, actor, utcnow(),
    )


async def verify_for_track_in(db, lot: dict, eq_id: str, op_code: str, actor: str) -> dict:
    """進站前的配方比對。

    **刻意不拋例外**：比對紀錄（含失敗的）正是稽核要的證據，
    若在呼叫端的交易裡直接 raise，紀錄會跟著被回滾掉。
    因此這裡一律回傳結果，由呼叫端在交易提交後再決定要不要擋下進站。
    回傳的 ``passed`` 為 False 時，``message`` 就是要給現場看的原因。
    """
    device_id = lot.get("device_id", "")
    eq = await fetch_one(
        db, f"SELECT eq_id, model FROM {T_EQUIPMENTS} WHERE eq_id = $1", eq_id
    ) if eq_id else None
    eq_model = (eq or {}).get("model", "") or ""

    approved = await approved_recipes(db, op_code, device_id, eq_model)
    expected = [r["ppid"] for r in approved]
    current = await loaded_recipe(db, eq_id) if eq_id else None
    loaded = (current or {}).get("ppid", "") or ""

    async def fail(reason: str, message: str) -> dict:
        check = await _record_check(db, lot, eq_id, op_code, expected, loaded, False, reason, actor)
        return {"passed": False, "reason": reason, "message": message, "check": check}

    if not approved:
        return await fail(
            f"站別 {op_code} 與料號 {device_id} 尚無已發行的核可配方",
            f"站別 {op_code} 與料號 {device_id} 尚無已發行的核可配方，"
            f"無法進站（該站已設定必須比對配方）",
        )
    if not loaded:
        return await fail(
            f"設備 {eq_id} 未回報目前載入的配方",
            f"設備 {eq_id} 未回報目前載入的配方，請先確認機台配方後再進站",
        )
    if loaded not in expected:
        return await fail(
            f"機台配方 {loaded} 不在核可清單內：{', '.join(expected)}",
            f"設備 {eq_id} 目前載入的配方 {loaded} 未核可用於 {op_code} 站的 {device_id}"
            f"（核可清單：{', '.join(expected)}）",
        )

    matched = next(r for r in approved if r["ppid"] == loaded)
    # 校驗碼是選配：機台有回報才比，避免沒有這項能力的老機台永遠過不了
    declared = (current or {}).get("checksum", "") or ""
    if matched["checksum"] and declared and matched["checksum"] != declared:
        return await fail(
            f"配方 {loaded} 校驗碼不符（核可 {matched['checksum']} / 機台 {declared}）",
            f"配方 {loaded} 校驗碼不符（核可 {matched['checksum']} / 機台 {declared}），"
            f"機台上的配方內容可能已被修改",
        )

    check = await _record_check(
        db, lot, eq_id, op_code, expected, loaded, True,
        f"比對通過：{loaded} v{matched['version']}", actor,
    )
    return {"passed": True, "recipe": matched, "check": check}


async def list_checks(
    db, lot_id: str | None = None, eq_id: str | None = None,
    passed: bool | None = None, limit: int = 200,
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_RECIPE_CHECKS}
        WHERE ($1::text IS NULL OR lot_id = $1)
          AND ($2::text IS NULL OR eq_id = $2)
          AND ($3::boolean IS NULL OR passed = $3)
        ORDER BY timestamp DESC, id DESC LIMIT $4
        """,
        lot_id, eq_id, passed, limit,
    )


async def status_overview(db) -> dict:
    """配方看板：機台載了什麼、有沒有載到未核可的配方。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT e.eq_id, e.name AS eq_name, e.model, e.area, e.current_state, e.op_codes,
               r.ppid, r.version, r.source, r.loaded_at, r.loaded_by
        FROM {T_EQUIPMENTS} e
        LEFT JOIN {T_EQUIPMENT_RECIPES} r ON r.eq_id = e.eq_id
        WHERE e.active
        ORDER BY e.eq_id
        """,
    )
    released = await fetch_all(
        db, f"SELECT ppid, version, op_code, device_id FROM {T_RECIPES} WHERE status = $1",
        STATUS_RELEASED,
    )
    approved_ppids = {r["ppid"] for r in released}

    out: list[dict[str, Any]] = []
    unknown = 0
    for row in rows:
        ppid = row["ppid"] or ""
        recognised = bool(ppid) and ppid in approved_ppids
        if ppid and not recognised:
            unknown += 1
        out.append({**row, "recognised": recognised})
    return {
        "equipments": out,
        "loaded": sum(1 for r in out if r["ppid"]),
        "unloaded": sum(1 for r in out if not r["ppid"]),
        "unknown_recipe": unknown,
        "released_recipes": len(released),
    }
