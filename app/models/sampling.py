"""抽樣檢驗模型。"""

from pydantic import Field, model_validator

from app.models.base import MESModel


class SamplingLevelIn(MESModel):
    """批量級距：這個批量抽幾顆、允收幾顆、拒收幾顆。"""

    lot_size_from: int = Field(ge=0)
    lot_size_to: int | None = Field(default=None, description="留空表示沒有上限")
    code_letter: str = Field(default="", description="Z1.4 樣本大小代字，僅供對照")
    sample_size: int = Field(gt=0)
    accept_number: int = Field(ge=0)
    reject_number: int | None = Field(default=None, description="留空表示允收數 + 1")

    @model_validator(mode="after")
    def _check(self):
        if self.lot_size_to is not None and self.lot_size_to < self.lot_size_from:
            raise ValueError("批量上限不可小於下限")
        if self.reject_number is not None and self.reject_number <= self.accept_number:
            raise ValueError("拒收數必須大於允收數")
        return self


class SamplingPlanIn(MESModel):
    """抽樣計畫。各家客戶談定的計畫不同，因此級距是資料而非寫死的常數。"""

    plan_code: str = Field(min_length=2, max_length=32)
    name: str
    op_code: str
    device_id: str = Field(default="", description="留空表示全料號適用")
    customer_code: str = Field(default="", description="留空表示全客戶適用")
    plan_type: str = Field(default="AQL", description="FULL 全檢 / SKIP_LOT 跳批 / AQL 抽樣")
    lot_interval: int = Field(default=1, gt=0, description="每 N 批驗 1 批")
    aql: float = Field(default=1.0, gt=0, le=100)
    inspection_level: str = "II"
    levels: list[SamplingLevelIn] = Field(default_factory=list)
    remark: str = ""
    active: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.plan_type not in {"FULL", "SKIP_LOT", "AQL"}:
            raise ValueError("plan_type 必須是 FULL / SKIP_LOT / AQL")
        if self.plan_type == "AQL" and not self.levels:
            raise ValueError("AQL 計畫必須至少提供一段批量級距")
        return self


class SamplingPlanUpdate(MESModel):
    name: str | None = None
    device_id: str | None = None
    customer_code: str | None = None
    plan_type: str | None = None
    lot_interval: int | None = Field(default=None, gt=0)
    aql: float | None = Field(default=None, gt=0, le=100)
    inspection_level: str | None = None
    levels: list[SamplingLevelIn] | None = None
    remark: str | None = None
    active: bool | None = None


class InspectionDefect(MESModel):
    defect_code: str
    qty: int = Field(gt=0)


class InspectionJudgeIn(MESModel):
    """回報檢出不良數並做允收／拒收判定。"""

    lot_id: str
    defect_found: int = Field(ge=0)
    defects: list[InspectionDefect] = Field(default_factory=list)
    remark: str = ""

    @model_validator(mode="after")
    def _check(self):
        if self.defects:
            total = sum(d.qty for d in self.defects)
            if total != self.defect_found:
                raise ValueError(f"不良明細合計 {total} 與檢出不良數 {self.defect_found} 不符")
        return self
