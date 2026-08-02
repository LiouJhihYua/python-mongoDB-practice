"""配方管理模型。"""

from datetime import datetime

from pydantic import Field

from app.models.base import MESModel


class RecipeIn(MESModel):
    """建立配方草稿；版本由系統給號。"""

    ppid: str = Field(min_length=1, description="機台端的配方名稱（Process Program ID）")
    name: str
    op_code: str
    device_id: str = Field(default="", description="留空表示全料號適用")
    eq_model: str = Field(default="", description="留空表示不限機型")
    parameters: dict = Field(default_factory=dict, description="配方參數，例如 {\"溫度\": 175}")
    checksum: str = Field(default="", description="機台端配方的校驗碼，留空表示不比對")
    remark: str = ""


class RecipeUpdate(MESModel):
    """修改草稿內容（僅 DRAFT 版本可改）。"""

    name: str | None = None
    device_id: str | None = None
    eq_model: str | None = None
    parameters: dict | None = None
    checksum: str | None = None
    remark: str | None = None


class RecipeReleaseIn(MESModel):
    effective_from: datetime | None = Field(default=None, description="生效時間，留空為立即生效")
    remark: str = ""


class RecipeLoadIn(MESModel):
    """回報機台目前載入的配方。"""

    ppid: str = Field(min_length=1)
    version: int | None = Field(default=None, gt=0)
    checksum: str = ""


class RecipeDownloadIn(MESModel):
    """把 MES 上核可的配方下發到機台（S7F3）。"""

    ppid: str = Field(min_length=1)
    version: int | None = Field(default=None, gt=0, description="留空表示取最新版")


class RecipeSelectIn(MESModel):
    """叫機台切換到指定配方（S2F41 PP-SELECT）。"""

    ppid: str = Field(min_length=1)
