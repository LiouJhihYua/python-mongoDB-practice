"""抽樣檢驗與屬性管制圖測試。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import utcnow
from app.services import attribute_spc_service as attr
from app.services import lot_service, sampling_service
from tests.helpers import advance_to, make_lot, run_step

pytestmark = pytest.mark.asyncio

LEVELS = [
    {"lot_size_from": 1, "lot_size_to": 500, "code_letter": "H",
     "sample_size": 50, "accept_number": 1, "reject_number": 2},
    {"lot_size_from": 501, "lot_size_to": 3200, "code_letter": "K",
     "sample_size": 125, "accept_number": 3, "reject_number": 4},
    {"lot_size_from": 3201, "lot_size_to": None, "code_letter": "L",
     "sample_size": 200, "accept_number": 5, "reject_number": 6},
]


async def make_plan(db, plan_code="VI-AQL10", op_code="FT", lot_interval=1,
                    plan_type="AQL", levels=None, device_id="") -> dict:
    return await sampling_service.create_plan(
        db,
        {
            "plan_code": plan_code, "name": "外觀檢抽樣", "op_code": op_code,
            "device_id": device_id, "customer_code": "", "plan_type": plan_type,
            "lot_interval": lot_interval, "aql": 1.0, "inspection_level": "II",
            "levels": LEVELS if levels is None else levels, "remark": "", "active": True,
        },
        "qc01",
    )


# ── Z1.4 對照 ───────────────────────────────────────────────
def test_z14_code_letters():
    assert sampling_service.z14_code_letter(100)["code_letter"] == "F"
    assert sampling_service.z14_code_letter(100)["sample_size"] == 20
    assert sampling_service.z14_code_letter(1000)["code_letter"] == "J"
    assert sampling_service.z14_code_letter(1_000_000)["code_letter"] == "Q"


# ── 計畫維護 ────────────────────────────────────────────────
async def test_create_plan_with_levels(factory):
    plan = await make_plan(factory)
    assert plan["plan_code"] == "VI-AQL10"
    assert [l["sample_size"] for l in plan["levels"]] == [50, 125, 200]


async def test_plan_rejects_unknown_operation(factory):
    with pytest.raises(ValidationError, match="站別不存在"):
        await make_plan(factory, op_code="NO_SUCH_OP")


async def test_plan_rejects_overlapping_levels(factory):
    bad = [
        {"lot_size_from": 1, "lot_size_to": 500, "sample_size": 50,
         "accept_number": 1, "reject_number": 2},
        {"lot_size_from": 400, "lot_size_to": 900, "sample_size": 80,
         "accept_number": 2, "reject_number": 3},
    ]
    with pytest.raises(ValidationError, match="重疊"):
        await make_plan(factory, levels=bad)


async def test_plan_rejects_bad_accept_reject(factory):
    bad = [{"lot_size_from": 1, "lot_size_to": None, "sample_size": 50,
            "accept_number": 3, "reject_number": 2}]
    with pytest.raises(ValidationError, match="拒收數"):
        await make_plan(factory, levels=bad)


async def test_level_lookup(factory):
    plan = await make_plan(factory)
    assert sampling_service.level_for(plan["levels"], 200)["sample_size"] == 50
    assert sampling_service.level_for(plan["levels"], 1000)["sample_size"] == 125
    assert sampling_service.level_for(plan["levels"], 99_999)["sample_size"] == 200


async def test_update_plan_replaces_levels(factory):
    db = factory
    await make_plan(db)
    updated = await sampling_service.update_plan(
        db, "VI-AQL10",
        {"lot_interval": 5,
         "levels": [{"lot_size_from": 1, "lot_size_to": None, "sample_size": 32,
                     "accept_number": 0, "reject_number": 1}]},
        "qc01",
    )
    assert updated["lot_interval"] == 5 and len(updated["levels"]) == 1


async def test_delete_plan(factory):
    db = factory
    await make_plan(db)
    assert (await sampling_service.delete_plan(db, "VI-AQL10", "qc01"))["deleted"] is True
    with pytest.raises(NotFoundError):
        await sampling_service.get_plan(db, "VI-AQL10")


async def test_applicable_prefers_device_specific(factory):
    db = factory
    await make_plan(db, plan_code="FT-COMMON")
    await make_plan(db, plan_code="FT-QFN48", device_id="TEST-QFN48")
    plan = await sampling_service.applicable_plan(db, "FT", "TEST-QFN48")
    assert plan["plan_code"] == "FT-QFN48"


# ── 抽樣決策 ────────────────────────────────────────────────
async def _lot_at_ft(db, users) -> dict:
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "FT")
    return await lot_service.get_lot(db, lot["lot_id"])


async def test_track_in_creates_pending_inspection(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'FT'")
    await make_plan(db)

    lot = await _lot_at_ft(db, users)
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
    )
    sampling = result["sampling"]
    assert sampling["decision"] == "SAMPLED"
    assert sampling["sample_size"] == 125  # 批量 2000 顆落在 501~3200
    assert sampling["accept_number"] == 3 and sampling["reject_number"] == 4
    assert sampling["result"] == "PENDING"


async def test_skip_lot_rotation_is_deterministic(factory, users):
    """跳批以計數輪替，不是隨機 —— 抽檢比例必須可稽核。"""
    db = factory
    await db.execute("UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'FT'")
    await make_plan(db, lot_interval=3)

    decisions = []
    for _ in range(6):
        lot = await _lot_at_ft(db, users)
        result = await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
        )
        decisions.append(result["sampling"]["decision"])
        await run_step_out(db, users, lot["lot_id"])

    assert decisions == ["SAMPLED", "SKIPPED", "SKIPPED", "SAMPLED", "SKIPPED", "SKIPPED"]


async def run_step_out(db, users, lot_id: str) -> None:
    """把已進站的批號出站，好讓下一批可以用同一台機。"""
    lot = await lot_service.get_lot(db, lot_id)
    operation = await db.fetchrow("SELECT * FROM operations WHERE op_code = $1", lot["current_op"])
    device = await db.fetchrow("SELECT * FROM devices WHERE device_id = $1", lot["device_id"])
    expected, _ = lot_service.compute_expected_output(
        int(lot["qty"]), operation, device, lot["unit_type"]
    )
    await lot_service.track_out(
        db,
        {"lot_id": lot_id, "good_qty": expected, "reject_qty": 0, "defects": [],
         "materials": [], "bin_map": None, "remark": ""},
        users["op001"],
    )


async def test_full_inspection_plan(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'FT'")
    await make_plan(db, plan_type="FULL", levels=[])

    lot = await _lot_at_ft(db, users)
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
    )
    assert result["sampling"]["decision"] == "FULL"
    assert result["sampling"]["sample_size"] == result["sampling"]["lot_size"]


async def test_sampling_rate_used_when_no_plan(factory, users):
    """站別主檔的 sampling_rate 終於有作用了：0.5 表示每 2 批驗 1 批。"""
    db = factory
    await db.execute(
        "UPDATE operations SET require_sampling_decision = TRUE, sampling_rate = 0.5 "
        "WHERE op_code = 'FT'"
    )
    decisions = []
    for _ in range(4):
        lot = await _lot_at_ft(db, users)
        result = await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
        )
        decisions.append(result["sampling"]["decision"])
        await run_step_out(db, users, lot["lot_id"])
    assert decisions == ["FULL", "SKIPPED", "FULL", "SKIPPED"]


async def test_zero_sampling_rate_means_no_inspection(factory, users):
    db = factory
    await db.execute(
        "UPDATE operations SET require_sampling_decision = TRUE, sampling_rate = 0 "
        "WHERE op_code = 'FT'"
    )
    lot = await _lot_at_ft(db, users)
    result = await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
    )
    assert result["sampling"]["decision"] == "SKIPPED"
    assert result["sampling"]["result"] == "NOT_REQUIRED"


async def test_missing_level_is_reported(factory, users):
    db = factory
    await db.execute("UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'FT'")
    await make_plan(
        db, levels=[{"lot_size_from": 1, "lot_size_to": 10, "sample_size": 2,
                     "accept_number": 0, "reject_number": 1}],
    )
    lot = await _lot_at_ft(db, users)
    with pytest.raises(StateError, match="沒有涵蓋批量"):
        await lot_service.track_in(
            db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
        )


# ── 判定 ────────────────────────────────────────────────────
async def _pending_lot(db, users) -> str:
    await db.execute("UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'FT'")
    await make_plan(db)
    lot = await _lot_at_ft(db, users)
    await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
    )
    return lot["lot_id"]


async def test_judge_accepts_below_reject_number(factory, users):
    db = factory
    lot_id = await _pending_lot(db, users)
    result = await sampling_service.judge(db, {"lot_id": lot_id, "defect_found": 3}, "qc01")
    assert result["accepted"] is True and result["result"] == "ACCEPT"


async def test_judge_rejects_at_reject_number(factory, users):
    db = factory
    lot_id = await _pending_lot(db, users)
    result = await sampling_service.judge(db, {"lot_id": lot_id, "defect_found": 4}, "qc01")
    assert result["accepted"] is False and result["result"] == "REJECT"
    assert "拒收" in result["message"]


async def test_judge_rejects_more_than_sample(factory, users):
    db = factory
    lot_id = await _pending_lot(db, users)
    with pytest.raises(ValidationError, match="超過樣本數"):
        await sampling_service.judge(db, {"lot_id": lot_id, "defect_found": 999}, "qc01")


async def test_judge_without_pending_record(factory, users):
    db = factory
    lot = await make_lot(db, qty=2)
    with pytest.raises(NotFoundError, match="沒有待判定"):
        await sampling_service.judge(db, {"lot_id": lot["lot_id"], "defect_found": 0}, "qc01")


async def test_summary_counts(factory, users):
    db = factory
    lot_id = await _pending_lot(db, users)
    await sampling_service.judge(db, {"lot_id": lot_id, "defect_found": 0}, "qc01")
    summary = await sampling_service.summary(db, utcnow() - timedelta(hours=1), utcnow())
    entry = next(e for e in summary["by_operation"] if e["op_code"] == "FT")
    assert entry["inspected"] == 1 and entry["accepted"] == 1
    assert summary["total_rejected"] == 0


# ── 屬性管制圖 ──────────────────────────────────────────────
def test_p_chart_variable_sample_size():
    points = [{"n": 1000, "d": 10} for _ in range(9)] + [{"n": 100, "d": 1}]
    chart = attr.p_chart(points)
    assert chart["ready"] is True
    assert chart["center"] == pytest.approx(91 / 9100, abs=1e-6)
    # 樣本數小的那一點界限應該比較寬
    assert chart["points"][-1]["ucl"] > chart["points"][0]["ucl"]


def test_p_chart_flags_out_of_control():
    points = [{"n": 1000, "d": 10} for _ in range(9)] + [{"n": 1000, "d": 40}]
    chart = attr.p_chart(points)
    assert chart["points"][-1]["out_of_control"] is True
    assert chart["violations"] == 1


def test_p_chart_extreme_outlier_widens_center():
    """單一極端點會把中心線拉高，使原本正常的點掉到 LCL 之下 ——
    這是管制圖的已知特性，也是建立界限時要先排除異常點的原因。"""
    points = [{"n": 1000, "d": 10} for _ in range(9)] + [{"n": 1000, "d": 200}]
    chart = attr.p_chart(points)
    assert chart["violations"] == 10


def test_p_chart_needs_enough_points():
    chart = attr.p_chart([{"n": 100, "d": 1}] * 3)
    assert chart["ready"] is False and "資料點" in chart["message"]


def test_np_chart_requires_constant_sample_size():
    with pytest.raises(ValidationError, match="樣本數固定"):
        attr.np_chart([{"n": 100, "d": 1}] * 5 + [{"n": 200, "d": 2}] * 5)


def test_np_chart_math():
    points = [{"n": 100, "d": 5} for _ in range(10)]
    chart = attr.np_chart(points)
    assert chart["center"] == pytest.approx(5.0)
    # UCL = np̄ + 3√(np̄(1-p̄)) = 5 + 3√(5×0.95)
    assert chart["ucl"] == pytest.approx(5 + 3 * (5 * 0.95) ** 0.5, abs=1e-3)


def test_c_chart_math():
    points = [{"c": 4} for _ in range(10)]
    chart = attr.c_chart(points)
    assert chart["center"] == pytest.approx(4.0)
    assert chart["ucl"] == pytest.approx(4 + 3 * 2, abs=1e-6)  # 3√4 = 6
    assert chart["lcl"] == 0.0  # 負值被夾到 0


def test_u_chart_variable_area():
    points = [{"c": 8, "n": 4} for _ in range(9)] + [{"c": 1, "n": 1}]
    chart = attr.u_chart(points)
    assert chart["center"] == pytest.approx(73 / 37, abs=1e-6)
    assert chart["points"][-1]["ucl"] > chart["points"][0]["ucl"]


def test_ewma_detects_small_sustained_shift():
    """單點都沒超出 3σ，但持續偏移會被 EWMA 抓到。"""
    baseline = [10.0, 10.2, 9.8, 10.1, 9.9, 10.0, 10.1, 9.9, 10.0, 10.1]
    drift = [10.6] * 12
    chart = attr.ewma_chart(baseline + drift, center=10.0, sigma=0.15)
    assert chart["ready"] is True
    assert chart["violations"] > 0
    assert chart["points"][-1]["value"] > chart["points"][0]["value"]


def test_ewma_stable_process_has_no_violation():
    values = [10.0, 10.1, 9.9, 10.05, 9.95, 10.02, 9.98, 10.03, 9.97, 10.0]
    chart = attr.ewma_chart(values, center=10.0, sigma=0.15)
    assert chart["violations"] == 0


def test_ewma_rejects_bad_lambda():
    with pytest.raises(ValidationError, match="λ"):
        attr.ewma_chart([1.0] * 10, lam=0)


async def test_attribute_chart_from_production_data(factory, users):
    db = factory
    for _ in range(9):
        lot = await make_lot(db, qty=1)
        await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
        await run_step(db, users, lot["lot_id"], "WB-01", reject=10, defect_code="WB-NSOP")

    chart = await attr.build_chart(
        db, "p", "WIRE_BOND", utcnow() - timedelta(hours=2), utcnow() + timedelta(minutes=1)
    )
    assert chart["ready"] is True and len(chart["points"]) == 9
    assert chart["center"] == pytest.approx(10 / 1000, abs=1e-6)


async def test_attribute_chart_rejects_unknown_type(factory):
    with pytest.raises(ValidationError, match="不支援的管制圖類型"):
        await attr.build_chart(factory, "xbar", "FT", utcnow() - timedelta(hours=1), utcnow())


# ── API ─────────────────────────────────────────────────────
async def test_sampling_api_flow(client, token, factory, users):
    db = factory
    qc = await token("qc01")
    res = await client.post(
        "/api/sampling/plans",
        json={"plan_code": "API-PLAN", "name": "測試計畫", "op_code": "FT",
              "plan_type": "AQL", "lot_interval": 1, "aql": 1.0, "levels": LEVELS},
        headers=qc,
    )
    assert res.status_code == 201, res.text

    lookup = await client.get("/api/sampling/plans/API-PLAN/lookup?lot_size=1000", headers=qc)
    assert lookup.json()["level"]["sample_size"] == 125
    assert lookup.json()["z14_suggestion"]["code_letter"] == "J"

    await db.execute("UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'FT'")
    lot = await _lot_at_ft(db, users)
    await lot_service.track_in(
        db, {"lot_id": lot["lot_id"], "eq_id": "FT-01", "remark": ""}, users["op001"]
    )

    pending = await client.get("/api/sampling/inspections/pending", headers=qc)
    assert len(pending.json()) == 1

    judged = await client.post(
        "/api/sampling/inspections/judge",
        json={"lot_id": lot["lot_id"], "defect_found": 1}, headers=qc,
    )
    assert judged.json()["result"] == "ACCEPT"


async def test_attribute_chart_api(client, token, factory, users):
    db = factory
    for _ in range(9):
        lot = await make_lot(db, qty=1)
        await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
        await run_step(db, users, lot["lot_id"], "WB-01", reject=5, defect_code="WB-NSOP")

    headers = await token("eng01")
    res = await client.get("/api/spc/attribute/WIRE_BOND?chart=p&hours=2", headers=headers)
    assert res.status_code == 200
    assert res.json()["chart"] == "p" and res.json()["ready"] is True

    overview = await client.get("/api/spc/attribute/overview?hours=2", headers=headers)
    assert any(r["op_code"] == "WIRE_BOND" for r in overview.json())
