"""ERP 介接模型。"""

from datetime import datetime

from pydantic import Field, model_validator

from app.models.base import MESModel
from app.models.enums import ERPDocType, ERPOutboundType


class ERPInboundIn(MESModel):
    """ERP 下行單據。``external_id`` 是冪等鍵，重送同一筆不會重複建檔。"""

    doc_type: ERPDocType
    external_id: str = Field(min_length=1, description="ERP 端單號")
    payload: dict = Field(description="單據內容，欄位依 doc_type 而定")
    source: str = Field(default="REST", description="REST / FILE")


class ERPInboundBatchIn(MESModel):
    documents: list[ERPInboundIn] = Field(min_length=1, max_length=500)


class ERPCsvIn(MESModel):
    """以 CSV 匯入下行單據（沒有中介平台的小廠最常用的方式）。"""

    doc_type: ERPDocType
    content: str = Field(min_length=1, description="含表頭的 CSV 內容")
    external_id_column: str = Field(default="external_id", description="作為冪等鍵的欄位")


class ERPProcessIn(MESModel):
    doc_type: ERPDocType | None = None
    limit: int = Field(default=50, ge=1, le=500)


class ERPOutboundBuildIn(MESModel):
    """把指定期間的 MES 事件整理成待送 ERP 的單據。"""

    doc_types: list[ERPOutboundType] = Field(default_factory=list, description="留空表示全部")
    start: datetime | None = None
    end: datetime | None = None
    hours: int = Field(default=24, ge=1, le=24 * 90)


class ERPOutboundAckIn(MESModel):
    ids: list[int] = Field(min_length=1, max_length=1000)
    remark: str = ""


class ERPOutboundFailIn(MESModel):
    ids: list[int] = Field(min_length=1, max_length=1000)
    error: str = Field(min_length=1)

    @model_validator(mode="after")
    def _dedupe(self):
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("ids 重複")
        return self
