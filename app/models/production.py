"""生產執行模型：工單、批號、進出站、拆併批、扣留、報廢、出貨。"""

from datetime import datetime

from pydantic import Field, model_validator

from app.models.base import MESModel
from app.models.enums import HoldReason, UnitType


# ── 工單 ────────────────────────────────────────────────────
class WorkOrderIn(MESModel):
    device_id: str
    plan_qty: int = Field(gt=0, description="計畫投入量（依 unit_type）")
    unit_type: UnitType = UnitType.WAFER
    due_date: datetime
    priority: int = Field(default=5, ge=1, le=9, description="1 最急，9 最緩")
    customer_po: str = ""
    remark: str = ""


class WorkOrderUpdate(MESModel):
    plan_qty: int | None = Field(default=None, gt=0)
    due_date: datetime | None = None
    priority: int | None = Field(default=None, ge=1, le=9)
    remark: str | None = None


# ── 批號 ────────────────────────────────────────────────────
class LotCreateIn(MESModel):
    """自工單開批。"""

    wo_no: str
    qty: int = Field(gt=0)
    wafer_ids: list[str] = Field(default_factory=list, description="投入的晶圓 ID")
    carrier_id: str = Field(default="", description="Magazine / Boat / Tray 編號")
    priority: int | None = Field(default=None, ge=1, le=9)
    remark: str = ""

    @model_validator(mode="after")
    def _check_wafers(self):
        if self.wafer_ids and len(self.wafer_ids) != self.qty:
            raise ValueError("投入晶圓數與批量不符")
        return self


class MaterialUsage(MESModel):
    material_id: str
    material_lot: str = ""
    qty: float = Field(gt=0)


class DefectItem(MESModel):
    defect_code: str
    qty: int = Field(gt=0)
    remark: str = ""


class TrackInIn(MESModel):
    lot_id: str
    eq_id: str = Field(default="", description="站別若要求設備則必填")
    remark: str = ""


class TrackOutIn(MESModel):
    lot_id: str
    good_qty: int | None = Field(default=None, ge=0, description="測試站可改用 bin_map 推算")
    reject_qty: int = Field(default=0, ge=0)
    defects: list[DefectItem] = Field(default_factory=list)
    bin_map: dict[str, int] | None = Field(
        default=None, description="測試站 Bin 分佈，例如 {\"1\": 9500, \"5\": 480}"
    )
    materials: list[MaterialUsage] = Field(default_factory=list, description="本站耗用材料")
    remark: str = ""

    @model_validator(mode="after")
    def _check_qty(self):
        if self.good_qty is None and self.bin_map is None:
            raise ValueError("需提供 good_qty 或 bin_map")
        if self.bin_map is not None:
            if not self.bin_map:
                raise ValueError("bin_map 不可為空")
            for k, v in self.bin_map.items():
                if not str(k).isdigit():
                    raise ValueError(f"Bin 代碼必須為數字：{k}")
                if v < 0:
                    raise ValueError(f"Bin {k} 數量不可為負")
        if self.defects:
            total = sum(d.qty for d in self.defects)
            if self.good_qty is not None and total != self.reject_qty:
                raise ValueError(f"不良明細合計 {total} 與不良數 {self.reject_qty} 不符")
        return self


class SplitIn(MESModel):
    """拆批：把母批拆成多個子批，母批轉為歷史批。"""

    lot_id: str
    quantities: list[int] = Field(min_length=2, description="各子批數量，合計須等於母批現有量")
    reason: str = ""

    @model_validator(mode="after")
    def _check(self):
        if any(q <= 0 for q in self.quantities):
            raise ValueError("子批數量必須大於 0")
        return self


class MergeIn(MESModel):
    """併批：相同產品／流程／站點的批號合併成新批。"""

    lot_ids: list[str] = Field(min_length=2)
    carrier_id: str = ""
    reason: str = ""


class HoldIn(MESModel):
    lot_id: str
    reason: HoldReason = HoldReason.QUALITY
    remark: str = ""


class ReleaseIn(MESModel):
    lot_id: str
    remark: str = ""


class ScrapIn(MESModel):
    lot_id: str
    qty: int = Field(gt=0)
    defect_code: str
    remark: str = ""


class ReworkIn(MESModel):
    """重工：把批號退回較前面的站序重跑。"""

    lot_id: str
    to_seq: int = Field(gt=0)
    reason: str = ""


class ShipmentIn(MESModel):
    customer_code: str
    lot_ids: list[str] = Field(min_length=1)
    customer_po: str = ""
    remark: str = ""


class LotQuery(MESModel):
    """批號查詢條件。"""

    status: str | None = None
    op_code: str | None = None
    device_id: str | None = None
    wo_no: str | None = None
    customer_code: str | None = None
    on_hold: bool | None = None
    skip: int = 0
    limit: int = Field(default=50, ge=1, le=500)
