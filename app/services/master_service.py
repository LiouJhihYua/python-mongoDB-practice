"""主檔服務：含跨主檔的參照完整性檢查。"""

from __future__ import annotations

from app.database import (
    COL_CUSTOMERS,
    COL_DEFECT_CODES,
    COL_DEVICES,
    COL_EQUIPMENTS,
    COL_MATERIALS,
    COL_OPERATIONS,
    COL_PACKAGES,
    COL_ROUTES,
    COL_WAFERS,
)
from app.errors import NotFoundError, ValidationError
from app.models.base import clean, utcnow
from app.models.enums import EquipmentState
from app.services.crud import CRUD

customers = CRUD(COL_CUSTOMERS, "code", "客戶")
packages = CRUD(COL_PACKAGES, "package_code", "封裝型式")
devices = CRUD(COL_DEVICES, "device_id", "產品料號")
operations = CRUD(COL_OPERATIONS, "op_code", "站別")
equipments = CRUD(COL_EQUIPMENTS, "eq_id", "設備")
defect_codes = CRUD(COL_DEFECT_CODES, "code", "不良代碼")
materials = CRUD(COL_MATERIALS, "material_id", "材料")
wafers = CRUD(COL_WAFERS, "wafer_id", "晶圓")


# ── 參照檢查 ────────────────────────────────────────────────
async def _assert_exists(db, collection: str, field: str, values: list[str], label: str) -> None:
    values = [v for v in dict.fromkeys(values) if v]
    if not values:
        return
    found = await db[collection].find({field: {"$in": values}}).to_list(length=len(values))
    missing = set(values) - {d[field] for d in found}
    if missing:
        raise ValidationError(f"{label}不存在：{', '.join(sorted(missing))}")


async def create_device(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, COL_CUSTOMERS, "code", [payload["customer_code"]], "客戶")
    await _assert_exists(db, COL_PACKAGES, "package_code", [payload["package_code"]], "封裝型式")
    await get_active_route(db, payload["route_code"])  # 確認流程存在且啟用
    return await devices.create(db, payload, actor)


async def create_route(db, payload: dict, actor: str) -> dict:
    await _assert_exists(
        db, COL_OPERATIONS, "op_code", [s["op_code"] for s in payload["steps"]], "站別"
    )
    key = {"route_code": payload["route_code"], "version": payload["version"]}
    if await db[COL_ROUTES].find_one(key):
        from app.errors import DuplicateError

        raise DuplicateError(f"流程 {payload['route_code']} v{payload['version']} 已存在")
    doc = dict(payload)
    doc.update(created_at=utcnow(), created_by=actor, updated_at=utcnow(), updated_by=actor)
    await db[COL_ROUTES].insert_one(doc)
    return clean(await db[COL_ROUTES].find_one(key))


async def get_active_route(db, route_code: str, version: int | None = None) -> dict:
    """取得啟用中的流程；未指定版本時取版號最大者。"""
    query: dict = {"route_code": route_code, "active": True}
    if version is not None:
        query["version"] = version
    doc = await db[COL_ROUTES].find_one(query, sort=[("version", -1)])
    if doc is None:
        raise NotFoundError(f"找不到啟用中的流程：{route_code}")
    return doc


async def list_routes(db, skip: int = 0, limit: int = 100, active: bool | None = None) -> dict:
    filt = {} if active is None else {"active": active}
    total = await db[COL_ROUTES].count_documents(filt)
    cursor = db[COL_ROUTES].find(filt).sort([("route_code", 1), ("version", -1)]).skip(skip).limit(limit)
    return {
        "items": [clean(d) for d in await cursor.to_list(length=limit)],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


async def create_equipment(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, COL_OPERATIONS, "op_code", payload.get("op_codes", []), "站別")
    doc = dict(payload)
    doc.update(
        current_state=EquipmentState.NON_SCHEDULED.value,
        state_since=utcnow(),
        state_reason="初始建檔",
        current_lot_id=None,
    )
    return await equipments.create(db, doc, actor)


async def create_defect_code(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, COL_OPERATIONS, "op_code", payload.get("op_codes", []), "站別")
    return await defect_codes.create(db, payload, actor)


async def create_wafer(db, payload: dict, actor: str) -> dict:
    await _assert_exists(db, COL_DEVICES, "device_id", [payload["device_id"]], "產品料號")
    doc = dict(payload)
    gross, good = int(doc.get("gross_die") or 0), int(doc.get("cp_good_die") or 0)
    if good > gross:
        raise ValidationError(f"CP 良品數 {good} 不可大於總晶粒數 {gross}")
    # 服務層也會被腳本直接呼叫，CP 良率的推算不能只放在 API 的輸入模型
    if doc.get("cp_yield") is None:
        doc["cp_yield"] = round(good / gross, 4) if gross else None
    doc.setdefault("received_at", utcnow())
    doc.update(assembly_lot_id=None, consumed=False)
    return await wafers.create(db, doc, actor)


async def get_route_step(route: dict, seq: int) -> dict | None:
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
    steps = sorted(route.get("steps", []), key=lambda s: s["seq"])
    for step in steps:
        if step["seq"] > current_seq:
            return step
    return None
