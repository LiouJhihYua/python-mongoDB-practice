"""MongoDB 連線與索引管理。

支援兩種後端：
* ``mongo``  —— 透過 motor 連線真實 MongoDB（正式環境）
* ``memory`` —— 使用 mongomock 的記憶體資料庫，免安裝即可展示／跑測試
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel

from app.config import settings

logger = logging.getLogger(__name__)

# ── Collection 名稱 ──────────────────────────────────────────
COL_USERS = "users"
COL_CUSTOMERS = "customers"
COL_PACKAGES = "packages"
COL_DEVICES = "devices"
COL_OPERATIONS = "operations"
COL_ROUTES = "routes"
COL_EQUIPMENTS = "equipments"
COL_EQUIPMENT_LOGS = "equipment_state_logs"
COL_PM_TASKS = "pm_tasks"
COL_WORK_ORDERS = "work_orders"
COL_LOTS = "lots"
COL_LOT_HISTORY = "lot_history"
COL_DEFECT_CODES = "defect_codes"
COL_DEFECT_RECORDS = "defect_records"
COL_HOLDS = "holds"
COL_MATERIALS = "materials"
COL_MATERIAL_TXNS = "material_transactions"
COL_WAFERS = "wafers"
COL_SHIPMENTS = "shipments"
COL_MEASUREMENT_ITEMS = "measurement_items"
COL_MEASUREMENTS = "measurements"
COL_TOOLS = "tools"
COL_TOOL_LOGS = "tool_logs"
COL_COUNTERS = "counters"
COL_AUDIT = "audit_logs"

INDEXES: dict[str, list[IndexModel]] = {
    COL_USERS: [IndexModel([("username", ASCENDING)], unique=True, name="ux_username")],
    COL_CUSTOMERS: [IndexModel([("code", ASCENDING)], unique=True, name="ux_code")],
    COL_PACKAGES: [IndexModel([("package_code", ASCENDING)], unique=True, name="ux_pkg")],
    COL_DEVICES: [
        IndexModel([("device_id", ASCENDING)], unique=True, name="ux_device"),
        IndexModel([("customer_code", ASCENDING)], name="ix_customer"),
    ],
    COL_OPERATIONS: [IndexModel([("op_code", ASCENDING)], unique=True, name="ux_op")],
    COL_ROUTES: [
        IndexModel([("route_code", ASCENDING), ("version", ASCENDING)], unique=True, name="ux_route_ver"),
    ],
    COL_EQUIPMENTS: [
        IndexModel([("eq_id", ASCENDING)], unique=True, name="ux_eq"),
        IndexModel([("area", ASCENDING), ("current_state", ASCENDING)], name="ix_area_state"),
    ],
    COL_EQUIPMENT_LOGS: [
        IndexModel([("eq_id", ASCENDING), ("start_time", DESCENDING)], name="ix_eq_time"),
    ],
    COL_PM_TASKS: [IndexModel([("eq_id", ASCENDING), ("due_date", ASCENDING)], name="ix_eq_due")],
    COL_WORK_ORDERS: [
        IndexModel([("wo_no", ASCENDING)], unique=True, name="ux_wo"),
        IndexModel([("status", ASCENDING), ("due_date", ASCENDING)], name="ix_status_due"),
    ],
    COL_LOTS: [
        IndexModel([("lot_id", ASCENDING)], unique=True, name="ux_lot"),
        IndexModel([("status", ASCENDING), ("current_op", ASCENDING)], name="ix_status_op"),
        IndexModel([("wo_no", ASCENDING)], name="ix_wo"),
        IndexModel([("device_id", ASCENDING)], name="ix_device"),
        IndexModel([("parent_lot_id", ASCENDING)], name="ix_parent"),
    ],
    COL_LOT_HISTORY: [
        IndexModel([("lot_id", ASCENDING), ("timestamp", ASCENDING)], name="ix_lot_time"),
        IndexModel([("op_code", ASCENDING), ("timestamp", DESCENDING)], name="ix_op_time"),
        IndexModel([("eq_id", ASCENDING), ("timestamp", DESCENDING)], name="ix_eq_time"),
        IndexModel([("action", ASCENDING), ("timestamp", DESCENDING)], name="ix_action_time"),
    ],
    COL_DEFECT_CODES: [IndexModel([("code", ASCENDING)], unique=True, name="ux_defect")],
    COL_DEFECT_RECORDS: [
        IndexModel([("lot_id", ASCENDING)], name="ix_lot"),
        IndexModel([("defect_code", ASCENDING), ("timestamp", DESCENDING)], name="ix_code_time"),
    ],
    COL_HOLDS: [
        IndexModel([("lot_id", ASCENDING), ("status", ASCENDING)], name="ix_lot_status"),
    ],
    COL_MATERIALS: [IndexModel([("material_id", ASCENDING)], unique=True, name="ux_material")],
    COL_MATERIAL_TXNS: [
        IndexModel([("lot_id", ASCENDING)], name="ix_lot"),
        IndexModel([("material_id", ASCENDING), ("timestamp", DESCENDING)], name="ix_mat_time"),
    ],
    COL_WAFERS: [
        IndexModel([("wafer_id", ASCENDING)], unique=True, name="ux_wafer"),
        IndexModel([("assembly_lot_id", ASCENDING)], name="ix_asm_lot"),
    ],
    COL_SHIPMENTS: [IndexModel([("shipment_no", ASCENDING)], unique=True, name="ux_shipment")],
    COL_MEASUREMENT_ITEMS: [
        IndexModel([("item_code", ASCENDING)], unique=True, name="ux_item"),
        IndexModel([("op_code", ASCENDING)], name="ix_op"),
    ],
    COL_MEASUREMENTS: [
        IndexModel([("item_code", ASCENDING), ("timestamp", ASCENDING)], name="ix_item_time"),
        IndexModel([("lot_id", ASCENDING)], name="ix_lot"),
        IndexModel([("eq_id", ASCENDING), ("timestamp", DESCENDING)], name="ix_eq_time"),
    ],
    COL_TOOLS: [
        IndexModel([("tool_id", ASCENDING)], unique=True, name="ux_tool"),
        IndexModel([("eq_id", ASCENDING), ("status", ASCENDING)], name="ix_eq_status"),
    ],
    COL_TOOL_LOGS: [IndexModel([("tool_id", ASCENDING), ("timestamp", DESCENDING)], name="ix_tool_time")],
    COL_COUNTERS: [IndexModel([("_id", ASCENDING)], name="ix_counter")],
    COL_AUDIT: [
        IndexModel([("timestamp", DESCENDING)], name="ix_time"),
        IndexModel([("actor", ASCENDING), ("timestamp", DESCENDING)], name="ix_actor_time"),
    ],
}


class _DBState:
    client: Any = None
    db: Any = None


_state = _DBState()


def _make_client(url: str):
    if settings.db_backend == "memory":
        from mongomock_motor import AsyncMongoMockClient

        logger.warning("使用記憶體資料庫（mongomock）—— 資料不會保留，僅供展示／測試")
        return AsyncMongoMockClient()

    from motor.motor_asyncio import AsyncIOMotorClient

    return AsyncIOMotorClient(url, serverSelectionTimeoutMS=5000, tz_aware=True)


async def connect_db(url: str | None = None, db_name: str | None = None, force: bool = False):
    """建立連線並確保索引存在。

    已連線時預設直接沿用（記憶體後端若重連會遺失資料，腳本才能與 API 共用同一份）。
    """
    if _state.db is not None and not force and url is None and db_name is None:
        return _state.db
    _state.client = _make_client(url or settings.mongodb_url)
    _state.db = _state.client[db_name or settings.mongodb_db]
    await ensure_indexes(_state.db)
    return _state.db


async def close_db() -> None:
    if _state.client is not None and hasattr(_state.client, "close"):
        _state.client.close()
    _state.client = None
    _state.db = None


def get_db():
    """取得目前的資料庫控制代碼（FastAPI 相依注入用）。"""
    if _state.db is None:
        raise RuntimeError("資料庫尚未初始化，請先呼叫 connect_db()")
    return _state.db


def set_db(db) -> None:
    """測試用：直接注入資料庫。"""
    _state.db = db


async def ensure_indexes(db) -> None:
    for name, models in INDEXES.items():
        if name == COL_COUNTERS:  # _id 已是主鍵
            continue
        try:
            await db[name].create_indexes(models)
        except Exception as exc:  # pragma: no cover - mongomock 不支援部分索引選項
            logger.debug("建立 %s 索引時略過：%s", name, exc)


async def next_sequence(db, key: str, start: int = 1) -> int:
    """以 counters collection 產生連號（工單／批號／出貨單）。"""
    doc = await db[COL_COUNTERS].find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    if doc is None or "seq" not in doc:  # mongomock 舊版回傳 None
        doc = await db[COL_COUNTERS].find_one({"_id": key})
    seq = (doc or {}).get("seq", start)
    return int(seq)
