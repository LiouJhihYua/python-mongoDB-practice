"""SPC 統計製程管制：量測收集、X-bar/R 管制圖、製程能力與判異規則。

站別的良率只告訴你「已經做壞了多少」，SPC 告訴你「製程正在往壞的方向漂」——
在做出不良品之前就攔下來。
"""

from __future__ import annotations

import statistics
from datetime import datetime

from app.database import (
    COL_LOTS,
    COL_MEASUREMENT_ITEMS,
    COL_MEASUREMENTS,
    COL_OPERATIONS,
)
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import clean, clean_all, shift_of, utcnow
from app.models.enums import HoldReason, LotStatus, SPCRule
from app.services.crud import CRUD

items = CRUD(COL_MEASUREMENT_ITEMS, "item_code", "量測項目")

#: 管制圖常數表（子群大小 n → A2 / D3 / D4 / d2）
CHART_CONSTANTS: dict[int, tuple[float, float, float, float]] = {
    2: (1.880, 0.0, 3.267, 1.128),
    3: (1.023, 0.0, 2.574, 1.693),
    4: (0.729, 0.0, 2.282, 2.059),
    5: (0.577, 0.0, 2.114, 2.326),
    6: (0.483, 0.0, 2.004, 2.534),
    7: (0.419, 0.076, 1.924, 2.704),
    8: (0.373, 0.136, 1.864, 2.847),
    9: (0.337, 0.184, 1.816, 2.970),
    10: (0.308, 0.223, 1.777, 3.078),
}

#: 建立管制界限所需的最少子群數（基準期）
BASELINE_SUBGROUPS = 12
#: 判異時往回看的子群數
RULE_WINDOW = 25


async def create_item(db, payload: dict, actor: str) -> dict:
    operation = await db[COL_OPERATIONS].find_one({"op_code": payload["op_code"]})
    if operation is None:
        raise ValidationError(f"站別不存在：{payload['op_code']}")

    # 規格檢核放在服務層，腳本直接呼叫時同樣成立
    doc = dict(payload)
    usl, lsl = doc.get("usl"), doc.get("lsl")
    if usl is None and lsl is None:
        raise ValidationError("規格上限與下限至少要設定一個")
    if usl is not None and lsl is not None:
        if usl <= lsl:
            raise ValidationError(f"規格上限 {usl} 必須大於下限 {lsl}")
        if doc.get("target") is None:
            doc["target"] = (usl + lsl) / 2
        elif not lsl <= doc["target"] <= usl:
            raise ValidationError(f"目標值 {doc['target']} 必須落在規格 [{lsl}, {usl}] 之間")
    if int(doc.get("sample_size", 5)) not in CHART_CONSTANTS:
        raise ValidationError(f"子群大小需介於 {min(CHART_CONSTANTS)}～{max(CHART_CONSTANTS)} 之間")
    return await items.create(db, doc, actor)


async def get_item(db, item_code: str) -> dict:
    doc = await db[COL_MEASUREMENT_ITEMS].find_one({"item_code": item_code})
    if doc is None:
        raise NotFoundError(f"找不到量測項目：{item_code}")
    return doc


# ── 量測收集 ────────────────────────────────────────────────
async def record_measurement(db, payload: dict, user: dict) -> dict:
    """記錄一次子群量測，即時判定超規與管制圖異常。"""
    actor = user["username"]
    item = await get_item(db, payload["item_code"])
    if not item.get("active", True):
        raise StateError(f"量測項目 {item['item_code']} 已停用")

    lot = await db[COL_LOTS].find_one({"lot_id": payload["lot_id"]})
    if lot is None:
        raise NotFoundError(f"找不到批號：{payload['lot_id']}")
    if lot["current_op"] != item["op_code"]:
        raise ValidationError(
            f"批號 {lot['lot_id']} 目前在 {lot['current_op']} 站，"
            f"與量測項目所屬站別 {item['op_code']} 不符"
        )
    if item.get("device_id") and item["device_id"] != lot["device_id"]:
        raise ValidationError(f"量測項目 {item['item_code']} 僅適用料號 {item['device_id']}")

    values = [float(v) for v in payload["values"]]
    if len(values) != int(item["sample_size"]):
        raise ValidationError(
            f"量測筆數 {len(values)} 與項目設定的子群大小 {item['sample_size']} 不符"
        )

    usl, lsl = item.get("usl"), item.get("lsl")
    out_of_spec = [
        v for v in values
        if (usl is not None and v > usl) or (lsl is not None and v < lsl)
    ]

    now = utcnow()
    doc = {
        "item_code": item["item_code"],
        "item_name": item.get("name", ""),
        "lot_id": lot["lot_id"],
        "device_id": lot["device_id"],
        "op_code": item["op_code"],
        "eq_id": payload.get("eq_id") or lot.get("eq_id"),
        "operator": actor,
        "values": values,
        "mean": round(statistics.fmean(values), 6),
        "range": round(max(values) - min(values), 6),
        "min": min(values),
        "max": max(values),
        "stdev": round(statistics.stdev(values), 6) if len(values) > 1 else 0.0,
        "out_of_spec": bool(out_of_spec),
        "out_of_spec_values": out_of_spec,
        "violations": [],
        "remark": payload.get("remark", ""),
        "timestamp": now,
        "shift": shift_of(now),
    }
    result = await db[COL_MEASUREMENTS].insert_one(dict(doc))

    # 納入本筆後重新判異
    violations = await _detect_violations(db, item, now)
    if out_of_spec:
        violations.insert(0, SPCRule.OUT_OF_SPEC.value)
    if violations:
        await db[COL_MEASUREMENTS].update_one(
            {"_id": result.inserted_id}, {"$set": {"violations": violations}}
        )

    held = False
    if violations and item.get("auto_hold_on_violation", True):
        held = await _hold_for_spc(db, lot, item, violations, actor)

    saved = clean(await db[COL_MEASUREMENTS].find_one({"_id": result.inserted_id}))
    saved["violations"] = violations
    saved["lot_held"] = held
    return saved


async def _hold_for_spc(db, lot: dict, item: dict, violations: list[str], actor: str) -> bool:
    """SPC 異常自動扣留批號；已扣留中則不重複開單。"""
    from app.services import lot_service

    if lot["status"] not in {LotStatus.WAITING.value, LotStatus.RUNNING.value}:
        return False
    if await lot_service.find_open_hold(db, lot["lot_id"]):
        return False
    await lot_service.apply_hold(
        db, lot, HoldReason.QUALITY, actor,
        f"SPC 異常（{item['item_code']}）：{', '.join(violations)}",
    )
    return True


# ── 判異規則 ────────────────────────────────────────────────
async def _recent_subgroups(db, item_code: str, before: datetime, limit: int = RULE_WINDOW) -> list[dict]:
    cursor = (
        db[COL_MEASUREMENTS]
        .find({"item_code": item_code, "timestamp": {"$lte": before}})
        .sort([("timestamp", -1)])
        .limit(limit)
    )
    rows = await cursor.to_list(length=limit)
    return list(reversed(rows))


def limits_from_subgroups(subgroups: list[dict], sample_size: int) -> dict:
    """由子群平均與全距推算 X-bar / R 管制界限。

    X-bar 圖的 UCL = X̿ + A2·R̄ 即 X̿ + 3σ_x̄，所以 σ_x̄ = A2·R̄ / 3；
    判異規則要用的是這個 σ，而不是子群平均自己的標準差
    （用後者等於讓界限跟著製程漂移一起移動，永遠測不到漂移）。
    """
    a2, d3, d4, _d2 = CHART_CONSTANTS[sample_size]
    means = [s["mean"] for s in subgroups]
    ranges = [s["range"] for s in subgroups]
    x_bar_bar = statistics.fmean(means)
    r_bar = statistics.fmean(ranges)
    sigma_xbar = a2 * r_bar / 3
    return {
        "x_bar_bar": round(x_bar_bar, 6),
        "r_bar": round(r_bar, 6),
        "sigma_xbar": round(sigma_xbar, 6),
        "x_ucl": round(x_bar_bar + a2 * r_bar, 6),
        "x_lcl": round(x_bar_bar - a2 * r_bar, 6),
        "r_ucl": round(d4 * r_bar, 6),
        "r_lcl": round(d3 * r_bar, 6),
        "subgroups": len(subgroups),
    }


async def establish_limits(
    db, item_code: str, start: datetime | None = None, end: datetime | None = None, actor: str = "system"
) -> dict:
    """以基準期資料建立並凍結管制界限。

    正規 SPC 的做法：工程師挑一段製程穩定的期間當基準，之後所有點都跟
    這組固定界限比對。製程漂移時界限不會跟著跑，才測得出來。
    """
    item = await get_item(db, item_code)
    match: dict = {"item_code": item_code}
    if start or end:
        window: dict = {}
        if start:
            window["$gte"] = start
        if end:
            window["$lt"] = end
        match["timestamp"] = window

    rows = await db[COL_MEASUREMENTS].find(match).sort([("timestamp", 1)]).to_list(length=None)
    if len(rows) < BASELINE_SUBGROUPS:
        raise ValidationError(
            f"基準期只有 {len(rows)} 組子群，需至少 {BASELINE_SUBGROUPS} 組才能建立管制界限"
        )
    limits = limits_from_subgroups(rows, int(item["sample_size"]))
    limits.update(established_at=utcnow(), established_by=actor)
    await db[COL_MEASUREMENT_ITEMS].update_one(
        {"item_code": item_code}, {"$set": {"control_limits": limits, "updated_at": utcnow()}}
    )
    return {"item_code": item_code, "control_limits": limits}


async def _detect_violations(db, item: dict, before: datetime) -> list[str]:
    limits = item.get("control_limits")
    if not limits:
        # 累積足夠基準資料後自動建立一次界限，之後即固定
        count = await db[COL_MEASUREMENTS].count_documents({"item_code": item["item_code"]})
        if count < BASELINE_SUBGROUPS:
            return []
        limits = (await establish_limits(db, item["item_code"], actor="auto"))["control_limits"]

    subgroups = await _recent_subgroups(db, item["item_code"], before)
    means = [s["mean"] for s in subgroups]
    return check_rules(means, limits["x_bar_bar"], limits["sigma_xbar"])


def check_rules(means: list[float], center: float, sigma: float) -> list[str]:
    """對最新一點套用 Nelson rules 常用子集，回傳觸發的規則。"""
    if sigma <= 0 or not means:
        return []
    violations: list[str] = []
    latest = means[-1]

    # 規則 1：單點超出 3σ
    if abs(latest - center) > 3 * sigma:
        violations.append(SPCRule.BEYOND_3SIGMA.value)

    # 規則 2：連續 9 點在中心線同側
    if len(means) >= 9:
        tail = means[-9:]
        if all(v > center for v in tail) or all(v < center for v in tail):
            violations.append(SPCRule.RUN_9_SAME_SIDE.value)

    # 規則 3：連續 6 點持續上升或下降
    if len(means) >= 7:
        tail = means[-7:]
        diffs = [b - a for a, b in zip(tail, tail[1:])]
        if all(d > 0 for d in diffs) or all(d < 0 for d in diffs):
            violations.append(SPCRule.TREND_6.value)

    # 規則 4：連續三點中有兩點落在同側 2σ 外
    if len(means) >= 3:
        tail = means[-3:]
        above = sum(1 for v in tail if v - center > 2 * sigma)
        below = sum(1 for v in tail if center - v > 2 * sigma)
        if max(above, below) >= 2 and abs(latest - center) > 2 * sigma:
            violations.append(SPCRule.TWO_OF_THREE_2SIGMA.value)

    return violations


# ── 管制圖與製程能力 ────────────────────────────────────────
async def control_chart(
    db, item_code: str, start: datetime, end: datetime, lot_id: str | None = None
) -> dict:
    """X-bar / R 管制圖：以歷史子群推算管制界限，並標出異常點。"""
    item = await get_item(db, item_code)
    match: dict = {"item_code": item_code, "timestamp": {"$gte": start, "$lt": end}}
    if lot_id:
        match["lot_id"] = lot_id

    rows = await db[COL_MEASUREMENTS].find(match).sort([("timestamp", 1)]).to_list(length=None)
    points = [
        {
            "timestamp": r["timestamp"],
            "lot_id": r["lot_id"],
            "eq_id": r.get("eq_id"),
            "mean": r["mean"],
            "range": r["range"],
            "values": r["values"],
            "out_of_spec": r.get("out_of_spec", False),
            "violations": r.get("violations", []),
        }
        for r in rows
    ]

    sample_size = int(item["sample_size"])
    # 優先採用已凍結的管制界限；沒有的話才用本區間資料暫算
    limits = item.get("control_limits")
    limits_source = "已建立的基準期"
    if not limits:
        limits_source = "本區間暫算"
        limits = limits_from_subgroups(rows, sample_size) if len(rows) >= BASELINE_SUBGROUPS else None
    if limits is None:
        limits_source = None

    return {
        "item": {
            "item_code": item["item_code"], "name": item.get("name", ""),
            "op_code": item["op_code"], "unit": item.get("unit", ""),
            "usl": item.get("usl"), "lsl": item.get("lsl"), "target": item.get("target"),
            "sample_size": sample_size,
        },
        "window": {"start": start, "end": end},
        "points": points,
        "control_limits": limits,
        "limits_source": limits_source,
        "note": None if limits else f"子群數不足 {BASELINE_SUBGROUPS} 組，尚無法建立管制界限",
    }


async def capability(
    db, item_code: str, start: datetime, end: datetime, device_id: str | None = None
) -> dict:
    """製程能力指數 Cp / Cpk / Ca。

    Cp 只看變異，Cpk 同時看變異與偏移 —— 兩者差距大代表製程雖穩定但沒對準中心。
    """
    item = await get_item(db, item_code)
    match: dict = {"item_code": item_code, "timestamp": {"$gte": start, "$lt": end}}
    if device_id:
        match["device_id"] = device_id

    rows = await db[COL_MEASUREMENTS].find(match).to_list(length=None)
    values = [v for r in rows for v in r["values"]]
    result = {
        "item_code": item_code,
        "name": item.get("name", ""),
        "unit": item.get("unit", ""),
        "usl": item.get("usl"), "lsl": item.get("lsl"), "target": item.get("target"),
        "window": {"start": start, "end": end},
        "sample_count": len(values),
        "subgroup_count": len(rows),
    }
    if len(values) < 2:
        return {**result, "mean": None, "stdev": None, "cp": None, "cpk": None, "ca": None,
                "out_of_spec_count": 0, "note": "量測筆數不足，無法計算製程能力"}

    mean = statistics.fmean(values)
    sigma = statistics.stdev(values)
    usl, lsl, target = item.get("usl"), item.get("lsl"), item.get("target")

    cp = cpu = cpl = ca = None
    if sigma > 0:
        if usl is not None and lsl is not None:
            cp = (usl - lsl) / (6 * sigma)
        if usl is not None:
            cpu = (usl - mean) / (3 * sigma)
        if lsl is not None:
            cpl = (mean - lsl) / (3 * sigma)
    cpk = min(x for x in (cpu, cpl) if x is not None) if (cpu is not None or cpl is not None) else None
    if usl is not None and lsl is not None and target is not None:
        half_range = (usl - lsl) / 2
        ca = abs(mean - target) / half_range if half_range else None

    out_of_spec = sum(
        1 for v in values
        if (usl is not None and v > usl) or (lsl is not None and v < lsl)
    )
    return {
        **result,
        "mean": round(mean, 6),
        "stdev": round(sigma, 6),
        "cp": round(cp, 3) if cp is not None else None,
        "cpk": round(cpk, 3) if cpk is not None else None,
        "ca": round(ca, 3) if ca is not None else None,
        "out_of_spec_count": out_of_spec,
        "grade": _grade(cpk),
        "note": None,
    }


def _grade(cpk: float | None) -> str:
    """業界常用的 Cpk 分級。"""
    if cpk is None:
        return "N/A"
    if cpk >= 1.67:
        return "A+ 製程優異"
    if cpk >= 1.33:
        return "A 製程足夠"
    if cpk >= 1.0:
        return "B 勉強可接受，須改善"
    if cpk >= 0.67:
        return "C 製程不足，需立即改善"
    return "D 製程失控"


async def list_measurements(
    db, item_code: str | None = None, lot_id: str | None = None,
    violations_only: bool = False, limit: int = 200,
) -> list[dict]:
    filt: dict = {k: v for k, v in {"item_code": item_code, "lot_id": lot_id}.items() if v}
    if violations_only:
        filt["violations"] = {"$ne": []}
    cursor = db[COL_MEASUREMENTS].find(filt).sort([("timestamp", -1)]).limit(limit)
    return clean_all(await cursor.to_list(length=limit))


async def violation_summary(db, start: datetime, end: datetime) -> list[dict]:
    """各量測項目的判異次數，供看板顯示。"""
    rows = await db[COL_MEASUREMENTS].aggregate(
        [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}, "violations": {"$ne": []}}},
            {
                "$group": {
                    "_id": {"item": "$item_code", "name": "$item_name", "op": "$op_code"},
                    "count": {"$sum": 1},
                    "lots": {"$addToSet": "$lot_id"},
                }
            },
            {"$sort": {"count": -1}},
        ]
    ).to_list(length=None)
    return [
        {
            "item_code": r["_id"]["item"],
            "item_name": r["_id"]["name"],
            "op_code": r["_id"]["op"],
            "violations": r["count"],
            "affected_lots": sorted(r["lots"]),
        }
        for r in rows
    ]
