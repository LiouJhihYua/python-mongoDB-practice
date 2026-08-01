"""晶圓 Map 與 Die 級追溯測試。"""

from __future__ import annotations

import pytest

from app.errors import NotFoundError, StateError, ValidationError
from app.services import wafermap_service as wm
from tests.helpers import make_lot

pytestmark = pytest.mark.asyncio


def circular_grid(size: int = 20, null_bin: int = -1, pass_bin: int = 1) -> list[list[int]]:
    """圓形晶圓：圓外標成 null_bin，圓內全部先當良品。"""
    radius = size / 2 - 0.5
    center = (size - 1) / 2
    return [
        [
            pass_bin if ((x - center) ** 2 + (y - center) ** 2) ** 0.5 <= radius else null_bin
            for x in range(size)
        ]
        for y in range(size)
    ]


async def upload(db, wafer_id: str = "WTEST001-01", grid=None, **kwargs) -> dict:
    if grid is None and not kwargs.get("text") and not kwargs.get("rle"):
        grid = circular_grid()
    payload = {
        "wafer_id": wafer_id,
        "source": "CP",
        "grid": grid,
        "rle": None,
        "text": None,
        "origin": "UPPER_LEFT",
        "notch": "DOWN",
        "null_bin": -1,
        "pass_bins": [1],
        "remark": "",
        "update_wafer": True,
    }
    payload.update(kwargs)
    return await wm.upload_map(db, payload, "eng01")


# ── RLE 編解碼 ──────────────────────────────────────────────
def test_rle_round_trip():
    assert wm.encode_row([1, 1, 2]) == "1:2,2:1"
    assert wm.decode_row("1:2,2:1") == [1, 1, 2]
    grid = circular_grid(24)
    assert wm.decode_grid(wm.encode_grid(grid)) == grid


def test_rle_rejects_bad_format():
    with pytest.raises(ValidationError):
        wm.decode_row("1-2")


def test_parse_text_map_skips_comments():
    grid = wm.parse_text_map("# 註解\n1 1 -1\n1 2 1\n")
    assert grid == [[1, 1, -1], [1, 2, 1]]


def test_parse_text_map_rejects_non_integer():
    with pytest.raises(ValidationError):
        wm.parse_text_map("1 1 X")


# ── 上傳與統計 ──────────────────────────────────────────────
async def test_upload_computes_statistics_and_updates_wafer(factory):
    db = factory
    grid = circular_grid()
    grid[10][10] = 4  # 種一顆不良
    doc = await upload(db, grid=grid)

    expected_die = sum(1 for row in grid for v in row if v != -1)
    assert doc["die_count"] == expected_die
    assert doc["pass_count"] == expected_die - 1
    assert doc["bin_counts"]["4"] == 1
    assert doc["rows"] == 20 and doc["cols"] == 20

    wafer = await db.fetchrow("SELECT * FROM wafers WHERE wafer_id = 'WTEST001-01'")
    assert wafer["cp_good_die"] == expected_die - 1


async def test_upload_accepts_text_and_rle_formats(factory):
    db = factory
    text_doc = await upload(db, "WTEST001-01", grid=None, text="1 1 -1\n1 2 1\n")
    assert text_doc["die_count"] == 5 and text_doc["pass_count"] == 4

    rle_doc = await upload(db, "WTEST001-02", grid=None, rle=["1:2,-1:1", "1:1,2:1,1:1"])
    assert rle_doc["cols"] == 3
    assert wm.decode_grid(list(rle_doc["rle"])) == [[1, 1, -1], [1, 2, 1]]


async def test_upload_rejects_more_pass_than_gross_die(factory):
    db = factory
    with pytest.raises(ValidationError, match="超過晶圓理論總粒數"):
        await upload(db, grid=[[1] * 40 for _ in range(40)])  # 1600 > gross_die 1000


async def test_upload_rejects_unknown_wafer(factory):
    with pytest.raises(NotFoundError):
        await upload(factory, "NO-SUCH-WAFER")


async def test_upload_overwrites_existing_map(factory):
    db = factory
    await upload(db)
    again = await upload(db, source="AOI", remark="重上傳")
    assert again["source"] == "AOI" and again["remark"] == "重上傳"
    count = await db.fetchval("SELECT count(*) FROM wafer_maps WHERE wafer_id = 'WTEST001-01'")
    assert count == 1


# ── 分析 ────────────────────────────────────────────────────
def test_cluster_analysis_separates_clusters_from_random_fails():
    grid = circular_grid(24)
    for y in range(6, 10):  # 4x4 的群聚不良
        for x in range(6, 10):
            grid[y][x] = 5
    grid[15][15] = 7  # 單點隨機不良

    result = wm.cluster_analysis(grid, -1, [1], min_size=5)
    assert result["cluster_count"] == 1
    assert result["largest_cluster"] == 16
    assert result["fail_count"] == 17  # 群聚 16 + 隨機 1
    assert result["clusters"][0]["bins"] == {"5": 16}


def test_edge_analysis_detects_edge_yield_loss():
    grid = circular_grid(24)
    ring = wm._edge_rings(grid, -1)
    for y, row in enumerate(grid):
        for x, value in enumerate(row):
            if value != -1 and ring[y][x] == 0:
                grid[y][x] = 9  # 最外圈全掛

    edge = wm.edge_analysis(grid, -1, [1], rings=2)
    assert edge["edge_yield"] < edge["center_yield"]
    assert edge["center_yield"] == 1.0
    assert edge["gap"] > 0


def test_analyse_grid_reports_findings():
    grid = circular_grid(24)
    for y in range(5, 11):
        for x in range(5, 11):
            grid[y][x] = 5
    report = wm.analyse_grid(grid, -1, [1], rings=2, min_cluster=5)
    assert report["bin_pareto"][0]["bin"] == 5
    assert any("群聚" in f for f in report["findings"])


def test_analyse_grid_reports_random_distribution():
    grid = circular_grid(24)
    grid[8][8] = 3
    report = wm.analyse_grid(grid, -1, [1])
    assert any("接近隨機" in f for f in report["findings"])


async def test_analysis_endpoint_data(factory):
    db = factory
    grid = circular_grid()
    for y in range(8, 12):
        for x in range(8, 12):
            grid[y][x] = 6
    await upload(db, grid=grid)
    result = await wm.analyse(db, "WTEST001-01")
    assert result["clusters"]["largest_cluster"] == 16
    assert result["fail_count"] == 16


def test_downsample_keeps_worst_result():
    grid = [[1] * 8 for _ in range(8)]
    grid[3][3] = 9
    reduced = wm.downsample(grid, -1, [1], max_size=4)
    assert reduced["step"] == 2
    assert reduced["rows"] == 4
    assert reduced["grid"][1][1] == 9  # 不良不會被平均掉


async def test_map_grid_downsamples(factory):
    db = factory
    await upload(db, grid=circular_grid(32))
    full = await wm.map_grid(db, "WTEST001-01", max_size=0)
    assert full["downsample_step"] == 1 and len(full["grid"]) == 32

    small = await wm.map_grid(db, "WTEST001-01", max_size=8)
    assert small["downsample_step"] == 4 and small["display_rows"] == 8


# ── die 綁定 ────────────────────────────────────────────────
async def _lot_with_maps(db, wafers: int = 2, grid_size: int = 12) -> dict:
    lot = await make_lot(db, qty=wafers)
    for wafer_id in lot["wafer_ids"]:
        await upload(db, wafer_id, grid=circular_grid(grid_size))
    return lot


async def test_assign_dies_binds_units_to_coordinates(factory):
    db = factory
    lot = await _lot_with_maps(db)
    result = await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 30, "wafer_ids": []}, "op001")

    assert result["assigned"] == 30
    assert result["unit_seq_from"] == 1 and result["unit_seq_to"] == 30
    rows = await db.fetch(
        "SELECT * FROM die_assignments WHERE lot_id = $1 ORDER BY unit_seq", lot["lot_id"]
    )
    assert [r["unit_seq"] for r in rows] == list(range(1, 31))
    assert all(r["cp_bin"] == 1 and r["status"] == "ASSIGNED" for r in rows)
    assert len({(r["wafer_id"], r["die_x"], r["die_y"]) for r in rows}) == 30


async def test_assign_dies_continues_numbering(factory):
    db = factory
    lot = await _lot_with_maps(db)
    await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 10, "wafer_ids": []}, "op001")
    second = await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 5, "wafer_ids": []}, "op001")
    assert second["unit_seq_from"] == 11 and second["unit_seq_to"] == 15


async def test_assign_dies_spans_multiple_wafers(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=2, grid_size=8)
    available = await db.fetchval("SELECT sum(pass_count) FROM wafer_maps")
    result = await wm.assign_dies(
        db, {"lot_id": lot["lot_id"], "qty": int(available), "wafer_ids": []}, "op001"
    )
    assert len(result["by_wafer"]) == 2


async def test_assign_dies_rejects_when_not_enough_good_die(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=1, grid_size=8)
    with pytest.raises(ValidationError, match="不足"):
        await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 10_000, "wafer_ids": []}, "op001")


async def test_assign_dies_requires_map(factory):
    db = factory
    lot = await make_lot(db, qty=2)
    with pytest.raises(ValidationError, match="尚未上傳 Map"):
        await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 5, "wafer_ids": []}, "op001")


async def test_assign_dies_rejects_foreign_wafer(factory):
    db = factory
    lot = await _lot_with_maps(db)
    with pytest.raises(ValidationError, match="不屬於批號"):
        await wm.assign_dies(
            db, {"lot_id": lot["lot_id"], "qty": 5, "wafer_ids": ["WTEST001-50"]}, "op001"
        )


async def test_map_locked_after_assignment(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=1)
    await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 5, "wafer_ids": []}, "op001")
    wafer_id = lot["wafer_ids"][0]
    with pytest.raises(StateError):
        await upload(db, wafer_id)
    with pytest.raises(StateError):
        await wm.delete_map(db, wafer_id, "eng01")


# ── die 級追溯 ──────────────────────────────────────────────
async def test_die_trace_both_directions(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=1)
    await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 20, "wafer_ids": []}, "op001")

    forward = await wm.die_of_unit(db, lot["lot_id"], 7)
    die = forward["die"]
    assert forward["wafer"]["wafer_id"] == die["wafer_id"]
    assert forward["wafer_map"]["source"] == "CP"

    backward = await wm.unit_of_die(db, die["wafer_id"], die["die_x"], die["die_y"])
    assert backward["die"]["unit_seq"] == 7
    assert backward["lot"]["lot_id"] == lot["lot_id"]


async def test_die_trace_missing_unit(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=1)
    with pytest.raises(NotFoundError):
        await wm.die_of_unit(db, lot["lot_id"], 999)


async def test_area_lookup_lists_impacted_lots(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=1)
    await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 20, "wafer_ids": []}, "op001")
    wafer_id = lot["wafer_ids"][0]
    area = await wm.dies_in_area(db, wafer_id, 0, 11, 0, 3)
    assert area["die_count"] > 0
    assert area["impacted_lots"][0]["lot_id"] == lot["lot_id"]


async def test_ft_results_close_the_loop(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=1)
    await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 10, "wafer_ids": []}, "op001")

    results = [{"unit_seq": i, "ft_bin": 1 if i > 2 else 5} for i in range(1, 11)]
    outcome = await wm.record_ft_results(
        db, {"lot_id": lot["lot_id"], "results": results, "pass_bins": [1]}, "op001"
    )
    assert outcome["passed"] == 8 and outcome["failed"] == 2
    assert outcome["die_yield"] == pytest.approx(0.8)

    summary = await wm.die_summary(db, lot["lot_id"])
    assert summary["tested"] == 10 and summary["passed"] == 8
    assert {b["bin"] for b in summary["ft_bins"]} == {1, 5}

    correlation = await wm.wafer_ft_correlation(db, lot["wafer_ids"][0])
    assert correlation["tested"] == 10 and correlation["failed"] == 2


async def test_ft_results_reject_unknown_unit(factory):
    db = factory
    lot = await _lot_with_maps(db, wafers=1)
    await wm.assign_dies(db, {"lot_id": lot["lot_id"], "qty": 3, "wafer_ids": []}, "op001")
    with pytest.raises(ValidationError, match="沒有這些成品序號"):
        await wm.record_ft_results(
            db, {"lot_id": lot["lot_id"], "results": [{"unit_seq": 99, "ft_bin": 1}]}, "op001"
        )


async def test_lot_map_summary_aggregates(factory):
    db = factory
    lot = await make_lot(db, qty=3)
    for wafer_id in lot["wafer_ids"][:2]:
        await upload(db, wafer_id, grid=circular_grid(12))
    summary = await wm.lot_map_summary(db, lot["lot_id"])
    assert summary["wafer_count"] == 3 and summary["map_count"] == 2
    assert summary["missing_maps"] == [lot["wafer_ids"][2]]
    assert summary["yield"] == 1.0


# ── API ─────────────────────────────────────────────────────
async def test_wafer_map_api_flow(client, token, factory):
    headers = await token("eng01")
    grid = circular_grid(16)
    grid[8][8] = 4

    res = await client.post(
        "/api/wafer/maps",
        json={"wafer_id": "WTEST001-01", "grid": grid, "pass_bins": [1]},
        headers=headers,
    )
    assert res.status_code == 201, res.text
    assert res.json()["pass_count"] > 0

    detail = await client.get("/api/wafer/maps/WTEST001-01", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["available_die"] == detail.json()["pass_count"]

    analysis = await client.get("/api/wafer/maps/WTEST001-01/analysis", headers=headers)
    assert analysis.json()["fail_count"] == 1

    listing = await client.get("/api/wafer/maps?wafer_lot_id=WTEST001", headers=headers)
    assert len(listing.json()) == 1

    grid_res = await client.get("/api/wafer/maps/WTEST001-01/grid?max_size=8", headers=headers)
    assert grid_res.json()["downsample_step"] == 2


async def test_wafer_map_api_requires_engineer(client, token, factory):
    headers = await token("op001")
    res = await client.post(
        "/api/wafer/maps", json={"wafer_id": "WTEST001-01", "grid": [[1]]}, headers=headers
    )
    assert res.status_code == 403
