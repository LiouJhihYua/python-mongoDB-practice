"""PostgreSQL 連線與結構管理。

支援兩種後端：
* ``postgres`` —— 連線既有的 PostgreSQL（正式環境）
* ``embedded`` —— 以 pgserver 就地啟動一個 PostgreSQL 實例，
  免安裝即可展示／跑測試，資料會保留在 ``MES_EMBEDDED_DATA_DIR``

整個技術堆疊都採寬鬆開源授權：PostgreSQL（PostgreSQL License）、
asyncpg（Apache-2.0）、FastAPI / Pydantic / uvicorn（MIT / BSD）。
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

# ── 資料表名稱 ──────────────────────────────────────────────
T_USERS = "users"
T_CUSTOMERS = "customers"
T_PACKAGES = "packages"
T_DEVICES = "devices"
T_OPERATIONS = "operations"
T_ROUTES = "routes"
T_EQUIPMENTS = "equipments"
T_EQUIPMENT_LOGS = "equipment_state_logs"
T_PM_TASKS = "pm_tasks"
T_WORK_ORDERS = "work_orders"
T_LOTS = "lots"
T_LOT_HISTORY = "lot_history"
T_DEFECT_CODES = "defect_codes"
T_DEFECT_RECORDS = "defect_records"
T_HOLDS = "holds"
T_MATERIALS = "materials"
T_MATERIAL_TXNS = "material_transactions"
T_WAFERS = "wafers"
T_SHIPMENTS = "shipments"
T_MEASUREMENT_ITEMS = "measurement_items"
T_MEASUREMENTS = "measurements"
T_TOOLS = "tools"
T_TOOL_LOGS = "tool_logs"
T_WAFER_MAPS = "wafer_maps"
T_DIE_ASSIGNMENTS = "die_assignments"
T_SOPS = "sops"
T_SOP_ACKS = "sop_acknowledgements"
T_ERP_INBOUND = "erp_inbound"
T_ERP_OUTBOUND = "erp_outbound"
T_SECS_LINKS = "secs_links"
T_SECS_MESSAGES = "secs_messages"
T_SECS_EVENT_RULES = "secs_event_rules"
T_RECIPES = "recipes"
T_EQUIPMENT_RECIPES = "equipment_recipes"
T_RECIPE_CHECKS = "recipe_checks"
T_SAMPLING_PLANS = "sampling_plans"
T_SAMPLING_LEVELS = "sampling_levels"
T_INSPECTIONS = "inspection_records"
T_COMPLAINTS = "complaints"
T_COMPLAINT_EVENTS = "complaint_events"
T_CARRIERS = "carriers"
T_CARRIER_LOGS = "carrier_logs"
T_COUNTERS = "counters"
T_AUDIT = "audit_logs"

#: 依外鍵相依順序排列，清空資料時照這個順序 TRUNCATE
ALL_TABLES = (
    T_AUDIT, T_SECS_MESSAGES, T_SECS_EVENT_RULES, T_SECS_LINKS,
    T_RECIPE_CHECKS, T_EQUIPMENT_RECIPES, T_RECIPES,
    T_INSPECTIONS, T_SAMPLING_LEVELS, T_SAMPLING_PLANS,
    T_COMPLAINT_EVENTS, T_COMPLAINTS, T_CARRIER_LOGS, T_CARRIERS,
    T_ERP_OUTBOUND, T_ERP_INBOUND, T_SOP_ACKS, T_SOPS,
    T_DIE_ASSIGNMENTS, T_WAFER_MAPS,
    T_TOOL_LOGS, T_TOOLS, T_MATERIAL_TXNS, T_MEASUREMENTS,
    T_MEASUREMENT_ITEMS, T_PM_TASKS, T_EQUIPMENT_LOGS, T_DEFECT_RECORDS,
    T_HOLDS, T_LOT_HISTORY, T_LOTS, T_SHIPMENTS, T_WORK_ORDERS, T_WAFERS,
    T_MATERIALS, T_DEFECT_CODES, T_EQUIPMENTS, T_DEVICES, T_ROUTES,
    T_OPERATIONS, T_PACKAGES, T_CUSTOMERS, T_USERS, T_COUNTERS,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
#: 記錄已套用的版本，讓既有資料庫也能安全升級
MIGRATIONS_TABLE = "schema_migrations"


class _DBState:
    pool: asyncpg.Pool | None = None
    embedded_server: Any = None
    dsn: str | None = None


_state = _DBState()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """讓 JSONB 欄位可以直接收送 Python 的 dict / list。"""
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


def _start_embedded() -> str:
    """以 pgserver 啟動內嵌 PostgreSQL，回傳連線字串。"""
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "embedded 模式需要 pgserver 套件：pip install pgserver\n"
            "或改用 MES_DB_BACKEND=postgres 連線既有的 PostgreSQL"
        ) from exc

    data_dir = Path(settings.embedded_data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.warning("啟動內嵌 PostgreSQL（資料目錄 %s）", data_dir)
    _state.embedded_server = pgserver.get_server(data_dir)
    return _state.embedded_server.get_uri()


async def _ensure_database(dsn: str, db_name: str) -> str:
    """內嵌模式下建立專屬資料庫（已存在則略過）。"""
    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()
    base, _, _ = dsn.partition("?")
    query = dsn[len(base):]
    head, _, _old = base.rpartition("/")
    return f"{head}/{db_name}{query}"


def migration_files() -> list[Path]:
    """依檔名排序的 migration 清單（``0001_baseline.sql``、``0002_....sql`` …）。"""
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


async def applied_migrations(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(f"SELECT version FROM {MIGRATIONS_TABLE}")
    return {r["version"] for r in rows}


async def apply_schema(conn: asyncpg.Connection) -> list[str]:
    """依序套用尚未執行的 migration，回傳這次實際套用的版本。

    ``0001_baseline`` 是完整的初始結構，內容全部是 ``IF NOT EXISTS``，
    因此在既有資料庫上重跑也是安全的 —— 跑完只是把版本記進去，
    之後的變更就都走 migration，不會再有「程式碼與資料庫結構不一致」的問題。

    每個 migration 各自跑在一個交易裡：中途失敗不會留下半套結構。
    """
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    done = await applied_migrations(conn)
    applied: list[str] = []
    for path in migration_files():
        version = path.stem
        if version in done:
            continue
        logger.info("套用 migration %s", version)
        async with conn.transaction():
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute(
                f"INSERT INTO {MIGRATIONS_TABLE} (version) VALUES ($1)", version
            )
        applied.append(version)
    return applied


async def migration_status(conn: asyncpg.Connection) -> list[dict]:
    """列出所有 migration 與是否已套用（供維運確認）。"""
    rows = await conn.fetch(
        f"SELECT version, applied_at FROM {MIGRATIONS_TABLE} ORDER BY version"
    )
    when = {r["version"]: r["applied_at"] for r in rows}
    return [
        {"version": p.stem, "applied": p.stem in when, "applied_at": when.get(p.stem)}
        for p in migration_files()
    ]


async def connect_db(dsn: str | None = None, force: bool = False) -> asyncpg.Pool:
    """建立連線池並確保資料表存在。

    已連線時預設直接沿用，讓腳本與 API 共用同一個連線池。
    """
    if _state.pool is not None and not force and dsn is None:
        return _state.pool

    if dsn is None:
        if settings.db_backend == "embedded":
            dsn = await _ensure_database(_start_embedded(), settings.database_name)
        else:
            dsn = settings.database_url

    _state.dsn = dsn
    _state.pool = await asyncpg.create_pool(
        dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        init=_init_connection,
        command_timeout=60,
    )
    async with _state.pool.acquire() as conn:
        await apply_schema(conn)
    return _state.pool


async def close_db() -> None:
    if _state.pool is not None:
        await _state.pool.close()
    _state.pool = None
    if _state.embedded_server is not None:
        _state.embedded_server.cleanup()
        _state.embedded_server = None


def get_pool() -> asyncpg.Pool:
    if _state.pool is None:
        raise RuntimeError("資料庫尚未初始化，請先呼叫 connect_db()")
    return _state.pool


@asynccontextmanager
async def acquire():
    """取得一條連線（腳本與背景工作用）。"""
    async with get_pool().acquire() as conn:
        yield conn


async def get_db():
    """FastAPI 相依：每個請求取用一條連線，讓服務層能開交易。"""
    async with get_pool().acquire() as conn:
        yield conn


async def truncate_all(conn: asyncpg.Connection) -> None:
    """清空所有資料（測試與 seed --reset 用）。"""
    tables = ", ".join(ALL_TABLES)
    await conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


# ── 查詢輔助 ────────────────────────────────────────────────
def to_dict(record: asyncpg.Record | None) -> dict | None:
    return dict(record) if record is not None else None


def to_dicts(records) -> list[dict]:
    return [dict(r) for r in records]


async def fetch_one(db, sql: str, *args) -> dict | None:
    return to_dict(await db.fetchrow(sql, *args))


async def fetch_all(db, sql: str, *args) -> list[dict]:
    return to_dicts(await db.fetch(sql, *args))


async def fetch_value(db, sql: str, *args):
    return await db.fetchval(sql, *args)


async def execute(db, sql: str, *args) -> str:
    return await db.execute(sql, *args)


async def next_sequence(db, key: str) -> int:
    """以 counters 資料表產生連號（工單／批號／出貨單）。"""
    return await db.fetchval(
        """
        INSERT INTO counters (key, seq) VALUES ($1, 1)
        ON CONFLICT (key) DO UPDATE SET seq = counters.seq + 1
        RETURNING seq
        """,
        key,
    )
