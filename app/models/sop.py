"""e-SOP 電子作業指導書模型。"""

from datetime import datetime

from pydantic import Field, model_validator

from app.models.base import MESModel


class SOPStep(MESModel):
    """一個作業步驟。"""

    seq: int = Field(gt=0)
    instruction: str = Field(min_length=1, description="作業動作")
    detail: str = ""
    image: str = Field(default="", description="示意圖網址")
    checkpoint: str = Field(default="", description="自主檢查點")
    duration_sec: int = Field(default=0, ge=0)


class SOPAttachment(MESModel):
    name: str
    url: str = ""
    kind: str = Field(default="LINK", description="LINK / IMAGE / VIDEO / PDF")


class SOPIn(MESModel):
    """建立新的 SOP（版本由系統給號，一律從 DRAFT 起算）。"""

    sop_code: str
    title: str
    op_code: str
    device_id: str = Field(default="", description="留空表示全料號適用")
    summary: str = ""
    steps: list[SOPStep] = Field(default_factory=list)
    hazards: str = Field(default="", description="風險與注意事項")
    ppe: list[str] = Field(default_factory=list, description="應穿戴的防護具")
    attachments: list[SOPAttachment] = Field(default_factory=list)
    require_ack: bool = Field(default=True, description="作業員進站前是否必須先確認")

    @model_validator(mode="after")
    def _check_steps(self):
        seqs = [s.seq for s in self.steps]
        if len(set(seqs)) != len(seqs):
            raise ValueError("步驟序號重複")
        if seqs != sorted(seqs):
            raise ValueError("步驟序號必須遞增")
        return self


class SOPUpdate(MESModel):
    """修改草稿內容（僅 DRAFT 版本可改）。"""

    title: str | None = None
    device_id: str | None = None
    summary: str | None = None
    steps: list[SOPStep] | None = None
    hazards: str | None = None
    ppe: list[str] | None = None
    attachments: list[SOPAttachment] | None = None
    require_ack: bool | None = None


class SOPReleaseIn(MESModel):
    effective_from: datetime | None = Field(default=None, description="生效時間，留空為立即生效")
    remark: str = ""


class SOPAckIn(MESModel):
    sop_code: str
    version: int | None = Field(default=None, description="留空表示確認目前生效的版本")
