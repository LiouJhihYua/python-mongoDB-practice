"""客訴（RMA）與載具管理模型。"""

from datetime import datetime

from pydantic import Field, model_validator

from app.models.base import MESModel


# ── 客訴 ────────────────────────────────────────────────────
class ComplaintIn(MESModel):
    """建單當下會自動跑一次影響分析，把受影響的批號與客戶圈出來。"""

    customer_code: str
    customer_ref: str = Field(default="", description="客戶端的客訴單號")
    device_id: str = Field(default="", description="留空時自申告批號推得")
    lot_ids: list[str] = Field(default_factory=list, description="客戶申告的批號")
    unit_seqs: list[int] = Field(
        default_factory=list, description="退回品的成品序號（有 die 綁定才填得出來）"
    )
    qty: int = Field(default=0, ge=0)
    severity: str = Field(default="MINOR", description="CRITICAL / MAJOR / MINOR")
    category: str = ""
    description: str = ""
    owner: str = Field(default="", description="負責人")
    due_date: datetime | None = None
    received_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.severity not in {"CRITICAL", "MAJOR", "MINOR"}:
            raise ValueError("severity 必須是 CRITICAL / MAJOR / MINOR")
        if not self.lot_ids and not self.description:
            raise ValueError("請至少提供申告批號或問題描述")
        return self


class D8StepIn(MESModel):
    content: str = Field(min_length=1)
    owner: str = ""
    completed: bool = True


class ComplaintStatusIn(MESModel):
    status: str = Field(description="INVESTIGATING / ACTION / CLOSED / REJECTED")
    remark: str = ""


class ComplaintNoteIn(MESModel):
    content: str = Field(min_length=1)


# ── 載具 ────────────────────────────────────────────────────
class CarrierIn(MESModel):
    carrier_id: str = Field(min_length=2, max_length=32)
    carrier_type: str = Field(default="MAGAZINE", description="MAGAZINE / BOAT / TRAY / FOUP / REEL_BOX")
    capacity: int = Field(default=0, ge=0, description="0 表示不限")
    location: str = ""
    clean_interval: int = Field(default=0, ge=0, description="累計使用幾次後要清洗，0 表示不管制")
    remark: str = ""
    active: bool = True


class CarrierAssignIn(MESModel):
    carrier_id: str
    lot_id: str
    remark: str = ""


class CarrierStatusIn(MESModel):
    status: str = Field(description="EMPTY / DIRTY / MAINTENANCE / SCRAPPED")
    remark: str = ""
