"""認證與 RBAC 測試。"""

import pytest

from app.security import create_access_token, decode_access_token, hash_password, verify_password


def test_password_hash_roundtrip():
    hashed = hash_password("secret123")
    assert hashed.startswith("pbkdf2_sha256$")
    assert verify_password("secret123", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("secret123", "亂七八糟的字串")


def test_token_roundtrip():
    token = create_access_token("op001", ["operator"])
    payload = decode_access_token(token)
    assert payload["sub"] == "op001"
    assert payload["roles"] == ["operator"]


def test_tampered_token_rejected():
    from fastapi import HTTPException

    token = create_access_token("op001", ["operator"])
    header, body, _sig = token.split(".")
    forged = f"{header}.{body}.{'A' * 43}"
    with pytest.raises(HTTPException) as exc:
        decode_access_token(forged)
    assert exc.value.status_code == 401


def test_expired_token_rejected():
    from fastapi import HTTPException

    token = create_access_token("op001", ["operator"], expires_minutes=-1)
    with pytest.raises(HTTPException):
        decode_access_token(token)


def test_login_ok(client):
    res = client.post("/api/auth/login", json={"username": "admin", "password": "admin1234"})
    assert res.status_code == 200
    assert res.json()["user"]["roles"] == ["admin"]


def test_login_wrong_password(client):
    res = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert res.status_code == 401


def test_endpoint_requires_auth(client):
    assert client.get("/api/lots").status_code == 401


def test_viewer_cannot_create_work_order(client, token):
    res = client.post(
        "/api/work-orders",
        headers=token("viewer01"),
        json={"device_id": "TEST-QFN48", "plan_qty": 10, "unit_type": "WAFER",
              "due_date": "2030-01-01T00:00:00Z"},
    )
    assert res.status_code == 403
    assert "權限不足" in res.json()["detail"]


def test_planner_can_create_work_order(client, token):
    res = client.post(
        "/api/work-orders",
        headers=token("planner01"),
        json={"device_id": "TEST-QFN48", "plan_qty": 10, "unit_type": "WAFER",
              "due_date": "2030-01-01T00:00:00Z"},
    )
    assert res.status_code == 201
    assert res.json()["wo_no"].startswith("WO")


def test_admin_bypasses_role_check(client, token):
    """admin 不必掛生管角色也能操作。"""
    res = client.post(
        "/api/work-orders",
        headers=token("admin"),
        json={"device_id": "TEST-QFN48", "plan_qty": 10, "unit_type": "WAFER",
              "due_date": "2030-01-01T00:00:00Z"},
    )
    assert res.status_code == 201
