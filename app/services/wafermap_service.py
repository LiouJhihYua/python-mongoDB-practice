"""晶圓 Map 與 Die 級追溯。

一片 12 吋晶圓動輒上萬顆晶粒，逐顆存成一列會讓資料庫爆掉；
這裡把每一列以 Run-Length Encoding 壓成 ``"bin:count,bin:count,…"``，
一張完整的 map 只佔幾 KB，取用時再解回二維陣列。

除了保存 map，本模組提供 OSAT 現場真正會用到的三件事：

* **Bin 統計與帕累托** —— 哪個 Bin 吃掉了良率
* **邊緣良率與群聚不良分析** —— 區分「晶圓廠製程問題」與「隨機不良」
* **die → unit 綁定** —— 黏晶時記錄每顆成品來自哪個晶粒座標，
  客戶退回一顆不良品時可以直接回推到晶圓上的 (x, y)
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any

from app.database import (
    T_DIE_ASSIGNMENTS,
    T_LOTS,
    T_WAFER_MAPS,
    T_WAFERS,
    fetch_all,
    fetch_one,
)
from app.errors import DuplicateError, NotFoundError, StateError, ValidationError
from app.models.base import utcnow
from app.models.enums import ACTIVE_LOT_STATUSES
from app.services import audit_service

#: 邊緣良率預設往內數幾圈算「邊緣晶粒」
DEFAULT_EDGE_RINGS = 2
#: 幾顆以上的連續不良才算群聚（低於此值視為隨機不良）
DEFAULT_MIN_CLUSTER = 5
#: 邊緣與中心良率差距超過此值就提醒（晶圓廠邊緣製程異常的典型徵兆）
EDGE_GAP_ALERT = 0.05

ACTIVE_STATUS_VALUES = [s.value for s in ACTIVE_LOT_STATUSES]

STATUS_ASSIGNED = "ASSIGNED"
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"


# ── RLE 編解碼 ──────────────────────────────────────────────
def encode_row(row: list[int]) -> str:
    """單列轉 RLE：``[1, 1, 2]`` → ``"1:2,2:1"``。"""
    parts: list[str] = []
    prev: int | None = None
    count = 0
    for value in row:
        if prev is not None and value == prev:
            count += 1
            continue
        if prev is not None:
            parts.append(f"{prev}:{count}")
        prev, count = value, 1
    if prev is not None:
        parts.append(f"{prev}:{count}")
    return ",".join(parts)


def decode_row(spec: str) -> list[int]:
    """RLE 單列還原成整數列。"""
    out: list[int] = []
    if not spec:
        return out
    for chunk in spec.split(","):
        bin_text, sep, count_text = chunk.partition(":")
        if not sep:
            raise ValidationError(f"RLE 格式錯誤：{chunk}（應為 bin:count）")
        try:
            out.extend([int(bin_text)] * int(count_text))
        except ValueError:
            raise ValidationError(f"RLE 格式錯誤：{chunk}（bin 與 count 必須為整數）")
    return out


def encode_grid(grid: list[list[int]]) -> list[str]:
    return [encode_row(row) for row in grid]


def decode_grid(rle: list[str]) -> list[list[int]]:
    return [decode_row(spec) for spec in rle]


def parse_text_map(text: str) -> list[list[int]]:
    """解析每行一列、以空白分隔的 ASCII map（晶圓廠常見的交換格式）。"""
    grid: list[list[int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            grid.append([int(token) for token in stripped.split()])
        except ValueError:
            raise ValidationError(f"第 {lineno} 行含有非整數的 Bin 代碼：{stripped}")
    if not grid:
        raise ValidationError("文字 map 內容為空")
    return grid


def _normalise(grid: list[list[int]]) -> list[list[int]]:
    """檢查並補齊成矩形（列長不一致直接擋掉，避免座標錯位）。"""
    if not grid or not grid[0]:
        raise ValidationError("晶圓 Map 不可為空")
    widths = {len(row) for row in grid}
    if len(widths) != 1:
        raise ValidationError(f"每一列的長度必須相同，目前有 {sorted(widths)} 三種以上長度")
    return grid


# ── 統計與分析 ──────────────────────────────────────────────
def bin_statistics(grid: list[list[int]], null_bin: int, pass_bins: list[int]) -> dict:
    counter: Counter[int] = Counter()
    for row in grid:
        counter.update(row)
    counter.pop(null_bin, None)
    die_count = sum(counter.values())
    pass_set = set(pass_bins)
    pass_count = sum(n for b, n in counter.items() if b in pass_set)
    return {
        "die_count": die_count,
        "pass_count": pass_count,
        "fail_count": die_count - pass_count,
        "yield": round(pass_count / die_count, 6) if die_count else 0.0,
        "bin_counts": {str(b): n for b, n in sorted(counter.items())},
    }


def _edge_rings(grid: list[list[int]], null_bin: int) -> list[list[int]]:
    """算出每顆晶粒距離晶圓邊界的環數（0 = 最外圈）。

    以「所有非晶粒位置與格線外側」為起點做廣度優先搜尋，
    因此圓形晶圓的邊緣會被正確識別，而不是只看矩形的四個邊。
    """
    rows, cols = len(grid), len(grid[0])
    ring = [[-1] * cols for _ in range(rows)]
    queue: deque[tuple[int, int]] = deque()

    for y in range(rows):
        for x in range(cols):
            if grid[y][x] == null_bin:
                continue
            on_border = y in (0, rows - 1) or x in (0, cols - 1)
            touches_void = any(
                grid[y + dy][x + dx] == null_bin
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1))
                if 0 <= y + dy < rows and 0 <= x + dx < cols
            )
            if on_border or touches_void:
                ring[y][x] = 0
                queue.append((y, x))

    while queue:
        y, x = queue.popleft()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = y + dy, x + dx
            if not (0 <= ny < rows and 0 <= nx < cols):
                continue
            if grid[ny][nx] == null_bin or ring[ny][nx] >= 0:
                continue
            ring[ny][nx] = ring[y][x] + 1
            queue.append((ny, nx))
    return ring


def edge_analysis(
    grid: list[list[int]], null_bin: int, pass_bins: list[int], rings: int = DEFAULT_EDGE_RINGS
) -> dict:
    """邊緣 vs 中心良率 —— 差距過大通常是晶圓廠的邊緣製程問題。"""
    ring = _edge_rings(grid, null_bin)
    pass_set = set(pass_bins)
    edge_die = edge_pass = center_die = center_pass = 0
    for y, row in enumerate(grid):
        for x, value in enumerate(row):
            if value == null_bin:
                continue
            is_pass = value in pass_set
            if 0 <= ring[y][x] < rings:
                edge_die += 1
                edge_pass += is_pass
            else:
                center_die += 1
                center_pass += is_pass

    edge_yield = round(edge_pass / edge_die, 6) if edge_die else 0.0
    center_yield = round(center_pass / center_die, 6) if center_die else 0.0
    return {
        "rings": rings,
        "edge_die": edge_die,
        "edge_pass": edge_pass,
        "edge_yield": edge_yield,
        "center_die": center_die,
        "center_pass": center_pass,
        "center_yield": center_yield,
        "gap": round(center_yield - edge_yield, 6),
    }


def cluster_analysis(
    grid: list[list[int]],
    null_bin: int,
    pass_bins: list[int],
    min_size: int = DEFAULT_MIN_CLUSTER,
    top_n: int = 10,
) -> dict:
    """群聚不良分析：把相連的不良晶粒歸成一群。

    大面積群聚代表製程／機台問題（刮傷、微粒、光罩缺陷），
    散落的單點則多半是隨機不良 —— 兩者的改善方向完全不同。
    """
    rows, cols = len(grid), len(grid[0])
    pass_set = set(pass_bins)
    seen = [[False] * cols for _ in range(rows)]
    clusters: list[dict] = []
    fail_total = 0
    clustered = 0

    for sy in range(rows):
        for sx in range(cols):
            value = grid[sy][sx]
            if value == null_bin or value in pass_set or seen[sy][sx]:
                continue
            queue: deque[tuple[int, int]] = deque([(sy, sx)])
            seen[sy][sx] = True
            members: list[tuple[int, int]] = []
            bins: Counter[int] = Counter()
            while queue:
                y, x = queue.popleft()
                members.append((y, x))
                bins[grid[y][x]] += 1
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if not (0 <= ny < rows and 0 <= nx < cols) or seen[ny][nx]:
                        continue
                    nvalue = grid[ny][nx]
                    if nvalue == null_bin or nvalue in pass_set:
                        continue
                    seen[ny][nx] = True
                    queue.append((ny, nx))
            fail_total += len(members)
            if len(members) < min_size:
                continue
            clustered += len(members)
            ys = [m[0] for m in members]
            xs = [m[1] for m in members]
            clusters.append(
                {
                    "size": len(members),
                    "bins": {str(b): n for b, n in sorted(bins.items())},
                    "x_min": min(xs), "x_max": max(xs),
                    "y_min": min(ys), "y_max": max(ys),
                    "center_x": round(sum(xs) / len(xs), 1),
                    "center_y": round(sum(ys) / len(ys), 1),
                }
            )

    clusters.sort(key=lambda c: c["size"], reverse=True)
    return {
        "min_size": min_size,
        "fail_count": fail_total,
        "cluster_count": len(clusters),
        "clustered_die": clustered,
        "clustered_ratio": round(clustered / fail_total, 6) if fail_total else 0.0,
        "largest_cluster": clusters[0]["size"] if clusters else 0,
        "clusters": clusters[:top_n],
    }


def analyse_grid(
    grid: list[list[int]],
    null_bin: int,
    pass_bins: list[int],
    rings: int = DEFAULT_EDGE_RINGS,
    min_cluster: int = DEFAULT_MIN_CLUSTER,
) -> dict:
    stats = bin_statistics(grid, null_bin, pass_bins)
    edge = edge_analysis(grid, null_bin, pass_bins, rings)
    clusters = cluster_analysis(grid, null_bin, pass_bins, min_cluster)

    pass_set = set(pass_bins)
    pareto = sorted(
        (
            {"bin": b, "qty": n, "ratio": round(n / stats["die_count"], 6) if stats["die_count"] else 0.0}
            for b, n in ((int(k), v) for k, v in stats["bin_counts"].items())
            if b not in pass_set
        ),
        key=lambda item: item["qty"],
        reverse=True,
    )

    findings: list[str] = []
    if edge["gap"] >= EDGE_GAP_ALERT and edge["edge_die"]:
        findings.append(
            f"邊緣良率 {edge['edge_yield']:.1%} 較中心 {edge['center_yield']:.1%} "
            f"低 {edge['gap']:.1%}，建議與晶圓廠確認邊緣製程"
        )
    if clusters["largest_cluster"] >= min_cluster:
        findings.append(
            f"存在 {clusters['cluster_count']} 個群聚不良，最大一群 {clusters['largest_cluster']} 顆"
            f"（占全部不良 {clusters['clustered_ratio']:.1%}）"
        )
    if pareto and stats["fail_count"]:
        top = pareto[0]
        findings.append(
            f"主要不良為 Bin {top['bin']}，{top['qty']} 顆"
            f"（占不良 {top['qty'] / stats['fail_count']:.1%}）"
        )
    if stats["fail_count"] and clusters["cluster_count"] == 0 and edge["gap"] < EDGE_GAP_ALERT:
        # 沒有群聚也沒有邊緣偏移 —— 改善方向要往製程能力而不是機台單點
        findings.append("不良未形成群聚、邊緣與中心良率相近，分布接近隨機")
    if not findings:
        findings.append("全數為良品，未偵測到任何不良")

    return {**stats, "edge": edge, "clusters": clusters, "bin_pareto": pareto, "findings": findings}


# ── Map 上傳與查詢 ──────────────────────────────────────────
async def upload_map(db, payload: dict, actor: str) -> dict:
    wafer_id = payload["wafer_id"]
    null_bin = int(payload.get("null_bin", -1))
    pass_bins = [int(b) for b in (payload.get("pass_bins") or [1])]

    if payload.get("grid"):
        grid = _normalise([[int(v) for v in row] for row in payload["grid"]])
    elif payload.get("text"):
        grid = _normalise(parse_text_map(payload["text"]))
    else:
        grid = _normalise(decode_grid(list(payload["rle"])))

    async with db.transaction():
        wafer = await fetch_one(
            db, f"SELECT * FROM {T_WAFERS} WHERE wafer_id = $1 FOR UPDATE", wafer_id
        )
        if wafer is None:
            raise NotFoundError(f"找不到晶圓：{wafer_id}")

        stats = bin_statistics(grid, null_bin, pass_bins)
        if stats["die_count"] == 0:
            raise ValidationError("晶圓 Map 內沒有任何有效晶粒（全部等於 null_bin）")
        if stats["pass_count"] > int(wafer["gross_die"]):
            raise ValidationError(
                f"Map 良品數 {stats['pass_count']} 超過晶圓理論總粒數 {wafer['gross_die']}，請確認 pass_bins"
            )
        if await _assigned_count(db, wafer_id):
            raise StateError(f"晶圓 {wafer_id} 已有晶粒綁定到批號，不可覆蓋 Map")

        now = utcnow()
        doc = await fetch_one(
            db,
            f"""
            INSERT INTO {T_WAFER_MAPS}
                (wafer_id, source, rows, cols, origin, notch, null_bin, pass_bins,
                 die_count, pass_count, bin_counts, rle, remark, uploaded_by, uploaded_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (wafer_id) DO UPDATE SET
                source = EXCLUDED.source, rows = EXCLUDED.rows, cols = EXCLUDED.cols,
                origin = EXCLUDED.origin, notch = EXCLUDED.notch, null_bin = EXCLUDED.null_bin,
                pass_bins = EXCLUDED.pass_bins, die_count = EXCLUDED.die_count,
                pass_count = EXCLUDED.pass_count, bin_counts = EXCLUDED.bin_counts,
                rle = EXCLUDED.rle, remark = EXCLUDED.remark,
                uploaded_by = EXCLUDED.uploaded_by, uploaded_at = EXCLUDED.uploaded_at
            RETURNING *
            """,
            wafer_id, payload.get("source", "CP"), len(grid), len(grid[0]),
            payload.get("origin", "UPPER_LEFT"), payload.get("notch", "DOWN"),
            null_bin, pass_bins, stats["die_count"], stats["pass_count"],
            stats["bin_counts"], encode_grid(grid), payload.get("remark", ""), actor, now,
        )

        if payload.get("update_wafer", True):
            # CP map 是良品數的第一手來源，順手把晶圓主檔對齊，避免兩邊各說各話
            await db.execute(
                f"""
                UPDATE {T_WAFERS}
                SET cp_good_die = $1, cp_yield = $2, updated_at = $3, updated_by = $4
                WHERE wafer_id = $5
                """,
                stats["pass_count"],
                round(stats["pass_count"] / stats["die_count"], 6),
                now, actor, wafer_id,
            )
        await audit_service.record_change(
            db, actor, "UPLOAD", T_WAFER_MAPS, wafer_id,
            {"source": payload.get("source", "CP"), "die_count": stats["die_count"],
             "pass_count": stats["pass_count"]},
        )
        return {**doc, "yield": stats["yield"]}


async def get_map(db, wafer_id: str) -> dict:
    doc = await fetch_one(db, f"SELECT * FROM {T_WAFER_MAPS} WHERE wafer_id = $1", wafer_id)
    if doc is None:
        raise NotFoundError(f"晶圓 {wafer_id} 尚未上傳 Map")
    return doc


async def map_detail(db, wafer_id: str) -> dict:
    doc = await get_map(db, wafer_id)
    wafer = await fetch_one(db, f"SELECT * FROM {T_WAFERS} WHERE wafer_id = $1", wafer_id)
    assigned = await _assigned_count(db, wafer_id)
    return {
        **{k: v for k, v in doc.items() if k != "rle"},
        "yield": round(doc["pass_count"] / doc["die_count"], 6) if doc["die_count"] else 0.0,
        "wafer": wafer,
        "assigned_die": assigned,
        "available_die": max(0, int(doc["pass_count"]) - assigned),
    }


def downsample(grid: list[list[int]], null_bin: int, pass_bins: list[int], max_size: int) -> dict:
    """把 map 縮到看板畫得動的大小。

    每個縮放後的格子取「該區塊內最差的結果」：只要區塊裡有不良就顯示不良，
    看板的用途是快速看出不良分布，不能把不良平均掉。
    """
    rows, cols = len(grid), len(grid[0])
    step = max(1, -(-max(rows, cols) // max_size))  # 無條件進位
    if step == 1:
        return {"grid": grid, "step": 1, "rows": rows, "cols": cols}

    pass_set = set(pass_bins)
    out: list[list[int]] = []
    for y0 in range(0, rows, step):
        line: list[int] = []
        for x0 in range(0, cols, step):
            block = [
                grid[y][x]
                for y in range(y0, min(y0 + step, rows))
                for x in range(x0, min(x0 + step, cols))
            ]
            real = [v for v in block if v != null_bin]
            if not real:
                line.append(null_bin)
            else:
                fails = [v for v in real if v not in pass_set]
                line.append(fails[0] if fails else real[0])
        out.append(line)
    return {"grid": out, "step": step, "rows": len(out), "cols": len(out[0]) if out else 0}


async def map_grid(db, wafer_id: str, max_size: int = 0) -> dict:
    """取回可繪製的二維 Bin 陣列。"""
    doc = await get_map(db, wafer_id)
    grid = decode_grid(list(doc["rle"]))
    result: dict[str, Any] = {
        "wafer_id": wafer_id,
        "rows": doc["rows"],
        "cols": doc["cols"],
        "null_bin": doc["null_bin"],
        "pass_bins": list(doc["pass_bins"]),
        "origin": doc["origin"],
        "notch": doc["notch"],
        "source": doc["source"],
    }
    if max_size and max(doc["rows"], doc["cols"]) > max_size:
        reduced = downsample(grid, doc["null_bin"], list(doc["pass_bins"]), max_size)
        return {**result, "grid": reduced["grid"], "display_rows": reduced["rows"],
                "display_cols": reduced["cols"], "downsample_step": reduced["step"]}
    return {**result, "grid": grid, "display_rows": doc["rows"],
            "display_cols": doc["cols"], "downsample_step": 1}


async def analyse(
    db, wafer_id: str, rings: int = DEFAULT_EDGE_RINGS, min_cluster: int = DEFAULT_MIN_CLUSTER
) -> dict:
    doc = await get_map(db, wafer_id)
    grid = decode_grid(list(doc["rle"]))
    analysis = analyse_grid(grid, doc["null_bin"], list(doc["pass_bins"]), rings, min_cluster)
    return {"wafer_id": wafer_id, "source": doc["source"], "uploaded_at": doc["uploaded_at"], **analysis}


async def list_maps(db, wafer_lot_id: str | None = None, device_id: str | None = None,
                    limit: int = 100) -> list[dict]:
    return await fetch_all(
        db,
        f"""
        SELECT m.wafer_id, m.source, m.rows, m.cols, m.die_count, m.pass_count,
               m.bin_counts, m.uploaded_at, m.uploaded_by,
               w.wafer_lot_id, w.device_id, w.gross_die, w.assembly_lot_id,
               CASE WHEN m.die_count > 0
                    THEN round(m.pass_count::numeric / m.die_count, 6) END AS yield
        FROM {T_WAFER_MAPS} m
        JOIN {T_WAFERS} w ON w.wafer_id = m.wafer_id
        WHERE ($1::text IS NULL OR w.wafer_lot_id = $1)
          AND ($2::text IS NULL OR w.device_id = $2)
        ORDER BY m.wafer_id
        LIMIT $3
        """,
        wafer_lot_id, device_id, limit,
    )


async def lot_map_summary(db, lot_id: str) -> dict:
    """整批投入晶圓的 Map 總覽 —— 開批後第一眼要看的東西。"""
    lot = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1", lot_id)
    if lot is None:
        raise NotFoundError(f"找不到批號：{lot_id}")
    wafer_ids = list(lot["wafer_ids"] or [])
    maps = await fetch_all(
        db,
        f"SELECT * FROM {T_WAFER_MAPS} WHERE wafer_id = ANY($1::text[]) ORDER BY wafer_id",
        wafer_ids,
    ) if wafer_ids else []

    combined: Counter[str] = Counter()
    for doc in maps:
        combined.update({k: int(v) for k, v in (doc["bin_counts"] or {}).items()})
    die_total = sum(int(m["die_count"]) for m in maps)
    pass_total = sum(int(m["pass_count"]) for m in maps)
    return {
        "lot_id": lot_id,
        "wafer_count": len(wafer_ids),
        "map_count": len(maps),
        "missing_maps": sorted(set(wafer_ids) - {m["wafer_id"] for m in maps}),
        "die_count": die_total,
        "pass_count": pass_total,
        "yield": round(pass_total / die_total, 6) if die_total else 0.0,
        "bin_counts": {k: combined[k] for k in sorted(combined, key=lambda b: int(b))},
        "wafers": [
            {
                "wafer_id": m["wafer_id"],
                "die_count": m["die_count"],
                "pass_count": m["pass_count"],
                "yield": round(m["pass_count"] / m["die_count"], 6) if m["die_count"] else 0.0,
            }
            for m in maps
        ],
    }


async def delete_map(db, wafer_id: str, actor: str) -> dict:
    async with db.transaction():
        await get_map(db, wafer_id)
        if await _assigned_count(db, wafer_id):
            raise StateError(f"晶圓 {wafer_id} 已有晶粒綁定到批號，不可刪除 Map")
        await db.execute(f"DELETE FROM {T_WAFER_MAPS} WHERE wafer_id = $1", wafer_id)
        await audit_service.record_change(db, actor, "DELETE", T_WAFER_MAPS, wafer_id, None)
    return {"wafer_id": wafer_id, "deleted": True}


# ── die → unit 綁定 ─────────────────────────────────────────
async def _assigned_count(db, wafer_id: str) -> int:
    return int(
        await db.fetchval(f"SELECT count(*) FROM {T_DIE_ASSIGNMENTS} WHERE wafer_id = $1", wafer_id)
        or 0
    )


async def assign_dies(db, payload: dict, actor: str) -> dict:
    """把晶圓上的良品晶粒依序綁定成批號的成品序號。

    黏晶（Die Attach）是晶粒失去座標的那一刻 —— 綁定必須在這裡做完，
    之後任何一顆成品都能回推到原本的晶圓座標。
    """
    lot_id = payload["lot_id"]
    async with db.transaction():
        lot = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1 FOR UPDATE", lot_id)
        if lot is None:
            raise NotFoundError(f"找不到批號：{lot_id}")
        if lot["status"] not in ACTIVE_STATUS_VALUES:
            raise StateError(f"批號 {lot_id} 狀態為 {lot['status']}，不可綁定晶粒")

        lot_wafers = list(lot["wafer_ids"] or [])
        if not lot_wafers:
            raise ValidationError(f"批號 {lot_id} 沒有記錄投入晶圓，無法綁定晶粒")
        wafer_ids = [w for w in (payload.get("wafer_ids") or lot_wafers)]
        outside = sorted(set(wafer_ids) - set(lot_wafers))
        if outside:
            raise ValidationError(f"以下晶圓不屬於批號 {lot_id}：{', '.join(outside)}")

        qty = int(payload.get("qty") or lot["qty"])
        if qty <= 0:
            raise ValidationError("綁定數量必須大於 0")

        maps = await fetch_all(
            db,
            f"SELECT * FROM {T_WAFER_MAPS} WHERE wafer_id = ANY($1::text[]) ORDER BY wafer_id FOR UPDATE",
            wafer_ids,
        )
        missing = sorted(set(wafer_ids) - {m["wafer_id"] for m in maps})
        if missing:
            raise ValidationError(f"以下晶圓尚未上傳 Map，無法綁定：{', '.join(missing)}")

        taken = await fetch_all(
            db,
            f"SELECT wafer_id, die_x, die_y FROM {T_DIE_ASSIGNMENTS} WHERE wafer_id = ANY($1::text[])",
            wafer_ids,
        )
        used: dict[str, set[tuple[int, int]]] = {}
        for row in taken:
            used.setdefault(row["wafer_id"], set()).add((row["die_x"], row["die_y"]))

        next_seq = int(
            await db.fetchval(
                f"SELECT coalesce(max(unit_seq), 0) FROM {T_DIE_ASSIGNMENTS} WHERE lot_id = $1", lot_id
            )
            or 0
        ) + 1

        rows: list[tuple] = []
        now = utcnow()
        seq = next_seq
        for doc in maps:
            if len(rows) >= qty:
                break
            grid = decode_grid(list(doc["rle"]))
            pass_set = set(doc["pass_bins"])
            occupied = used.get(doc["wafer_id"], set())
            for y, line in enumerate(grid):
                if len(rows) >= qty:
                    break
                for x, value in enumerate(line):
                    if value not in pass_set or (x, y) in occupied:
                        continue
                    rows.append((lot_id, seq, doc["wafer_id"], x, y, value, STATUS_ASSIGNED, actor, now))
                    seq += 1
                    if len(rows) >= qty:
                        break

        if len(rows) < qty:
            raise ValidationError(
                f"可用良品晶粒僅 {len(rows)} 顆，不足所需的 {qty} 顆"
                f"（晶圓：{', '.join(wafer_ids)}）"
            )

        try:
            await db.executemany(
                f"""
                INSERT INTO {T_DIE_ASSIGNMENTS}
                    (lot_id, unit_seq, wafer_id, die_x, die_y, cp_bin, status, assigned_by, assigned_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                rows,
            )
        except Exception as exc:  # 併發綁定同一片晶圓時由唯一索引擋下
            if "die_assignments" in str(exc):
                raise DuplicateError("晶粒已被其他批號綁定，請重新整理後再試")
            raise

        await audit_service.record_change(
            db, actor, "ASSIGN", T_DIE_ASSIGNMENTS, lot_id,
            {"qty": qty, "wafer_ids": wafer_ids, "from_unit_seq": next_seq},
        )
        by_wafer = Counter(r[2] for r in rows)
        return {
            "lot_id": lot_id,
            "assigned": len(rows),
            "unit_seq_from": next_seq,
            "unit_seq_to": seq - 1,
            "by_wafer": [{"wafer_id": w, "qty": by_wafer[w]} for w in sorted(by_wafer)],
        }


async def lot_dies(db, lot_id: str, skip: int = 0, limit: int = 200,
                   status: str | None = None) -> dict:
    where = "WHERE lot_id = $1 AND ($2::text IS NULL OR status = $2)"
    total = await db.fetchval(f"SELECT count(*) FROM {T_DIE_ASSIGNMENTS} {where}", lot_id, status)
    items = await fetch_all(
        db,
        f"SELECT * FROM {T_DIE_ASSIGNMENTS} {where} ORDER BY unit_seq OFFSET $3 LIMIT $4",
        lot_id, status, skip, limit,
    )
    return {"items": items, "total": total, "skip": skip, "limit": limit}


async def die_summary(db, lot_id: str) -> dict:
    """批號的晶粒綁定總覽：來自哪幾片晶圓、測試結果分布。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT wafer_id, status, count(*) AS qty
        FROM {T_DIE_ASSIGNMENTS} WHERE lot_id = $1
        GROUP BY wafer_id, status ORDER BY wafer_id, status
        """,
        lot_id,
    )
    ft = await fetch_all(
        db,
        f"""
        SELECT ft_bin, count(*) AS qty FROM {T_DIE_ASSIGNMENTS}
        WHERE lot_id = $1 AND ft_bin IS NOT NULL GROUP BY ft_bin ORDER BY ft_bin
        """,
        lot_id,
    )
    by_wafer: dict[str, dict] = {}
    for row in rows:
        entry = by_wafer.setdefault(row["wafer_id"], {"wafer_id": row["wafer_id"], "total": 0})
        entry[row["status"]] = int(row["qty"])
        entry["total"] += int(row["qty"])
    total = sum(e["total"] for e in by_wafer.values())
    tested = sum(int(f["qty"]) for f in ft)
    passed = sum(int(r["qty"]) for r in rows if r["status"] == STATUS_PASS)
    return {
        "lot_id": lot_id,
        "assigned": total,
        "tested": tested,
        "passed": passed,
        "die_yield": round(passed / tested, 6) if tested else None,
        "by_wafer": [by_wafer[k] for k in sorted(by_wafer)],
        "ft_bins": [{"bin": f["ft_bin"], "qty": int(f["qty"])} for f in ft],
    }


async def die_of_unit(db, lot_id: str, unit_seq: int) -> dict:
    """成品序號 → 晶圓座標（客戶退回一顆不良品時的第一個動作）。"""
    row = await fetch_one(
        db,
        f"SELECT * FROM {T_DIE_ASSIGNMENTS} WHERE lot_id = $1 AND unit_seq = $2",
        lot_id, unit_seq,
    )
    if row is None:
        raise NotFoundError(f"批號 {lot_id} 沒有第 {unit_seq} 顆的晶粒綁定紀錄")
    wafer = await fetch_one(db, f"SELECT * FROM {T_WAFERS} WHERE wafer_id = $1", row["wafer_id"])
    wafer_map = await fetch_one(
        db,
        f"SELECT wafer_id, source, rows, cols, die_count, pass_count, null_bin, pass_bins, uploaded_at "
        f"FROM {T_WAFER_MAPS} WHERE wafer_id = $1",
        row["wafer_id"],
    )
    neighbours = await fetch_all(
        db,
        f"""
        SELECT lot_id, unit_seq, die_x, die_y, cp_bin, ft_bin, status
        FROM {T_DIE_ASSIGNMENTS}
        WHERE wafer_id = $1 AND abs(die_x - $2) <= 1 AND abs(die_y - $3) <= 1
          AND NOT (die_x = $2 AND die_y = $3)
        ORDER BY die_y, die_x
        """,
        row["wafer_id"], row["die_x"], row["die_y"],
    )
    return {"die": row, "wafer": wafer, "wafer_map": wafer_map, "neighbour_dies": neighbours}


async def unit_of_die(db, wafer_id: str, die_x: int, die_y: int) -> dict:
    """晶圓座標 → 成品序號（晶圓廠通知某區塊有問題時的圈選）。"""
    row = await fetch_one(
        db,
        f"SELECT * FROM {T_DIE_ASSIGNMENTS} WHERE wafer_id = $1 AND die_x = $2 AND die_y = $3",
        wafer_id, die_x, die_y,
    )
    if row is None:
        raise NotFoundError(f"晶圓 {wafer_id} 的座標 ({die_x}, {die_y}) 沒有綁定紀錄")
    lot = await fetch_one(db, f"SELECT * FROM {T_LOTS} WHERE lot_id = $1", row["lot_id"])
    return {"die": row, "lot": lot}


async def dies_in_area(db, wafer_id: str, x_min: int, x_max: int, y_min: int, y_max: int) -> dict:
    """圈選晶圓上一塊區域，列出受影響的批號與成品序號。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_DIE_ASSIGNMENTS}
        WHERE wafer_id = $1 AND die_x BETWEEN $2 AND $3 AND die_y BETWEEN $4 AND $5
        ORDER BY lot_id, unit_seq
        """,
        wafer_id, x_min, x_max, y_min, y_max,
    )
    by_lot: Counter[str] = Counter(r["lot_id"] for r in rows)
    return {
        "wafer_id": wafer_id,
        "area": {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max},
        "die_count": len(rows),
        "impacted_lots": [{"lot_id": k, "die_count": by_lot[k]} for k in sorted(by_lot)],
        "dies": rows,
    }


async def record_ft_results(db, payload: dict, actor: str) -> dict:
    """回寫成品測試 Bin 到晶粒，完成 die 級良率閉環。"""
    lot_id = payload["lot_id"]
    pass_set = {int(b) for b in (payload.get("pass_bins") or [1])}
    results = payload["results"]

    async with db.transaction():
        existing = await fetch_all(
            db, f"SELECT unit_seq FROM {T_DIE_ASSIGNMENTS} WHERE lot_id = $1 FOR UPDATE", lot_id
        )
        known = {int(r["unit_seq"]) for r in existing}
        if not known:
            raise NotFoundError(f"批號 {lot_id} 沒有晶粒綁定紀錄")
        unknown = sorted({int(r["unit_seq"]) for r in results} - known)
        if unknown:
            raise ValidationError(f"批號 {lot_id} 沒有這些成品序號：{unknown[:10]}")

        rows = [
            (int(r["ft_bin"]), STATUS_PASS if int(r["ft_bin"]) in pass_set else STATUS_FAIL,
             lot_id, int(r["unit_seq"]))
            for r in results
        ]
        await db.executemany(
            f"UPDATE {T_DIE_ASSIGNMENTS} SET ft_bin = $1, status = $2 WHERE lot_id = $3 AND unit_seq = $4",
            rows,
        )
        await audit_service.record_change(
            db, actor, "FT_RESULT", T_DIE_ASSIGNMENTS, lot_id, {"count": len(rows)},
        )
    passed = sum(1 for r in rows if r[1] == STATUS_PASS)
    return {
        "lot_id": lot_id,
        "updated": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "die_yield": round(passed / len(rows), 6) if rows else 0.0,
    }


async def wafer_ft_correlation(db, wafer_id: str) -> dict:
    """CP 與 FT 的對照 —— 找出「CP 過、FT 掛」的晶粒分布。

    OSAT 最常被問的一句話：這是封裝做壞的，還是晶圓本來就不好？
    """
    rows = await fetch_all(
        db,
        f"""
        SELECT lot_id, unit_seq, die_x, die_y, cp_bin, ft_bin, status
        FROM {T_DIE_ASSIGNMENTS}
        WHERE wafer_id = $1 AND ft_bin IS NOT NULL
        ORDER BY die_y, die_x
        """,
        wafer_id,
    )
    if not rows:
        return {"wafer_id": wafer_id, "tested": 0, "failures": [], "message": "尚無 FT 結果"}

    failures = [r for r in rows if r["status"] == STATUS_FAIL]
    fail_bins: Counter[int] = Counter(int(r["ft_bin"]) for r in failures)
    doc = await fetch_one(
        db, f"SELECT rows, cols, null_bin, pass_bins, rle FROM {T_WAFER_MAPS} WHERE wafer_id = $1",
        wafer_id,
    )
    clustered = None
    if doc is not None and failures:
        # 把 FT 不良畫回晶圓座標，再跑一次群聚分析
        grid = [[doc["null_bin"]] * doc["cols"] for _ in range(doc["rows"])]
        for r in rows:
            if 0 <= r["die_y"] < doc["rows"] and 0 <= r["die_x"] < doc["cols"]:
                grid[r["die_y"]][r["die_x"]] = 1 if r["status"] == STATUS_PASS else 0
        clustered = cluster_analysis(grid, doc["null_bin"], [1])

    return {
        "wafer_id": wafer_id,
        "tested": len(rows),
        "passed": len(rows) - len(failures),
        "failed": len(failures),
        "die_yield": round((len(rows) - len(failures)) / len(rows), 6),
        "ft_fail_bins": [{"bin": b, "qty": n} for b, n in sorted(fail_bins.items())],
        "ft_fail_clusters": clustered,
        "failures": failures[:200],
    }
