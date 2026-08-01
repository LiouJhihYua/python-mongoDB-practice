"""晶圓 Map 與 Die 級追溯的請求模型。"""

from pydantic import Field, model_validator

from app.models.base import MESModel


class WaferMapIn(MESModel):
    """上傳晶圓 Map。

    三種格式擇一：

    * ``grid`` —— 二維陣列，最直覺（``[[1, 1, 2], [1, -1, 1]]``）
    * ``rle``  —— 已壓縮的每列字串（``["1:2,2:1", "1:1,-1:1,1:1"]``）
    * ``text`` —— 每行一列、以空白分隔的文字檔內容（晶圓廠常見的 ASCII map）
    """

    wafer_id: str
    source: str = Field(default="CP", description="CP / AOI / FT")
    grid: list[list[int]] | None = None
    rle: list[str] | None = None
    text: str | None = None
    origin: str = Field(default="UPPER_LEFT", description="第一顆晶粒的座標原點")
    notch: str = Field(default="DOWN", description="晶圓缺口方向")
    null_bin: int = Field(default=-1, description="晶圓外／無晶粒位置的代碼")
    pass_bins: list[int] = Field(default_factory=lambda: [1], min_length=1)
    remark: str = ""
    update_wafer: bool = Field(default=True, description="是否同步回寫晶圓主檔的 CP 良品數")

    @model_validator(mode="after")
    def _check_source_format(self):
        given = [f for f in ("grid", "rle", "text") if getattr(self, f)]
        if len(given) != 1:
            raise ValueError("grid / rle / text 三種格式請擇一提供")
        if self.grid is not None and self.grid:
            widths = {len(row) for row in self.grid}
            if len(widths) != 1:
                raise ValueError("grid 每一列的長度必須相同")
            if not self.grid[0]:
                raise ValueError("grid 不可為空列")
        if self.null_bin in self.pass_bins:
            raise ValueError("null_bin 不可同時被列為良品 Bin")
        return self


class DieAssignIn(MESModel):
    """把晶圓上的良品晶粒綁定到批號的成品序號（黏晶時執行）。"""

    lot_id: str
    qty: int | None = Field(default=None, gt=0, description="未指定時取批號現有量")
    wafer_ids: list[str] = Field(default_factory=list, description="限定只從這些晶圓取用")


class DieResultItem(MESModel):
    unit_seq: int = Field(gt=0)
    ft_bin: int


class DieResultIn(MESModel):
    """回寫成品測試結果到晶粒座標，達成 die 級追溯。"""

    lot_id: str
    results: list[DieResultItem] = Field(min_length=1)
    pass_bins: list[int] = Field(default_factory=lambda: [1], min_length=1)

    @model_validator(mode="after")
    def _check_unique(self):
        seqs = [r.unit_seq for r in self.results]
        if len(set(seqs)) != len(seqs):
            raise ValueError("results 內的 unit_seq 重複")
        return self
