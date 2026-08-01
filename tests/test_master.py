"""主檔的參照完整性與版本管理。"""

import pytest
from pydantic import ValidationError as PydanticError

from app.errors import DuplicateError, NotFoundError, ValidationError
from app.models.master import OperationIn, RouteIn
from app.services import master_service

ACTOR = "test"


async def test_duplicate_key_rejected(factory):
    db = factory
    with pytest.raises(DuplicateError, match="已存在"):
        await master_service.customers.create(
            db, {"code": "MTK", "name": "重複", "contact": "", "email": "", "active": True}, ACTOR
        )


async def test_device_requires_existing_customer(factory):
    db = factory
    with pytest.raises(ValidationError, match="客戶不存在"):
        await master_service.create_device(
            db,
            {"device_id": "X", "description": "", "customer_code": "NOPE", "customer_device": "",
             "package_code": "QFN48", "route_code": "RT-TEST", "wafer_size_inch": 12,
             "gross_die_per_wafer": 100, "units_per_strip": 1, "units_per_reel": 1,
             "die_size_mm": "", "wire_per_unit": 0, "target_yield": 0.98, "active": True},
            ACTOR,
        )


async def test_device_requires_existing_route(factory):
    db = factory
    with pytest.raises(NotFoundError, match="找不到啟用中的流程"):
        await master_service.create_device(
            db,
            {"device_id": "X", "description": "", "customer_code": "MTK", "customer_device": "",
             "package_code": "QFN48", "route_code": "NO-ROUTE", "wafer_size_inch": 12,
             "gross_die_per_wafer": 100, "units_per_strip": 1, "units_per_reel": 1,
             "die_size_mm": "", "wire_per_unit": 0, "target_yield": 0.98, "active": True},
            ACTOR,
        )


async def test_route_requires_existing_operations(factory):
    db = factory
    with pytest.raises(ValidationError, match="站別不存在"):
        await master_service.create_route(
            db,
            {"route_code": "RT-X", "version": 1, "description": "", "package_family": "",
             "steps": [{"seq": 10, "op_code": "NO_SUCH_OP", "standard_yield": 1.0, "note": ""}],
             "active": True},
            ACTOR,
        )


async def test_equipment_capability_must_exist(factory):
    db = factory
    with pytest.raises(ValidationError, match="站別不存在"):
        await master_service.create_equipment(
            db,
            {"eq_id": "XX-01", "name": "測試機", "model": "", "vendor": "", "area": "",
             "op_codes": ["GHOST_OP"], "ideal_cycle_time_sec": 1.0, "pm_interval_days": 90,
             "active": True},
            ACTOR,
        )


def test_route_step_sequence_must_be_sorted_and_unique():
    with pytest.raises(PydanticError, match="由小到大"):
        RouteIn(route_code="R", steps=[
            {"seq": 20, "op_code": "A"}, {"seq": 10, "op_code": "B"},
        ])
    with pytest.raises(PydanticError, match="不可重複"):
        RouteIn(route_code="R", steps=[
            {"seq": 10, "op_code": "A"}, {"seq": 10, "op_code": "B"},
        ])
    with pytest.raises(PydanticError, match="至少需要一個站別"):
        RouteIn(route_code="R", steps=[])


def test_test_operation_requires_pass_bins():
    with pytest.raises(PydanticError, match="良品 Bin"):
        OperationIn(op_code="FT2", name="測試", is_test=True, pass_bins=[])


async def test_route_versioning_picks_latest(factory):
    db = factory
    await master_service.create_route(
        db,
        {"route_code": "RT-TEST", "version": 2, "description": "改版", "package_family": "QFN",
         "steps": [{"seq": 10, "op_code": "WFR_RCV", "standard_yield": 1.0, "note": ""},
                   {"seq": 20, "op_code": "FT", "standard_yield": 1.0, "note": ""}],
         "active": True},
        ACTOR,
    )
    latest = await master_service.get_active_route(db, "RT-TEST")
    assert latest["version"] == 2
    pinned = await master_service.get_active_route(db, "RT-TEST", version=1)
    assert pinned["version"] == 1
    assert len(pinned["steps"]) == 4


async def test_inactive_route_not_selected(factory):
    db = factory
    await db["routes"].update_one({"route_code": "RT-TEST", "version": 1}, {"$set": {"active": False}})
    with pytest.raises(NotFoundError):
        await master_service.get_active_route(db, "RT-TEST")


async def test_deactivate_is_soft_delete(factory):
    db = factory
    await master_service.operations.deactivate(db, "FT", ACTOR)
    doc = await master_service.operations.get(db, "FT")
    assert doc["active"] is False  # 資料仍在，追溯不受影響


def test_route_step_helpers():
    route = {"route_code": "R", "steps": [
        {"seq": 10, "op_code": "A"}, {"seq": 20, "op_code": "B"}, {"seq": 30, "op_code": "C"},
    ]}
    assert master_service.first_step(route)["op_code"] == "A"
    assert master_service.next_step(route, 10)["op_code"] == "B"
    assert master_service.next_step(route, 30) is None


async def test_wafer_cp_yield_auto_computed(factory):
    """CP 良率的推算與檢核在模型與服務層都要成立（腳本會直接呼叫服務層）。"""
    db = factory
    from app.models.master import WaferIn

    wafer = WaferIn(wafer_id="W1", wafer_lot_id="WL1", device_id="TEST-QFN48",
                    gross_die=1000, cp_good_die=950)
    assert wafer.cp_yield == pytest.approx(0.95)

    with pytest.raises(PydanticError, match="不可大於總晶粒數"):
        WaferIn(wafer_id="W2", wafer_lot_id="WL1", device_id="TEST-QFN48",
                gross_die=100, cp_good_die=200)

    created = await master_service.create_wafer(
        db,
        {"wafer_id": "W9", "wafer_lot_id": "WL9", "device_id": "TEST-QFN48", "fab": "TSMC",
         "gross_die": 1000, "cp_good_die": 940, "cp_yield": None},
        ACTOR,
    )
    assert created["cp_yield"] == pytest.approx(0.94)
    assert created["consumed"] is False

    with pytest.raises(ValidationError, match="不可大於總晶粒數"):
        await master_service.create_wafer(
            db,
            {"wafer_id": "W10", "wafer_lot_id": "WL9", "device_id": "TEST-QFN48", "fab": "",
             "gross_die": 100, "cp_good_die": 200, "cp_yield": None},
            ACTOR,
        )
