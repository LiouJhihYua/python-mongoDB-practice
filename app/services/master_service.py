"""主檔服務：含跨主檔的參照完整性檢查。"""

from __future__ import annotations

from app.database import (
    T_CUSTOMERS,
    T_DEFECT_CODES,
    T_DEVICES,
    T_EQUIPMENTS,
    T_MATERIALS,
    T_OPERATIONS,
    T_PACKAGES,
    T_ROUTES,
    T_WAFERS,
    fetch_all,
    fetch_one,
)
from app.errors import DuplicateError, NotFoundError, ValidationError
from app.models.base import plain_values, utcnow
from app.models.enums import EquipmentState
from app.services import audit_service
from app.services.crud import Table

customers = Table(T_CUSTOMERS, "code", "客戶")
packages = Table(T_PACKAGES, "package_code", "封裝型式")
devices = Table(T_DEVICES, "device_id", "產品料號")
operations = Table(T_OPERATIONS, "op_code", "站別")
equipments = Table(T_EQUIPMENTS, "eq_id", "設備")
defect_codes = Table(T_DEFECT_CODES, "code", "不良代碼")
materials = Table(T_MATERIALS, "material_id", "材料")
wafers = Table(T_WAFERS, "wafer_id", "晶圓")


# ── 參照檢查 ────────────────────────────────────────────────
async def _assert_exists(db, table: str, field: str, values: list[str], label: str) -> None:
    wanted = [v for v in dict.fromkeys(values) if v]
    if not wanted:
        return
    rows = await db.fetch(f"SELECT {field} AS value FROM {table} WHERE {field} = ANY($1::text[])", wanted)
    missing = set(wanted) - {r["value"] for r in rows}
    if missing:
        raise ValidationError(f"{label}不存在：{', '.join(sorted(missing))}")


async def create_device(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, T_CUSTOMERS, "code", [payload["customer_code"]], "客戶")
    await _assert_exists(db, T_PACKAGES, "package_code", [payload["package_code"]], "封裝型式")
    await get_active_route(db, payload["route_code"])  # 確認流程存在且啟用
    return await devices.create(db, payload, actor)


# ── 製程流程（多版本）──────────────────────────────────────
async def create_route(db, payload: dict, actor: str) -> dict:
    steps = payload["steps"]
    await _assert_exists(db, T_OPERATIONS, "op_code", [s["op_code"] for s in steps], "站別")

    data = plain_values(payload)
    existing = await fetch_one(
        db,
        f"SELECT route_code FROM {T_ROUTES} WHERE route_code = $1 AND version = $2",
        data["route_code"], data["version"],
    )
    if existing:
        raise DuplicateError(f"流程 {data['route_code']} v{data['version']} 已存在")

    row = await fetch_one(
        db,
        f"""
        INSERT INTO {T_ROUTES}
            (route_code, version, description, package_family, steps, active, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
        RETURNING *
        """,
        data["route_code"], data["version"], data.get("description", ""),
        data.get("package_family", ""), data["steps"], data.get("active", True), actor,
    )
    await audit_service.record_change(
        db, actor, "CREATE", T_ROUTES, f"{data['route_code']}#{data['version']}", payload
    )
    return row


async def get_active_route(db, route_code: str, version: int | None = None) -> dict:
    """取得啟用中的流程；未指定版本時取版號最大者。"""
    row = await fetch_one(
        db,
        f"""
        SELECT * FROM {T_ROUTES}
        WHERE route_code = $1 AND active AND ($2::int IS NULL OR version = $2)
        ORDER BY version DESC
        LIMIT 1
        """,
        route_code, version,
    )
    if row is None:
        raise NotFoundError(f"找不到啟用中的流程：{route_code}")
    return row


async def list_routes(db, skip: int = 0, limit: int = 100, active: bool | None = None) -> dict:
    total = await db.fetchval(
        f"SELECT count(*) FROM {T_ROUTES} WHERE ($1::boolean IS NULL OR active = $1)", active
    )
    items = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_ROUTES}
        WHERE ($1::boolean IS NULL OR active = $1)
        ORDER BY route_code ASC, version DESC
        OFFSET $2 LIMIT $3
        """,
        active, skip, limit,
    )
    return {"items": items, "total": total, "skip": skip, "limit": limit}


async def create_equipment(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, T_OPERATIONS, "op_code", payload.get("op_codes", []), "站別")
    doc = dict(payload)
    doc.update(
        current_state=EquipmentState.NON_SCHEDULED.value,
        state_since=utcnow(),
        state_reason="初始建檔",
        state_remark="初始建檔",
        current_lot_id=None,
    )
    return await equipments.create(db, doc, actor)


async def create_defect_code(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, T_OPERATIONS, "op_code", payload.get("op_codes", []), "站別")
    return await defect_codes.create(db, payload, actor)


async def create_wafer(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, T_DEVICES, "device_id", [payload["device_id"]], "產品料號")
    doc = dict(payload)
    gross, good = int(doc.get("gross_die") or 0), int(doc.get("cp_good_die") or 0)
    if good > gross:
        raise ValidationError(f"CP 良品數 {good} 不可大於總晶粒數 {gross}")
    # 服務層也會被腳本直接呼叫，CP 良率的推算不能只放在 API 的輸入模型
    if doc.get("cp_yield") is None:
        doc["cp_yield"] = round(good / gross, 4) if gross else None
    if doc.get("received_at") is None:
        doc["received_at"] = utcnow()
    doc.update(assembly_lot_id=None, consumed=False)
    return await wafers.create(db, doc, actor)


# ── 流程走訪工具 ────────────────────────────────────────────
def get_route_step(route: dict, seq: int) -> dict | None:
    for step in route.get("steps", []):
        if step["seq"] == seq:
            return step
    return None


def first_step(route: dict) -> dict:
    steps = sorted(route.get("steps", []), key=lambda s: s["seq"])
    if not steps:
        raise ValidationError(f"流程 {route.get('route_code')} 沒有任何站別")
    return steps[0]


def next_step(route: dict, current_seq: int) -> dict | None:
    """回傳站序大於 current_seq 的第一個站；已是最後一站則回 None。"""
    for step in sorted(route.get("steps", []), key=lambda s: s["seq"]):
        if step["seq"] > current_seq:
            return step
    return None
