"""抽樣檢驗（Sampling / AQL）。

站別主檔一直有 ``sampling_rate`` 這個欄位，但先前沒有任何邏輯讀它 ——
設了「抽檢 10%」其實還是全檢。這個模組把抽樣真正做完：

1. **要不要驗** —— 依抽樣計畫的跳批間隔決定這一批是全檢、抽檢還是免檢
2. **驗幾顆** —— 依批量落在哪個級距取樣本數與允收／拒收數
3. **判允收還是拒收** —— 檢出不良數 ≥ 拒收數就退回品保處置

允收水準刻意做成「資料」而不是寫死的常數：ANSI/ASQ Z1.4 只是起點，
每家客戶談定的計畫都不一樣，品保必須能依合約自行維護級距。
本模組提供 Z1.4 的樣本大小代字對照當作建表的輔助，
但實際採用的 Ac/Re **一律以資料庫裡的計畫為準**。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import (
    T_INSPECTIONS,
    T_OPERATIONS,
    T_SAMPLING_LEVELS,
    T_SAMPLING_PLANS,
    fetch_all,
    fetch_one,
)
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import json_safe, shift_of, utcnow
from app.services import audit_service

PLAN_FULL = "FULL"
PLAN_SKIP_LOT = "SKIP_LOT"
PLAN_AQL = "AQL"

DECISION_FULL = "FULL"
DECISION_SAMPLED = "SAMPLED"
DECISION_SKIPPED = "SKIPPED"

RESULT_PENDING = "PENDING"
RESULT_ACCEPT = "ACCEPT"
RESULT_REJECT = "REJECT"
RESULT_NOT_REQUIRED = "NOT_REQUIRED"

#: ANSI/ASQ Z1.4 一般檢驗水準 II 的樣本大小代字與樣本數。
#: 只用來輔助建表（換算批量 → 建議樣本數），實際 Ac/Re 以資料庫的計畫為準。
Z14_LEVEL_II: tuple[tuple[int, int | None, str, int], ...] = (
    (2, 8, "A", 2),
    (9, 15, "B", 3),
    (16, 25, "C", 5),
    (26, 50, "D", 8),
    (51, 90, "E", 13),
    (91, 150, "F", 20),
    (151, 280, "G", 32),
    (281, 500, "H", 50),
    (501, 1_200, "J", 80),
    (1_201, 3_200, "K", 125),
    (3_201, 10_000, "L", 200),
    (10_001, 35_000, "M", 315),
    (35_001, 150_000, "N", 500),
    (150_001, 500_000, "P", 800),
    (500_001, None, "Q", 1_250),
)


def z14_code_letter(lot_size: int) -> dict:
    """依批量查 Z1.4 樣本大小代字與建議樣本數（建立抽樣計畫時的輔助）。"""
    for low, high, letter, sample in Z14_LEVEL_II:
        if lot_size >= low and (high is None or lot_size <= high):
            return {
                "lot_size": lot_size, "code_letter": letter, "sample_size": sample,
                "lot_size_from": low, "lot_size_to": high, "inspection_level": "II",
            }
    return {
        "lot_size": lot_size, "code_letter": "A", "sample_size": min(2, max(1, lot_size)),
        "lot_size_from": 1, "lot_size_to": 1, "inspection_level": "II",
    }


# ── 抽樣計畫維護 ────────────────────────────────────────────
async def create_plan(db, payload: dict, actor: str) -> dict:
    async with db.transaction():
        op = await fetch_one(
            db, f"SELECT op_code FROM {T_OPERATIONS} WHERE op_code = $1", payload["op_code"]
        )
        if op is None:
            raise ValidationError(f"站別不存在：{payload['op_code']}")
        if payload.get("plan_type", PLAN_AQL) == PLAN_AQL and not payload.get("levels"):
            raise ValidationError("AQL 計畫必須至少提供一段批量級距")

        now = utcnow()
        plan = await fetch_one(
            db,
            f"""
            INSERT INTO {T_SAMPLING_PLANS}
                (plan_code, name, op_code, device_id, customer_code, plan_type, lot_interval,
                 aql, inspection_level, remark, active, created_at, created_by, updated_at, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $12, $13)
            RETURNING *
            """,
            payload["plan_code"], payload["name"], payload["op_code"],
            payload.get("device_id", ""), payload.get("customer_code", ""),
            payload.get("plan_type", PLAN_AQL), int(payload.get("lot_interval", 1)),
            float(payload.get("aql", 1.0)), payload.get("inspection_level", "II"),
            payload.get("remark", ""), bool(payload.get("active", True)), now, actor,
        )
        await _replace_levels(db, payload["plan_code"], payload.get("levels") or [])
        await audit_service.record_change(
            db, actor, "CREATE", T_SAMPLING_PLANS, payload["plan_code"], payload
        )
        return {**plan, "levels": await plan_levels(db, payload["plan_code"])}


async def _replace_levels(db, plan_code: str, levels: list[dict]) -> None:
    await db.execute(f"DELETE FROM {T_SAMPLING_LEVELS} WHERE plan_code = $1", plan_code)
    if not levels:
        return
    rows = []
    for level in sorted(levels, key=lambda x: int(x["lot_size_from"])):
        accept = int(level["accept_number"])
        reject = int(level.get("reject_number") or accept + 1)
        if reject <= accept:
            raise ValidationError(f"拒收數 {reject} 必須大於允收數 {accept}")
        rows.append(
            (
                plan_code, int(level["lot_size_from"]),
                None if level.get("lot_size_to") in (None, "") else int(level["lot_size_to"]),
                level.get("code_letter", ""), int(level["sample_size"]), accept, reject,
            )
        )
    # 級距不可重疊，否則同一批量會查到兩組 Ac/Re
    for previous, current in zip(rows, rows[1:]):
        if previous[2] is None or current[1] <= previous[2]:
            raise ValidationError(
                f"批量級距重疊或未封閉：{previous[1]}~{previous[2]} 與 {current[1]}~{current[2]}"
            )
    await db.executemany(
        f"""
        INSERT INTO {T_SAMPLING_LEVELS}
            (plan_code, lot_size_from, lot_size_to, code_letter, sample_size, accept_number, reject_number)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        rows,
    )


async def update_plan(db, plan_code: str, patch: dict, actor: str) -> dict:
    async with db.transaction():
        plan = await fetch_one(
            db, f"SELECT * FROM {T_SAMPLING_PLANS} WHERE plan_code = $1 FOR UPDATE", plan_code
        )
        if plan is None:
            raise NotFoundError(f"找不到抽樣計畫：{plan_code}")

        allowed = {"name", "device_id", "customer_code", "plan_type", "lot_interval",
                   "aql", "inspection_level", "remark", "active"}
        fields = {k: v for k, v in patch.items() if k in allowed and v is not None}
        if fields:
            fields["updated_at"] = utcnow()
            fields["updated_by"] = actor
            assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(fields, start=1))
            plan = await fetch_one(
                db,
                f"UPDATE {T_SAMPLING_PLANS} SET {assignments} "
                f"WHERE plan_code = ${len(fields) + 1} RETURNING *",
                *fields.values(), plan_code,
            )
        if patch.get("levels") is not None:
            await _replace_levels(db, plan_code, patch["levels"])
        await audit_service.record_change(
            db, actor, "UPDATE", T_SAMPLING_PLANS, plan_code, patch
        )
        return {**plan, "levels": await plan_levels(db, plan_code)}


async def plan_levels(db, plan_code: str) -> list[dict]:
    return await fetch_all(
        db,
        f"SELECT * FROM {T_SAMPLING_LEVELS} WHERE plan_code = $1 ORDER BY lot_size_from",
        plan_code,
    )


async def get_plan(db, plan_code: str) -> dict:
    plan = await fetch_one(db, f"SELECT * FROM {T_SAMPLING_PLANS} WHERE plan_code = $1", plan_code)
    if plan is None:
        raise NotFoundError(f"找不到抽樣計畫：{plan_code}")
    return {**plan, "levels": await plan_levels(db, plan_code)}


async def list_plans(db, op_code: str | None = None, active: bool | None = None) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT p.*, (SELECT count(*) FROM {T_SAMPLING_LEVELS} l WHERE l.plan_code = p.plan_code)
                    AS level_count
        FROM {T_SAMPLING_PLANS} p
        WHERE ($1::text IS NULL OR p.op_code = $1)
          AND ($2::boolean IS NULL OR p.active = $2)
        ORDER BY p.op_code, p.plan_code
        """,
        op_code, active,
    )


async def delete_plan(db, plan_code: str, actor: str) -> dict:
    await get_plan(db, plan_code)
    await db.execute(f"DELETE FROM {T_SAMPLING_PLANS} WHERE plan_code = $1", plan_code)
    await audit_service.record_change(db, actor, "DELETE", T_SAMPLING_PLANS, plan_code, None)
    return {"plan_code": plan_code, "deleted": True}


# ── 適用計畫與抽樣決策 ──────────────────────────────────────
async def applicable_plan(db, op_code: str, device_id: str = "", customer_code: str = "") -> dict | None:
    """挑適用的抽樣計畫：料號專用優先於客戶專用，客戶專用優先於通用。"""
    return await fetch_one(
        db,
        f"""
        SELECT * FROM {T_SAMPLING_PLANS}
        WHERE op_code = $1 AND active
          AND (device_id = $2 OR device_id = '')
          AND (customer_code = $3 OR customer_code = '')
        ORDER BY (device_id = $2) DESC, (customer_code = $3) DESC, plan_code
        LIMIT 1
        """,
        op_code, device_id or "", customer_code or "",
    )


def level_for(levels: list[dict], lot_size: int) -> dict | None:
    for level in levels:
        low = int(level["lot_size_from"])
        high = level["lot_size_to"]
        if lot_size >= low and (high is None or lot_size <= int(high)):
            return level
    return None


async def _seen_count(db, op_code: str, device_id: str) -> int:
    """這個站別／料號已經做過幾次抽檢決策 —— 跳批輪替的計數依據。

    用計數而不是隨機決定：抽檢比例必須可稽核，不能每次跑出來的結果都不一樣。
    """
    return int(
        await db.fetchval(
            f"SELECT count(*) FROM {T_INSPECTIONS} WHERE op_code = $1 AND device_id = $2",
            op_code, device_id,
        )
        or 0
    )


async def decide(db, lot: dict, operation: dict, actor: str) -> dict:
    """進站時決定這一批要全檢、抽檢還是免檢，並開立一筆待判定的檢驗紀錄。

    跳批以「已檢驗次數」計數而不是隨機決定 —— 抽檢比例必須是可稽核的，
    不能每次執行結果都不一樣。
    """
    op_code = operation["op_code"]
    device_id = lot.get("device_id", "")
    plan = await applicable_plan(db, op_code, device_id, lot.get("customer_code", ""))

    lot_size = int(lot.get("qty") or 0)
    if plan is not None:
        interval = int(plan["lot_interval"] or 1)
    else:
        # 沒有計畫就退回站別主檔的抽檢比例：0.1 表示每 10 批驗 1 批。
        # 這裡不能用 `or 1.0` —— 0 是「本站免檢」的合法設定，會被 or 吃掉。
        raw = operation.get("sampling_rate")
        rate = 1.0 if raw is None else float(raw)
        interval = 0 if rate <= 0 else 1 if rate >= 1 else max(1, round(1 / rate))

    if interval == 0:
        return await _record(
            db, lot, operation, plan, DECISION_SKIPPED, lot_size, 0, None, None,
            RESULT_NOT_REQUIRED, actor, "抽檢比例為 0，本站免檢",
        )

    seen = await _seen_count(db, op_code, device_id)
    # 每 interval 批驗 1 批：以「這個站別／料號已決策過幾批」輪替，
    # 第 0、interval、2×interval… 批輪到檢驗，其餘跳過
    if interval > 1 and seen % interval != 0:
        return await _record(
            db, lot, operation, plan, DECISION_SKIPPED, lot_size, 0, None, None,
            RESULT_NOT_REQUIRED, actor,
            f"跳批：每 {interval} 批驗 1 批，本批為第 {seen % interval + 1} 批",
        )

    # 沒有抽樣計畫時，sampling_rate 只說明「多久驗一批」，推導不出樣本數，
    # 因此輪到的那一批一律全檢
    plan_type = plan["plan_type"] if plan is not None else PLAN_FULL
    if plan_type == PLAN_FULL:
        return await _record(
            db, lot, operation, plan, DECISION_FULL, lot_size, lot_size, None, None,
            RESULT_PENDING, actor,
            "全檢" if interval == 1 else f"輪到本批，全檢（每 {interval} 批驗 1 批）",
        )
    if plan_type == PLAN_SKIP_LOT:
        return await _record(
            db, lot, operation, plan, DECISION_FULL, lot_size, lot_size, None, None,
            RESULT_PENDING, actor, f"跳批計畫：輪到本批，全檢（每 {interval} 批驗 1 批）",
        )

    levels = await plan_levels(db, plan["plan_code"])
    level = level_for(levels, lot_size)
    if level is None:
        suggestion = z14_code_letter(lot_size)
        raise StateError(
            f"抽樣計畫 {(plan or {}).get('plan_code', '(未設定)')} 沒有涵蓋批量 {lot_size} 的級距"
            f"（Z1.4 水準 II 建議樣本數 {suggestion['sample_size']}，代字 {suggestion['code_letter']}）"
        )
    return await _record(
        db, lot, operation, plan, DECISION_SAMPLED, lot_size,
        int(level["sample_size"]), int(level["accept_number"]), int(level["reject_number"]),
        RESULT_PENDING, actor,
        f"AQL {plan['aql']}：抽 {level['sample_size']} 顆，允收 {level['accept_number']} / "
        f"拒收 {level['reject_number']}",
    )


async def _record(
    db, lot: dict, operation: dict, plan: dict | None, decision: str, lot_size: int,
    sample_size: int, accept: int | None, reject: int | None, result: str,
    actor: str, remark: str,
) -> dict:
    now = utcnow()
    return await fetch_one(
        db,
        f"""
        INSERT INTO {T_INSPECTIONS}
            (lot_id, wo_no, device_id, customer_code, op_code, seq, eq_id, plan_code,
             decision, lot_size, sample_size, accept_number, reject_number,
             result, remark, inspector, timestamp, shift)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
        RETURNING *
        """,
        lot["lot_id"], lot.get("wo_no"), lot.get("device_id", ""), lot.get("customer_code", ""),
        operation["op_code"], lot.get("current_seq"), lot.get("eq_id"),
        (plan or {}).get("plan_code"), decision, lot_size, sample_size, accept, reject,
        result, remark, actor, now, shift_of(now),
    )


# ── 判定 ────────────────────────────────────────────────────
async def judge(db, payload: dict, actor: str) -> dict:
    """回報檢出不良數並做允收／拒收判定。"""
    lot_id = payload["lot_id"]
    async with db.transaction():
        record = await fetch_one(
            db,
            f"""
            SELECT * FROM {T_INSPECTIONS}
            WHERE lot_id = $1 AND result = $2
            ORDER BY timestamp DESC, id DESC LIMIT 1 FOR UPDATE
            """,
            lot_id, RESULT_PENDING,
        )
        if record is None:
            raise NotFoundError(f"批號 {lot_id} 沒有待判定的檢驗紀錄")

        found = int(payload["defect_found"])
        if found < 0:
            raise ValidationError("檢出不良數不可為負")
        if record["sample_size"] and found > int(record["sample_size"]):
            raise ValidationError(
                f"檢出不良數 {found} 超過樣本數 {record['sample_size']}"
            )

        reject_number = record["reject_number"]
        if reject_number is None:
            # 全檢沒有 Ac/Re：只要有不良就退回品保處置
            accepted = found == 0
        else:
            accepted = found < int(reject_number)

        result = RESULT_ACCEPT if accepted else RESULT_REJECT
        updated = await fetch_one(
            db,
            f"""
            UPDATE {T_INSPECTIONS}
            SET defect_found = $1, result = $2, defects = $3, remark = $4,
                inspector = $5, timestamp = $6, shift = $7
            WHERE id = $8 RETURNING *
            """,
            found, result, json_safe(payload.get("defects") or []),
            payload.get("remark", "") or record["remark"], actor,
            utcnow(), shift_of(utcnow()), record["id"],
        )
        await audit_service.record_change(
            db, actor, "INSPECT", T_INSPECTIONS, lot_id,
            {"result": result, "defect_found": found, "sample_size": record["sample_size"]},
        )
        return {
            **updated,
            "accepted": accepted,
            "message": (
                f"允收：檢出 {found} 顆，未達拒收數 {reject_number}"
                if accepted else
                f"拒收：檢出 {found} 顆，達到拒收數 {reject_number}，請開立扣留並處置"
            ),
        }


async def pending_inspections(db, op_code: str | None = None, limit: int = 100) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_INSPECTIONS}
        WHERE result = $1 AND ($2::text IS NULL OR op_code = $2)
        ORDER BY timestamp LIMIT $3
        """,
        RESULT_PENDING, op_code, limit,
    )


async def list_inspections(
    db, lot_id: str | None = None, op_code: str | None = None,
    result: str | None = None, limit: int = 200,
) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT * FROM {T_INSPECTIONS}
        WHERE ($1::text IS NULL OR lot_id = $1)
          AND ($2::text IS NULL OR op_code = $2)
          AND ($3::text IS NULL OR result = $3)
        ORDER BY timestamp DESC, id DESC LIMIT $4
        """,
        lot_id, op_code, result, limit,
    )


async def summary(db, start: datetime, end: datetime) -> dict:
    """抽檢概況：驗了幾批、跳過幾批、拒收率。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT op_code, decision, result, count(*) AS qty,
               sum(sample_size) AS sampled, sum(coalesce(defect_found, 0)) AS defects
        FROM {T_INSPECTIONS}
        WHERE timestamp >= $1 AND timestamp < $2
        GROUP BY op_code, decision, result
        ORDER BY op_code
        """,
        start, end,
    )
    by_op: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_op.setdefault(
            row["op_code"],
            {"op_code": row["op_code"], "inspected": 0, "skipped": 0,
             "accepted": 0, "rejected": 0, "sampled_units": 0, "defects_found": 0},
        )
        qty = int(row["qty"])
        if row["decision"] == DECISION_SKIPPED:
            entry["skipped"] += qty
        else:
            entry["inspected"] += qty
        if row["result"] == RESULT_ACCEPT:
            entry["accepted"] += qty
        elif row["result"] == RESULT_REJECT:
            entry["rejected"] += qty
        entry["sampled_units"] += int(row["sampled"] or 0)
        entry["defects_found"] += int(row["defects"] or 0)

    items = []
    for entry in by_op.values():
        judged = entry["accepted"] + entry["rejected"]
        total = entry["inspected"] + entry["skipped"]
        items.append(
            {
                **entry,
                "reject_rate": round(entry["rejected"] / judged, 4) if judged else None,
                "sampling_ratio": round(entry["inspected"] / total, 4) if total else None,
            }
        )
    items.sort(key=lambda x: x["op_code"])
    return {
        "start": start, "end": end, "by_operation": items,
        "total_inspected": sum(i["inspected"] for i in items),
        "total_skipped": sum(i["skipped"] for i in items),
        "total_rejected": sum(i["rejected"] for i in items),
    }
