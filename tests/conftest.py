"""測試共用設定：一律使用記憶體資料庫，每個測試取得乾淨的一份。"""

import os

# 必須早於 app.config 匯入
os.environ["MES_DB_BACKEND"] = "memory"
os.environ["MES_JWT_SECRET"] = "test-secret"
os.environ["MES_TZ_OFFSET_HOURS"] = "8"

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app import database  # noqa: E402
from app.models import base as base_models  # noqa: E402
from app.models.enums import (  # noqa: E402
    DefectCategory,
    DispositionType,
    EquipmentState,
    OperationType,
    Role,
    UnitTransform,
    UnitType,
)
from app.services import equipment_service, master_service, user_service  # noqa: E402

ACTOR = "test"

#: 精簡版 OSAT 流程：進料 → 切割（片→顆）→ 打線 → 測試
OPERATIONS = [
    dict(op_code="WFR_RCV", name="晶圓進料", op_type=OperationType.WAFER.value,
         unit_transform=UnitTransform.NONE.value, output_unit=UnitType.WAFER.value,
         requires_equipment=False, requires_certification=False,
         standard_cycle_time_sec=600, max_queue_minutes=0, is_test=False,
         pass_bins=[1], sampling_rate=1.0, allow_rework=True, active=True, area=""),
    dict(op_code="WFR_SAW", name="晶圓切割", op_type=OperationType.WAFER.value,
         unit_transform=UnitTransform.WAFER_TO_DIE.value, output_unit=UnitType.DIE.value,
         requires_equipment=True, requires_certification=True,
         standard_cycle_time_sec=1800, max_queue_minutes=0, is_test=False,
         pass_bins=[1], sampling_rate=1.0, allow_rework=True, active=True, area=""),
    dict(op_code="WIRE_BOND", name="打線接合", op_type=OperationType.ASSEMBLY.value,
         unit_transform=UnitTransform.NONE.value, output_unit=None,
         requires_equipment=True, requires_certification=True,
         standard_cycle_time_sec=3600, max_queue_minutes=60, is_test=False,
         pass_bins=[1], sampling_rate=1.0, allow_rework=True, active=True, area=""),
    dict(op_code="FT", name="最終測試", op_type=OperationType.TEST.value,
         unit_transform=UnitTransform.NONE.value, output_unit=None,
         requires_equipment=True, requires_certification=False,
         standard_cycle_time_sec=2400, max_queue_minutes=0, is_test=True,
         pass_bins=[1], sampling_rate=1.0, allow_rework=True, active=True, area=""),
]

EQUIPMENTS = [
    dict(eq_id="DS-01", name="切割機 01", model="DISCO", vendor="DISCO", area="WAFER",
         op_codes=["WFR_SAW"], ideal_cycle_time_sec=1.0, pm_interval_days=90, active=True),
    dict(eq_id="WB-01", name="打線機 01", model="K&S", vendor="K&S", area="ASSY",
         op_codes=["WIRE_BOND"], ideal_cycle_time_sec=0.1, pm_interval_days=90, active=True),
    dict(eq_id="FT-01", name="測試機 01", model="V93000", vendor="Advantest", area="TEST",
         op_codes=["FT"], ideal_cycle_time_sec=0.1, pm_interval_days=90, active=True),
]

DEFECTS = [
    dict(code="SAW-CHIP", name="切割崩角", category=DefectCategory.VISUAL.value,
         op_codes=["WFR_SAW"], default_disposition=DispositionType.SCRAP.value, active=True),
    dict(code="WB-NSOP", name="銲線未附著", category=DefectCategory.ASSEMBLY.value,
         op_codes=["WIRE_BOND"], default_disposition=DispositionType.REWORK.value, active=True),
    dict(code="FT-OPEN", name="電性開路", category=DefectCategory.ELECTRICAL.value,
         op_codes=["FT"], default_disposition=DispositionType.SCRAP.value, active=True),
    dict(code="HND-DROP", name="搬運損傷", category=DefectCategory.HANDLING.value,
         op_codes=[], default_disposition=DispositionType.SCRAP.value, active=True),
]

USERS = [
    ("planner01", [Role.PLANNER], []),
    ("op001", [Role.OPERATOR], ["WFR_RCV", "WFR_SAW", "WIRE_BOND", "FT"]),
    ("op002", [Role.OPERATOR], ["WFR_RCV"]),  # 未取得切割與打線資格
    ("qc01", [Role.QC], []),
    ("eng01", [Role.ENGINEER], []),
    ("viewer01", [Role.VIEWER], []),
]

GROSS_DIE = 1000


@pytest_asyncio.fixture
async def db():
    """每個測試一份全新的記憶體資料庫。"""
    database._state.db = None
    handle = await database.connect_db(force=True)
    yield handle
    base_models.set_clock(None)
    database._state.db = None


@pytest_asyncio.fixture
async def factory(db):
    """建立一座迷你 OSAT 廠：使用者、主檔、設備、不良碼、產品與流程。"""
    await user_service.ensure_bootstrap_admin(db)
    for username, roles, certs in USERS:
        await user_service.create_user(
            db,
            {
                "username": username, "password": f"{username}1234", "full_name": username,
                "employee_no": username.upper(), "department": "TEST",
                "roles": [r.value for r in roles], "certifications": certs, "active": True,
            },
            ACTOR,
        )

    await master_service.customers.create(
        db, {"code": "MTK", "name": "聯發科", "contact": "", "email": "", "active": True}, ACTOR
    )
    await master_service.packages.create(
        db, {"package_code": "QFN48", "family": "QFN", "lead_count": 48,
             "body_size_mm": "7x7", "substrate_type": "LEADFRAME", "active": True}, ACTOR
    )
    for op in OPERATIONS:
        await master_service.operations.create(db, dict(op), ACTOR)
    await master_service.create_route(
        db,
        {
            "route_code": "RT-TEST", "version": 1, "description": "測試流程",
            "package_family": "QFN",
            "steps": [
                {"seq": 10, "op_code": "WFR_RCV", "standard_yield": 1.0, "note": ""},
                {"seq": 20, "op_code": "WFR_SAW", "standard_yield": 0.998, "note": ""},
                {"seq": 30, "op_code": "WIRE_BOND", "standard_yield": 0.99, "note": ""},
                {"seq": 40, "op_code": "FT", "standard_yield": 0.97, "note": ""},
            ],
            "active": True,
        },
        ACTOR,
    )
    await master_service.create_device(
        db,
        {
            "device_id": "TEST-QFN48", "description": "測試料號", "customer_code": "MTK",
            "customer_device": "TEST", "package_code": "QFN48", "route_code": "RT-TEST",
            "wafer_size_inch": 12, "gross_die_per_wafer": GROSS_DIE, "units_per_strip": 50,
            "units_per_reel": 4000, "die_size_mm": "5x5", "wire_per_unit": 48,
            "target_yield": 0.98, "active": True,
        },
        ACTOR,
    )
    for eq in EQUIPMENTS:
        await master_service.create_equipment(db, dict(eq), ACTOR)
        await equipment_service.set_state(db, eq["eq_id"], EquipmentState.STANDBY, ACTOR, "開線")
    for defect in DEFECTS:
        await master_service.create_defect_code(db, dict(defect), ACTOR)

    for wafer in range(1, 51):
        await master_service.create_wafer(
            db,
            {
                "wafer_id": f"WTEST001-{wafer:02d}", "wafer_lot_id": "WTEST001",
                "device_id": "TEST-QFN48", "fab": "TSMC", "gross_die": GROSS_DIE,
                "cp_good_die": 980, "cp_yield": None,
            },
            ACTOR,
        )
    return db


@pytest_asyncio.fixture
async def users(factory):
    """以 username 取得使用者文件，供服務層直接呼叫。"""
    rows = await factory[database.COL_USERS].find({}).to_list(length=None)
    return {u["username"]: u for u in rows}


@pytest.fixture
def client(factory):
    """已完成初始化的 API 測試用戶端（沿用 factory 建立的資料庫）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token(client):
    """回傳依帳號取 Bearer 標頭的函式。"""

    def _headers(username: str = "admin", password: str | None = None) -> dict:
        password = password or ("admin1234" if username == "admin" else f"{username}1234")
        res = client.post("/api/auth/login", json={"username": username, "password": password})
        assert res.status_code == 200, res.text
        return {"Authorization": f"Bearer {res.json()['access_token']}"}

    return _headers


class Clock:
    """測試用可推進時鐘。"""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    instance = Clock()
    base_models.set_clock(instance)
    yield instance
    base_models.set_clock(None)
