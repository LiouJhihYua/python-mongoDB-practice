"""屬性管制圖（p / np / c / u）與 EWMA。

``spc_service`` 處理的是**計量值**（拉力、厚度、線徑）——量得出數字的東西。
但封測廠有一半的品質資料是**計數值**：這批有幾顆不良、這片有幾個缺點。
那要用屬性管制圖，數學基礎完全不同（二項分布／卜瓦松分布，不是常態分布）。

| 圖別 | 統計量 | 用在哪裡 |
| --- | --- | --- |
| p  | 不良率，樣本數可變 | 各站不良率（每批批量不同，所以界限逐點變動） |
| np | 不良數，樣本數固定 | 固定抽樣數的外觀檢 |
| c  | 缺點數，檢驗面積固定 | 每片晶圓的缺點數 |
| u  | 單位缺點數，面積可變 | 每千顆的缺點數 |

另外提供 **EWMA**：X-bar 圖對「一次跳很大」很敏感，但對「每天漂一點點」很鈍。
EWMA 把歷史值加權累積起來，正好補這個洞。

資料直接取自 ``lot_history`` 的出站紀錄（應產出量當樣本數、不良數當不良），
不需要另外建一套資料收集 —— 現場已經在填的東西就是最好的資料來源。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.database import T_DEFECT_RECORDS, T_LOT_HISTORY, fetch_all
from app.errors import ValidationError
from app.models.enums import LotAction

#: 至少要幾個點才算得出有意義的中心線
MIN_POINTS = 8
#: EWMA 的平滑係數，0.2 是偵測小幅漂移的常用值
EWMA_LAMBDA = 0.2
#: EWMA 管制界限的寬度（σ 的倍數）
EWMA_L = 3.0


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


# ── p 圖：不良率，樣本數可變 ────────────────────────────────
def p_chart(points: list[dict]) -> dict:
    """``points`` 每一筆需含 ``n``（樣本數）與 ``d``（不良數）。

    樣本數每批不同，因此管制界限是**逐點變動**的 —— 批量小的那一點界限自然寬。
    """
    usable = [p for p in points if int(p.get("n") or 0) > 0]
    if len(usable) < MIN_POINTS:
        return {"chart": "p", "ready": False, "points": [],
                "message": f"至少需要 {MIN_POINTS} 個資料點，目前 {len(usable)} 點"}

    total_n = sum(int(p["n"]) for p in usable)
    total_d = sum(int(p["d"]) for p in usable)
    p_bar = total_d / total_n

    out = []
    for point in usable:
        n = int(point["n"])
        d = int(point["d"])
        sigma = math.sqrt(p_bar * (1 - p_bar) / n) if n else 0.0
        ucl = _clamp01(p_bar + 3 * sigma)
        lcl = _clamp01(p_bar - 3 * sigma)
        value = d / n
        out.append(
            {
                **{k: v for k, v in point.items() if k not in ("n", "d")},
                "n": n, "d": d, "value": round(value, 6),
                "ucl": round(ucl, 6), "lcl": round(lcl, 6),
                "out_of_control": value > ucl or value < lcl,
            }
        )
    return {
        "chart": "p", "ready": True, "center": round(p_bar, 6),
        "total_n": total_n, "total_d": total_d,
        "points": out, "violations": sum(1 for p in out if p["out_of_control"]),
    }


# ── np 圖：不良數，樣本數固定 ───────────────────────────────
def np_chart(points: list[dict]) -> dict:
    usable = [p for p in points if int(p.get("n") or 0) > 0]
    if len(usable) < MIN_POINTS:
        return {"chart": "np", "ready": False, "points": [],
                "message": f"至少需要 {MIN_POINTS} 個資料點，目前 {len(usable)} 點"}
    sizes = {int(p["n"]) for p in usable}
    if len(sizes) != 1:
        raise ValidationError(
            f"np 圖要求樣本數固定，目前出現 {len(sizes)} 種樣本數；樣本數可變請改用 p 圖"
        )

    n = sizes.pop()
    p_bar = sum(int(p["d"]) for p in usable) / (n * len(usable))
    center = n * p_bar
    sigma = math.sqrt(center * (1 - p_bar))
    ucl = center + 3 * sigma
    lcl = max(0.0, center - 3 * sigma)
    out = [
        {
            **{k: v for k, v in point.items() if k not in ("n", "d")},
            "n": n, "value": int(point["d"]),
            "ucl": round(ucl, 4), "lcl": round(lcl, 4),
            "out_of_control": int(point["d"]) > ucl or int(point["d"]) < lcl,
        }
        for point in usable
    ]
    return {
        "chart": "np", "ready": True, "sample_size": n,
        "center": round(center, 4), "ucl": round(ucl, 4), "lcl": round(lcl, 4),
        "points": out, "violations": sum(1 for p in out if p["out_of_control"]),
    }


# ── c 圖：缺點數，檢驗面積固定 ──────────────────────────────
def c_chart(points: list[dict]) -> dict:
    """``points`` 每一筆需含 ``c``（缺點數）。"""
    usable = [p for p in points if p.get("c") is not None]
    if len(usable) < MIN_POINTS:
        return {"chart": "c", "ready": False, "points": [],
                "message": f"至少需要 {MIN_POINTS} 個資料點，目前 {len(usable)} 點"}

    c_bar = sum(int(p["c"]) for p in usable) / len(usable)
    sigma = math.sqrt(c_bar)
    ucl = c_bar + 3 * sigma
    lcl = max(0.0, c_bar - 3 * sigma)
    out = [
        {
            **{k: v for k, v in point.items() if k != "c"},
            "value": int(point["c"]),
            "ucl": round(ucl, 4), "lcl": round(lcl, 4),
            "out_of_control": int(point["c"]) > ucl or int(point["c"]) < lcl,
        }
        for point in usable
    ]
    return {
        "chart": "c", "ready": True, "center": round(c_bar, 4),
        "ucl": round(ucl, 4), "lcl": round(lcl, 4),
        "points": out, "violations": sum(1 for p in out if p["out_of_control"]),
    }


# ── u 圖：單位缺點數，面積可變 ──────────────────────────────
def u_chart(points: list[dict]) -> dict:
    """``points`` 每一筆需含 ``c``（缺點數）與 ``n``（檢驗單位數）。"""
    usable = [p for p in points if int(p.get("n") or 0) > 0]
    if len(usable) < MIN_POINTS:
        return {"chart": "u", "ready": False, "points": [],
                "message": f"至少需要 {MIN_POINTS} 個資料點，目前 {len(usable)} 點"}

    total_c = sum(int(p["c"]) for p in usable)
    total_n = sum(int(p["n"]) for p in usable)
    u_bar = total_c / total_n

    out = []
    for point in usable:
        n = int(point["n"])
        c = int(point["c"])
        sigma = math.sqrt(u_bar / n) if n else 0.0
        ucl = u_bar + 3 * sigma
        lcl = max(0.0, u_bar - 3 * sigma)
        value = c / n
        out.append(
            {
                **{k: v for k, v in point.items() if k not in ("n", "c")},
                "n": n, "c": c, "value": round(value, 6),
                "ucl": round(ucl, 6), "lcl": round(lcl, 6),
                "out_of_control": value > ucl or value < lcl,
            }
        )
    return {
        "chart": "u", "ready": True, "center": round(u_bar, 6),
        "total_c": total_c, "total_n": total_n,
        "points": out, "violations": sum(1 for p in out if p["out_of_control"]),
    }


# ── EWMA：偵測小幅但持續的漂移 ──────────────────────────────
def ewma_chart(
    values: list[float], center: float | None = None, sigma: float | None = None,
    lam: float = EWMA_LAMBDA, width: float = EWMA_L,
) -> dict:
    """指數加權移動平均。

    X-bar 圖對「單點跳很大」敏感，對「每天漂一點點」很鈍；
    EWMA 把歷史值以 λ 衰減加權累積起來，正好補這個洞。
    界限在前幾點較窄、之後收斂到穩態值，因此逐點計算。
    """
    if len(values) < MIN_POINTS:
        return {"chart": "ewma", "ready": False, "points": [],
                "message": f"至少需要 {MIN_POINTS} 個資料點，目前 {len(values)} 點"}
    if not 0 < lam <= 1:
        raise ValidationError("EWMA 的 λ 必須介於 0 與 1 之間")

    mu = center if center is not None else sum(values) / len(values)
    if sigma is None:
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        sigma = math.sqrt(variance)

    out = []
    z = mu
    for index, value in enumerate(values, start=1):
        z = lam * value + (1 - lam) * z
        spread = width * sigma * math.sqrt(
            (lam / (2 - lam)) * (1 - (1 - lam) ** (2 * index))
        )
        ucl, lcl = mu + spread, mu - spread
        out.append(
            {
                "index": index, "raw": round(value, 6), "value": round(z, 6),
                "ucl": round(ucl, 6), "lcl": round(lcl, 6),
                "out_of_control": z > ucl or z < lcl,
            }
        )
    return {
        "chart": "ewma", "ready": True, "lambda": lam, "width": width,
        "center": round(mu, 6), "sigma": round(sigma, 6),
        "points": out, "violations": sum(1 for p in out if p["out_of_control"]),
    }


# ── 資料來源 ────────────────────────────────────────────────
async def defect_rate_points(
    db, op_code: str, start: datetime, end: datetime,
    device_id: str | None = None, limit: int = 200,
) -> list[dict]:
    """自出站紀錄取「應產出量 / 不良數」，就是 p 圖要的資料。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT lot_id, device_id, eq_id, timestamp, shift,
               qty_expected AS n, qty_reject AS d
        FROM {T_LOT_HISTORY}
        WHERE action = $1 AND op_code = $2
          AND timestamp >= $3 AND timestamp < $4
          AND qty_expected > 0
          AND ($5::text IS NULL OR device_id = $5)
        ORDER BY timestamp
        LIMIT $6
        """,
        LotAction.TRACK_OUT.value, op_code, start, end, device_id, limit,
    )
    return [
        {
            "lot_id": r["lot_id"], "device_id": r["device_id"], "eq_id": r["eq_id"],
            "timestamp": r["timestamp"], "shift": r["shift"],
            "n": int(r["n"]), "d": int(r["d"]),
        }
        for r in rows
    ]


async def defect_count_points(
    db, op_code: str, start: datetime, end: datetime, limit: int = 200,
) -> list[dict]:
    """自不良紀錄取「每批的缺點數」，供 c / u 圖使用。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT d.lot_id,
               min(d.timestamp) AS timestamp,
               sum(d.qty)       AS c,
               max(h.qty_expected) AS n
        FROM {T_DEFECT_RECORDS} d
        LEFT JOIN {T_LOT_HISTORY} h
               ON h.lot_id = d.lot_id AND h.op_code = d.op_code AND h.action = $1
        WHERE d.op_code = $2 AND d.timestamp >= $3 AND d.timestamp < $4
        GROUP BY d.lot_id
        ORDER BY min(d.timestamp)
        LIMIT $5
        """,
        LotAction.TRACK_OUT.value, op_code, start, end, limit,
    )
    return [
        {
            "lot_id": r["lot_id"], "timestamp": r["timestamp"],
            "c": int(r["c"] or 0), "n": max(1, int(r["n"] or 1)),
        }
        for r in rows
    ]


CHART_BUILDERS = {"p": p_chart, "np": np_chart, "c": c_chart, "u": u_chart}


async def build_chart(
    db, chart: str, op_code: str, start: datetime, end: datetime,
    device_id: str | None = None, limit: int = 200,
) -> dict:
    """依圖別取對應的資料來源並算出管制圖。"""
    kind = chart.lower()
    if kind not in CHART_BUILDERS and kind != "ewma":
        raise ValidationError(f"不支援的管制圖類型：{chart}（可用 p / np / c / u / ewma）")

    if kind in ("p", "np"):
        points = await defect_rate_points(db, op_code, start, end, device_id, limit)
        result = CHART_BUILDERS[kind](points)
    elif kind in ("c", "u"):
        points = await defect_count_points(db, op_code, start, end, limit)
        result = CHART_BUILDERS[kind](points)
    else:
        points = await defect_rate_points(db, op_code, start, end, device_id, limit)
        values = [p["d"] / p["n"] for p in points if p["n"]]
        result = ewma_chart(values)
        result["source_points"] = points

    return {"op_code": op_code, "device_id": device_id, "start": start, "end": end, **result}


async def overview(db, start: datetime, end: datetime) -> list[dict]:
    """各站的不良率 p 圖摘要 —— 一眼看出哪一站的製程失控。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT op_code, count(*) AS lots,
               sum(qty_expected) AS n, sum(qty_reject) AS d
        FROM {T_LOT_HISTORY}
        WHERE action = $1 AND timestamp >= $2 AND timestamp < $3 AND qty_expected > 0
        GROUP BY op_code
        HAVING count(*) >= $4
        ORDER BY sum(qty_reject)::numeric / NULLIF(sum(qty_expected), 0) DESC
        """,
        LotAction.TRACK_OUT.value, start, end, MIN_POINTS,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        points = await defect_rate_points(db, row["op_code"], start, end)
        chart = p_chart(points)
        out.append(
            {
                "op_code": row["op_code"],
                "lots": int(row["lots"]),
                "units": int(row["n"] or 0),
                "defects": int(row["d"] or 0),
                "defect_rate": round(int(row["d"] or 0) / int(row["n"] or 1), 6),
                "center": chart.get("center"),
                "violations": chart.get("violations", 0),
                "ready": chart.get("ready", False),
            }
        )
    return out
