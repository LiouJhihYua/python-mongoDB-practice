"""工程基礎建設測試：migration、登入節流、Token 續期、稽核歸檔。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import database
from app.config import settings
from app.errors import NotFoundError
from app.models.base import utcnow
from app.services import audit_service, user_service

pytestmark = pytest.mark.asyncio


# ── Migration ───────────────────────────────────────────────
def test_migration_files_are_ordered():
    names = [p.stem for p in database.migration_files()]
    assert names == sorted(names)
    assert names[0] == "0001_baseline"
    assert len(names) >= 5


async def test_all_migrations_recorded(pool):
    async with pool.acquire() as conn:
        applied = await database.applied_migrations(conn)
    assert {p.stem for p in database.migration_files()} <= applied


async def test_apply_schema_is_idempotent(pool):
    """再跑一次不應該有任何待執行項目 —— 服務每次啟動都會呼叫它。"""
    async with pool.acquire() as conn:
        assert await database.apply_schema(conn) == []


async def test_migration_status_reports_all_applied(pool):
    async with pool.acquire() as conn:
        rows = await database.migration_status(conn)
    assert rows and all(r["applied"] for r in rows)
    assert all(r["applied_at"] is not None for r in rows)


# ── 登入失敗節流 ────────────────────────────────────────────
async def test_failed_login_counts_up(factory):
    db = factory
    for _ in range(3):
        assert await user_service.authenticate(db, "op001", "wrong") is None
    row = await db.fetchrow("SELECT failed_logins, locked_until FROM users WHERE username = 'op001'")
    assert row["failed_logins"] == 3 and row["locked_until"] is None


async def test_account_locks_after_max_attempts(factory):
    db = factory
    for _ in range(settings.login_max_attempts):
        await user_service.authenticate(db, "op001", "wrong")

    with pytest.raises(user_service.AccountLocked, match="鎖定"):
        await user_service.authenticate(db, "op001", "op0011234")  # 正確密碼也不放行


async def test_successful_login_resets_counter(factory):
    db = factory
    await user_service.authenticate(db, "op001", "wrong")
    user = await user_service.authenticate(db, "op001", "op0011234")
    assert user is not None

    row = await db.fetchrow("SELECT failed_logins, last_login_at FROM users WHERE username = 'op001'")
    assert row["failed_logins"] == 0 and row["last_login_at"] is not None


async def test_unlock_by_admin(factory):
    db = factory
    for _ in range(settings.login_max_attempts):
        await user_service.authenticate(db, "op001", "wrong")

    assert [u["username"] for u in await user_service.locked_accounts(db)] == ["op001"]
    await user_service.unlock(db, "op001", "admin")
    assert await user_service.locked_accounts(db) == []
    assert await user_service.authenticate(db, "op001", "op0011234") is not None


async def test_unlock_unknown_user(factory):
    with pytest.raises(NotFoundError):
        await user_service.unlock(factory, "nobody", "admin")


async def test_lock_expires(factory):
    db = factory
    for _ in range(settings.login_max_attempts):
        await user_service.authenticate(db, "op001", "wrong")
    # 把鎖定時間調到過去，模擬時間經過
    await db.execute(
        "UPDATE users SET locked_until = $1 WHERE username = 'op001'", utcnow() - timedelta(minutes=1)
    )
    assert await user_service.authenticate(db, "op001", "op0011234") is not None


async def test_unknown_username_does_not_error(factory):
    """帳號不存在時不能有特殊行為，否則回應差異會洩漏帳號是否存在。"""
    assert await user_service.authenticate(factory, "no-such-user", "x") is None


async def test_login_api_returns_lockout_status(client, token, factory):
    for _ in range(settings.login_max_attempts):
        res = await client.post("/api/auth/login", json={"username": "op001", "password": "bad"})
        assert res.status_code == 401

    locked = await client.post("/api/auth/login", json={"username": "op001", "password": "op0011234"})
    assert locked.status_code == 429
    assert "鎖定" in locked.json()["message"]

    admin = await token("admin")
    listed = await client.get("/api/auth/locked", headers=admin)
    assert [u["username"] for u in listed.json()] == ["op001"]

    unlocked = await client.post("/api/auth/users/op001/unlock", headers=admin)
    assert unlocked.status_code == 200

    ok = await client.post("/api/auth/login", json={"username": "op001", "password": "op0011234"})
    assert ok.status_code == 200


async def test_unlock_requires_admin(client, token, factory):
    qc = await token("qc01")
    res = await client.post("/api/auth/users/op001/unlock", headers=qc)
    assert res.status_code == 403


# ── Token 續期 ──────────────────────────────────────────────
async def test_refresh_issues_new_token(client, token, factory):
    headers = await token("op001")
    res = await client.post("/api/auth/refresh", headers=headers)
    assert res.status_code == 200
    assert res.json()["user"]["username"] == "op001"
    assert res.json()["access_token"]

    new_headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    me = await client.get("/api/auth/me", headers=new_headers)
    assert me.json()["username"] == "op001"


async def test_refresh_requires_valid_token(client, factory):
    res = await client.post("/api/auth/refresh", headers={"Authorization": "Bearer nope"})
    assert res.status_code == 401


# ── 稽核歸檔與清除 ──────────────────────────────────────────
async def _seed_audit(db, days_ago: int, count: int = 3) -> None:
    when = utcnow() - timedelta(days=days_ago)
    for i in range(count):
        await db.execute(
            "INSERT INTO audit_logs (kind, actor, method, path, status_code, success, timestamp) "
            "VALUES ($1, $2, $3, $4, $5, TRUE, $6)",
            "API", "op001", "POST", f"/api/test/{days_ago}-{i}", 200, when,
        )


async def test_retention_status(factory):
    db = factory
    await _seed_audit(db, days_ago=settings.audit_retention_days + 10, count=2)
    await _seed_audit(db, days_ago=1, count=3)

    status = await audit_service.retention_status(db)
    assert status["total"] >= 5
    assert status["expired"] == 2
    assert status["retention_days"] == settings.audit_retention_days


async def test_purge_defaults_to_dry_run(factory):
    db = factory
    await _seed_audit(db, days_ago=800, count=4)
    before = utcnow() - timedelta(days=700)

    dry = await audit_service.purge(db, before)
    assert dry["dry_run"] is True and dry["matched"] == 4 and dry["deleted"] == 0
    assert await db.fetchval("SELECT count(*) FROM audit_logs WHERE timestamp < $1", before) == 4


async def test_purge_deletes_when_confirmed(factory):
    db = factory
    await _seed_audit(db, days_ago=800, count=4)
    await _seed_audit(db, days_ago=1, count=2)
    before = utcnow() - timedelta(days=700)

    result = await audit_service.purge(db, before, dry_run=False)
    assert result["deleted"] == 4
    assert await db.fetchval("SELECT count(*) FROM audit_logs WHERE timestamp < $1", before) == 0
    assert await db.fetchval("SELECT count(*) FROM audit_logs") >= 2


async def test_export_jsonl(factory):
    db = factory
    await _seed_audit(db, days_ago=800, count=3)
    content = await audit_service.export_jsonl(db, utcnow() - timedelta(days=700))
    lines = content.splitlines()
    assert len(lines) == 3

    import json

    first = json.loads(lines[0])
    assert first["actor"] == "op001" and isinstance(first["timestamp"], str)


async def test_audit_api_retention_and_purge(client, token, factory):
    db = factory
    await _seed_audit(db, days_ago=800, count=3)
    admin = await token("admin")

    status = await client.get("/api/audit/retention", headers=admin)
    assert status.json()["expired"] == 3

    export = await client.get("/api/audit/export.jsonl?days=700", headers=admin)
    assert export.status_code == 200 and len(export.text.splitlines()) == 3

    dry = await client.delete("/api/audit?days=700", headers=admin)
    assert dry.json()["dry_run"] is True and dry.json()["deleted"] == 0

    done = await client.delete("/api/audit?days=700&confirm=true", headers=admin)
    assert done.json()["deleted"] == 3


async def test_audit_purge_requires_admin(client, token, factory):
    qc = await token("qc01")
    res = await client.delete("/api/audit?days=700", headers=qc)
    assert res.status_code == 403
