"""SPC（統計製程管制）模型：量測項目與量測資料。"""

from pydantic import Field, model_validator

from app.models.base import MESModel

#: 管制圖常數表可支援的子群大小
MIN_SUBGROUP, MAX_SUBGROUP = 2, 10


class MeasurementItemIn(MESModel):
    """量測項目主檔，例如打線拉力、黏晶推力、封膠厚度。"""

    item_code: str = Field(min_length=2, max_length=24)
    name: str
    op_code: str = Field(description="所屬站別")
    device_id: str = Field(default="", description="限定料號；留空表示全料號適用")
    unit: str = Field(default="", description="gf / um / mm / °C")

    usl: float | None = Field(default=None, description="規格上限")
    lsl: float | None = Field(default=None, description="規格下限")
    target: float | None = Field(default=None, description="目標值")

    sample_size: int = Field(default=5, ge=MIN_SUBGROUP, le=MAX_SUBGROUP, description="每次量測筆數（子群大小 n）")
    auto_hold_on_violation: bool = Field(default=True, description="超規或判異時自動扣留批號")
    active: bool = True

    @model_validator(mode="after")
    def _check_spec(self):
        if self.usl is None and self.lsl is None:
            raise ValueError("規格上限與下限至少要設定一個")
        if self.usl is not None and self.lsl is not None:
            if self.usl <= self.lsl:
                raise ValueError("規格上限必須大於下限")
            if self.target is None:
                self.target = (self.usl + self.lsl) / 2
            elif not self.lsl <= self.target <= self.usl:
                raise ValueError("目標值必須落在規格範圍內")
        return self


class MeasurementIn(MESModel):
    """一次子群量測；批號目前所在站別必須與量測項目相符。"""

    item_code: str
    lot_id: str
    values: list[float] = Field(min_length=MIN_SUBGROUP, max_length=MAX_SUBGROUP)
    eq_id: str = ""
    remark: str = ""


class ControlLimitsIn(MESModel):
    """手動設定管制界限；未設定時由歷史資料自動推算。"""

    x_bar_bar: float
    r_bar: float = Field(ge=0)
