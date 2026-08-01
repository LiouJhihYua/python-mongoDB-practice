"""SECS/GEM 設備連線模型。"""

from pydantic import Field

from app.models.base import MESModel
from app.models.enums import SECSEventAction


class SECSLinkIn(MESModel):
    """設備的 HSMS 連線參數。"""

    host: str = "127.0.0.1"
    port: int = Field(default=5000, ge=1, le=65535)
    session_id: int = Field(default=0, ge=0, le=65535, description="HSMS Session ID（設備 ID）")
    mode: str = Field(default="ACTIVE", description="ACTIVE：由 MES 主動連線")
    t3_timeout_sec: int = Field(default=45, ge=1, le=600, description="回覆逾時")
    t5_timeout_sec: int = Field(default=10, ge=1, le=600, description="斷線後重連間隔")
    linktest_sec: int = Field(default=30, ge=5, le=3600)
    enabled: bool = True


class SECSEventRuleIn(MESModel):
    """CEID（設備事件）對應到 MES 動作的規則。"""

    eq_id: str = Field(default="", description="留空表示套用到所有設備")
    ceid: int = Field(ge=0)
    name: str
    action: SECSEventAction
    params: dict = Field(
        default_factory=dict,
        description='例如 {"state": "PRODUCTIVE"} 或 {"state_vid": "1.0"} 由報告變數取值',
    )
    enabled: bool = True


class SECSReport(MESModel):
    rptid: int = Field(default=1, ge=0)
    values: list[str] = Field(default_factory=list)


class SECSSimulateIn(MESModel):
    """不接實機，直接灌一筆 S6F11 事件報告進來。"""

    ceid: int = Field(ge=0)
    data_id: int = Field(default=0, ge=0)
    reports: list[SECSReport] = Field(default_factory=list)


class SECSCommandIn(MESModel):
    """S2F41 遠端指令。"""

    command: str = Field(min_length=1, description="START / STOP / PP-SELECT ...")
    parameters: dict[str, str] = Field(default_factory=dict)


class SECSDecodeIn(MESModel):
    """把十六進位字串解回 SECS-II 結構 —— 現場對協定時的除錯利器。"""

    hex: str = Field(min_length=2, description="HSMS 訊息（可含或不含 4 位元組長度前綴）")
