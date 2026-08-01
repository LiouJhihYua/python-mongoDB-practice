"""主檔（Master Data）模型：客戶、封裝、產品、站別、流程、設備、不良碼、材料。"""

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from app.models.base import MESModel
from app.models.enums import (
    DefectCategory,
    DispositionType,
    EquipmentState,
    MaterialType,
    OperationType,
    UnitTransform,
    UnitType,
)


class CustomerIn(MESModel):
    code: str = Field(min_length=2, max_length=16)
    name: str
    contact: str = ""
    email: str = ""
    active: bool = True


class PackageIn(MESModel):
    """封裝型式，例如 QFN48、BGA256。"""

    package_code: str
    family: str = Field(description="QFN / BGA / SOIC / LGA / DFN / QFP ...")
    lead_count: int = Field(gt=0)
    body_size_mm: str = Field(default="", description="例如 7x7x0.85")
    substrate_type: str = Field(default="", description="LEADFRAME / SUBSTRATE")
    active: bool = True


class DeviceIn(MESModel):
    """產品料號 —— OSAT 以客戶 + 封裝 + 流程定義一顆產品。"""

    device_id: str = Field(min_length=2, max_length=48)
    description: str = ""
    customer_code: str
    customer_device: str = Field(default="", description="客戶端料號")
    package_code: str
    route_code: str

    wafer_size_inch: int = Field(default=12, description="8 或 12 吋")
    gross_die_per_wafer: int = Field(default=1, gt=0, description="每片晶圓總晶粒數")
    units_per_strip: int = Field(default=1, gt=0, description="每條基板顆數")
    units_per_reel: int = Field(default=1, gt=0, description="每卷編帶顆數")
    die_size_mm: str = ""
    wire_per_unit: int = Field(default=0, ge=0, description="每顆打線根數，用於金線耗用")

    target_yield: float = Field(default=0.98, gt=0, le=1)
    active: bool = True


class OperationIn(MESModel):
    """站別（工序）定義。"""

    op_code: str = Field(min_length=2, max_length=24)
    name: str
    op_type: OperationType = OperationType.ASSEMBLY
    area: str = Field(default="", description="所屬區域／樓層")

    unit_transform: UnitTransform = UnitTransform.NONE
    output_unit: UnitType | None = Field(
        default=None, description="產出單位；留空表示沿用進站單位"
    )

    requires_equipment: bool = True
    requires_certification: bool = False
    standard_cycle_time_sec: int = Field(default=0, ge=0, description="每批標準加工秒數")
    max_queue_minutes: int = Field(
        default=0, ge=0, description="Q-Time 上限（分鐘），0 表示不管制"
    )

    is_test: bool = False
    pass_bins: list[int] = Field(default_factory=lambda: [1], description="測試良品 Bin")
    sampling_rate: float = Field(default=1.0, ge=0, le=1, description="抽檢比例，1=全檢")
    allow_rework: bool = True
    active: bool = True

    @model_validator(mode="after")
    def _check_test(self):
        if self.is_test and not self.pass_bins:
            raise ValueError("測試站必須指定至少一個良品 Bin")
        return self


class RouteStep(MESModel):
    seq: int = Field(gt=0, description="站序，10、20、30… 便於插站")
    op_code: str
    standard_yield: float = Field(default=1.0, gt=0, le=1)
    note: str = ""


class RouteIn(MESModel):
    """製程流程（Route / Flow）。"""

    route_code: str
    version: int = Field(default=1, gt=0)
    description: str = ""
    package_family: str = ""
    steps: list[RouteStep]
    active: bool = True

    @field_validator("steps")
    @classmethod
    def _check_steps(cls, steps: list[RouteStep]) -> list[RouteStep]:
        if not steps:
            raise ValueError("流程至少需要一個站別")
        seqs = [s.seq for s in steps]
        if len(set(seqs)) != len(seqs):
            raise ValueError("站序（seq）不可重複")
        if seqs != sorted(seqs):
            raise ValueError("站序必須由小到大排列")
        return steps


class EquipmentIn(MESModel):
    """設備／機台。"""

    eq_id: str = Field(min_length=2, max_length=24)
    name: str
    model: str = ""
    vendor: str = ""
    area: str = ""
    op_codes: list[str] = Field(default_factory=list, description="可執行的站別（機台能力）")
    ideal_cycle_time_sec: float = Field(
        default=1.0, gt=0, description="理論每單位加工秒數，用於 OEE 效能指標"
    )
    installed_at: datetime | None = None
    pm_interval_days: int = Field(default=90, ge=0)
    active: bool = True


class EquipmentStateIn(MESModel):
    state: EquipmentState
    reason_code: str = ""
    remark: str = ""


class DefectCodeIn(MESModel):
    """不良代碼。"""

    code: str = Field(min_length=2, max_length=24)
    name: str
    category: DefectCategory = DefectCategory.ASSEMBLY
    op_codes: list[str] = Field(default_factory=list, description="適用站別，空=全站適用")
    default_disposition: DispositionType = DispositionType.SCRAP
    active: bool = True


class MaterialIn(MESModel):
    """封裝耗材主檔。"""

    material_id: str
    name: str
    material_type: MaterialType
    spec: str = ""
    uom: str = Field(default="PCS", description="計量單位：PCS / M / G")
    on_hand_qty: float = Field(default=0, ge=0)
    safety_stock: float = Field(default=0, ge=0)
    vendor: str = ""
    active: bool = True


class MaterialReceiptIn(MESModel):
    material_id: str
    material_lot: str
    qty: float = Field(gt=0)
    remark: str = ""


class WaferIn(MESModel):
    """來料晶圓 —— 追溯的最上游。"""

    wafer_id: str
    wafer_lot_id: str = Field(description="晶圓母批（來自晶圓廠）")
    device_id: str
    fab: str = ""
    gross_die: int = Field(gt=0)
    cp_good_die: int = Field(ge=0, description="CP（針測）良品數")
    cp_yield: float | None = None
    received_at: datetime | None = None

    @model_validator(mode="after")
    def _fill_yield(self):
        if self.cp_yield is None and self.gross_die:
            self.cp_yield = round(self.cp_good_die / self.gross_die, 4)
        if self.cp_good_die > self.gross_die:
            raise ValueError("CP 良品數不可大於總晶粒數")
        return self
