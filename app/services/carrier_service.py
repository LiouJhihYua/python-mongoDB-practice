"""載具管理（Magazine / Boat / Tray / FOUP）。

先前 ``lots.carrier_id`` 只是一個自由文字欄位，擋不住「同一個 Magazine
同時掛兩批」—— 這在現場真的會發生，而且發生了很難查。

這裡把載具做成有狀態的主檔：指派時佔用、釋放時歸還，
並靠資料庫的部分唯一索引（``ux_carrier_lot``）在最底層擋住重複佔用，
而不是仰賴應用程式每個路徑都記得檢查。

順帶管理清洗週期：載具用久了會殘留封膠與粉塵，是外觀不良的隱形來源。
"""

from __future__ import annotations

from app.database import T_CARRIER_LOGS, T_CARRIERS, T_LOTS, fetch_all, fetch_one
from app.errors import DuplicateError, NotFoundError, StateError, ValidationError
from app.models.base import utcnow
from app.services import audit_service

STATUS_EMPTY = "EMPTY"
STATUS_IN_USE = "IN_USE"
STATUS_DIRTY = "DIRTY"
STATUS_MAINTENANCE = "MAINTENANCE"
STATUS_SCRAPPED = "SCRAPPED"

#: 可以被指派給批號的狀態
ASSIGNABLE = frozenset({STATUS_EMPTY})

ACTION_ASSIGN = "ASSIGN"
ACTION_RELEASE = "RELEASE"
ACTION_CLEAN = "CLEAN"
ACTION_MAINTAIN = "MAINTAIN"
ACTION_SCRAP = "SCRAP"


async def create_carrier(db, payload: dict, actor: str) -> dict:
    existing = await fetch_one(
        db, f"SELECT carrier_id FROM {T_CARRIERS} WHERE carrier_id = $1", payload["carrier_id"]
    )
    if existing is not None:
        raise DuplicateError(f"載具已存在：{payload['carrier_id']}")

    now = utcnow()
    row = await fetch_one(
        db,
        f"""
        INSERT INTO {T_CARRIERS}
            (carrier_id, carrier_type, capacity, status, location, clean_interval,
             remark, active, created_at, created_by, updated_at, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $9, $10)
        RETURNING *
        """,
        payload["carrier_id"], payload.get("carrier_type", "MAGAZINE"),
        int(payload.get("capacity", 0)), STATUS_EMPTY, payload.get("location", ""),
        int(payload.get("clean_interval", 0)), payload.get("remark", ""),
        bool(payload.get("active", True)), now, actor,
    )
    await audit_service.record_change(db, actor, "CREATE", T_CARRIERS, payload["carrier_id"], payload)
    return row


async def get_carrier(db, carrier_id: str) -> dict:
    row = await fetch_one(db, f"SELECT * FROM {T_CARRIERS} WHERE carrier_id = $1", carrier_id)
    if row is None:
        raise NotFoundError(f"找不到載具：{carrier_id}")
    return row


async def list_carriers(
    db, status: str | None = None, carrier_type: str | None = None, limit: int = 200
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT c.*,
               CASE WHEN c.clean_interval > 0 AND c.use_count >= c.clean_interval
                    THEN TRUE ELSE FALSE END AS needs_cleaning
        FROM {T_CARRIERS} c
        WHERE ($1::text IS NULL OR c.status = $1)
          AND ($2::text IS NULL OR c.carrier_type = $2)
        ORDER BY c.carrier_id LIMIT $3
        """,
        status, carrier_type, limit,
    )


async def _log(db, carrier_id: str, action: str, lot_id: str | None,
               status: str, actor: str, remark: str = "") -> None:
    await db.execute(
        f"""
        INSERT INTO {T_CARRIER_LOGS} (carrier_id, action, lot_id, status, remark, operator, timestamp)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        carrier_id, action, lot_id, status, remark, actor, utcnow(),
    )


async def assign(db, carrier_id: str, lot_id: str, actor: str, remark: str = "") -> dict:
    """把載具指派給批號。已被別的批號佔用就擋下來。"""
    async with db.transaction():
        carrier = await fetch_one(
            db, f"SELECT * FROM {T_CARRIERS} WHERE carrier_id = $1 FOR UPDATE", carrier_id
        )
        if carrier is None:
            raise NotFoundError(f"找不到載具：{carrier_id}")
        if not carrier["active"]:
            raise StateError(f"載具 {carrier_id} 已停用")
        if carrier["current_lot_id"] == lot_id:
            return carrier  # 重複指派同一批視為冪等
        if carrier["current_lot_id"]:
            raise StateError(
                f"載具 {carrier_id} 目前掛著批號 {carrier['current_lot_id']}，請先釋放"
            )
        if carrier["status"] not in ASSIGNABLE:
            raise StateError(
                f"載具 {carrier_id} 目前狀態為 {carrier['status']}，需先清洗／保養完成才能使用"
            )

        lot = await fetch_one(db, f"SELECT lot_id, qty FROM {T_LOTS} WHERE lot_id = $1", lot_id)
        if lot is None:
            raise ValidationError(f"批號不存在：{lot_id}")
        capacity = int(carrier["capacity"] or 0)
        if capacity and int(lot["qty"] or 0) > capacity:
            raise ValidationError(
                f"批量 {lot['qty']} 超過載具 {carrier_id} 的容量 {capacity}"
            )

        now = utcnow()
        row = await fetch_one(
            db,
            f"""
            UPDATE {T_CARRIERS}
            SET status = $1, current_lot_id = $2, use_count = use_count + 1,
                updated_at = $3, updated_by = $4
            WHERE carrier_id = $5 RETURNING *
            """,
            STATUS_IN_USE, lot_id, now, actor, carrier_id,
        )
        await db.execute(
            f"UPDATE {T_LOTS} SET carrier_id = $1, updated_at = $2 WHERE lot_id = $3",
            carrier_id, now, lot_id,
        )
        await _log(db, carrier_id, ACTION_ASSIGN, lot_id, STATUS_IN_USE, actor, remark)
        return {**row, "needs_cleaning": _needs_cleaning(row)}


def _needs_cleaning(carrier: dict) -> bool:
    interval = int(carrier.get("clean_interval") or 0)
    return bool(interval) and int(carrier.get("use_count") or 0) >= interval


async def release(db, carrier_id: str, actor: str, remark: str = "") -> dict:
    """釋放載具。達到清洗週期時自動轉為待清洗，不會直接回到可用。"""
    async with db.transaction():
        carrier = await fetch_one(
            db, f"SELECT * FROM {T_CARRIERS} WHERE carrier_id = $1 FOR UPDATE", carrier_id
        )
        if carrier is None:
            raise NotFoundError(f"找不到載具：{carrier_id}")
        if not carrier["current_lot_id"]:
            raise StateError(f"載具 {carrier_id} 目前沒有掛任何批號")

        lot_id = carrier["current_lot_id"]
        status = STATUS_DIRTY if _needs_cleaning(carrier) else STATUS_EMPTY
        now = utcnow()
        row = await fetch_one(
            db,
            f"""
            UPDATE {T_CARRIERS}
            SET status = $1, current_lot_id = NULL, updated_at = $2, updated_by = $3
            WHERE carrier_id = $4 RETURNING *
            """,
            status, now, actor, carrier_id,
        )
        await db.execute(
            f"UPDATE {T_LOTS} SET carrier_id = '' WHERE lot_id = $1 AND carrier_id = $2",
            lot_id, carrier_id,
        )
        await _log(
            db, carrier_id, ACTION_RELEASE, lot_id, status, actor,
            remark or (f"使用 {row['use_count']} 次已達清洗週期" if status == STATUS_DIRTY else ""),
        )
        return {**row, "released_lot_id": lot_id, "needs_cleaning": status == STATUS_DIRTY}


async def assign_if_known(db, carrier_id: str, lot_id: str, actor: str) -> dict | None:
    """載具有建主檔才走佔用流程。

    現場常常先用起來、之後才補建主檔，因此沒建檔的載具編號只當文字保留，
    不阻擋開批 —— 但只要建了檔，就享有「不可重複佔用」的保護。
    """
    known = await fetch_one(db, f"SELECT carrier_id FROM {T_CARRIERS} WHERE carrier_id = $1", carrier_id)
    if known is None:
        return None
    return await assign(db, carrier_id, lot_id, actor, "開批時自動指派")


async def release_for_lot(db, lot_id: str, actor: str, remark: str = "") -> dict | None:
    """批號完工／報廢時自動歸還其載具（沒有載具就靜靜跳過）。"""
    carrier = await fetch_one(
        db, f"SELECT carrier_id FROM {T_CARRIERS} WHERE current_lot_id = $1", lot_id
    )
    if carrier is None:
        return None
    return await release(db, carrier["carrier_id"], actor, remark)


async def clean(db, carrier_id: str, actor: str, remark: str = "") -> dict:
    """完成清洗，使用次數歸零。"""
    async with db.transaction():
        carrier = await get_carrier(db, carrier_id)
        if carrier["current_lot_id"]:
            raise StateError(f"載具 {carrier_id} 仍掛著批號 {carrier['current_lot_id']}，不可清洗")
        now = utcnow()
        row = await fetch_one(
            db,
            f"""
            UPDATE {T_CARRIERS}
            SET status = $1, use_count = 0, last_cleaned_at = $2, updated_at = $2, updated_by = $3
            WHERE carrier_id = $4 RETURNING *
            """,
            STATUS_EMPTY, now, actor, carrier_id,
        )
        await _log(db, carrier_id, ACTION_CLEAN, None, STATUS_EMPTY, actor, remark)
        await audit_service.record_change(db, actor, "CLEAN", T_CARRIERS, carrier_id, None)
        return row


async def set_status(db, carrier_id: str, status: str, actor: str, remark: str = "") -> dict:
    """人工調整狀態（送修、報廢…）。"""
    valid = {STATUS_EMPTY, STATUS_DIRTY, STATUS_MAINTENANCE, STATUS_SCRAPPED}
    if status not in valid:
        raise ValidationError(f"不支援的載具狀態：{status}（可用 {', '.join(sorted(valid))}）")

    async with db.transaction():
        carrier = await get_carrier(db, carrier_id)
        if carrier["current_lot_id"]:
            raise StateError(
                f"載具 {carrier_id} 仍掛著批號 {carrier['current_lot_id']}，請先釋放再調整狀態"
            )
        row = await fetch_one(
            db,
            f"""
            UPDATE {T_CARRIERS} SET status = $1, updated_at = $2, updated_by = $3,
                                    active = CASE WHEN $1 = $4 THEN FALSE ELSE active END
            WHERE carrier_id = $5 RETURNING *
            """,
            status, utcnow(), actor, STATUS_SCRAPPED, carrier_id,
        )
        action = ACTION_SCRAP if status == STATUS_SCRAPPED else ACTION_MAINTAIN
        await _log(db, carrier_id, action, None, status, actor, remark)
        await audit_service.record_change(
            db, actor, "SET_STATUS", T_CARRIERS, carrier_id, {"status": status, "remark": remark}
        )
        return row


async def carrier_history(db, carrier_id: str, limit: int = 100) -> list[dict]:
    await get_carrier(db, carrier_id)
    return await fetch_all(
        db,
        f"SELECT * FROM {T_CARRIER_LOGS} WHERE carrier_id = $1 ORDER BY timestamp DESC LIMIT $2",
        carrier_id, limit,
    )


async def overview(db) -> dict:
    """載具看板：各狀態數量與待清洗清單。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT carrier_type, status, count(*) AS qty
        FROM {T_CARRIERS} GROUP BY carrier_type, status ORDER BY carrier_type, status
        """,
    )
    by_type: dict[str, dict] = {}
    for row in rows:
        entry = by_type.setdefault(row["carrier_type"], {"carrier_type": row["carrier_type"], "total": 0})
        entry[row["status"]] = int(row["qty"])
        entry["total"] += int(row["qty"])

    dirty = await fetch_all(
        db,
        f"""
        SELECT carrier_id, carrier_type, status, use_count, clean_interval, last_cleaned_at
        FROM {T_CARRIERS}
        WHERE status = $1 OR (clean_interval > 0 AND use_count >= clean_interval)
        ORDER BY use_count DESC LIMIT 50
        """,
        STATUS_DIRTY,
    )
    return {
        "by_type": [by_type[k] for k in sorted(by_type)],
        "total": sum(e["total"] for e in by_type.values()),
        "in_use": sum(e.get(STATUS_IN_USE, 0) for e in by_type.values()),
        "available": sum(e.get(STATUS_EMPTY, 0) for e in by_type.values()),
        "needs_cleaning": dirty,
    }
