"""共用模型工具：時間、文件序列化、分頁。"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings

T = TypeVar("T")


#: 可注入的時間來源；模擬產線與測試會覆寫它，正式執行時為 None
_clock: Callable[[], datetime] | None = None


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """覆寫系統時間來源（模擬／測試用），傳入 None 還原成真實時間。"""
    global _clock
    _clock = fn


def utcnow() -> datetime:
    """一律以 UTC 存放，前端／報表再依廠區時區換算。"""
    return _clock() if _clock is not None else datetime.now(timezone.utc)


def ensure_aware(dt: datetime) -> datetime:
    """MongoDB 取回的時間可能沒有時區資訊，一律補成 UTC 再運算。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_local(dt: datetime) -> datetime:
    """轉成廠區當地時間（用於班別與日報切分）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=settings.tz_offset_hours)))


def shift_of(dt: datetime) -> str:
    """依廠區班別設定回傳班別代碼，例如 20250801-D / 20250801-N。"""
    local = to_local(dt)
    starts = sorted(settings.shift_start_hours) or [0]
    labels = ["D", "N", "S3", "S4"]
    idx = 0
    for i, start in enumerate(starts):
        if local.hour >= start:
            idx = i
    # 早於當日第一個班別起始時間 → 歸屬前一天的最後一班
    if local.hour < starts[0]:
        local = local - timedelta(days=1)
        idx = len(starts) - 1
    return f"{local:%Y%m%d}-{labels[idx] if idx < len(labels) else f'S{idx + 1}'}"


def shift_window(label: str) -> tuple[datetime, datetime]:
    """由班別代碼還原出該班的起訖時間（UTC），供交接班報表使用。"""
    date_part, _, code = label.partition("-")
    starts = sorted(settings.shift_start_hours) or [0]
    labels = ["D", "N", "S3", "S4"][: len(starts)]
    try:
        idx = labels.index(code)
    except ValueError:
        raise ValueError(f"班別代碼無法辨識：{label}")
    tz = timezone(timedelta(hours=settings.tz_offset_hours))
    try:
        day = datetime.strptime(date_part, "%Y%m%d").replace(tzinfo=tz)
    except ValueError:
        raise ValueError(f"班別日期格式錯誤：{label}（應為 YYYYMMDD-D）")

    start = day.replace(hour=starts[idx])
    if idx + 1 < len(starts):
        end = day.replace(hour=starts[idx + 1])
    else:
        end = (day + timedelta(days=1)).replace(hour=starts[0])
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def mongo_encode(value: Any) -> Any:
    """遞迴把 Enum 轉成原生字串，確保寫入 MongoDB 的都是純量。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: mongo_encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mongo_encode(v) for v in value]
    return value


def to_mongo(model: BaseModel, **kwargs: Any) -> dict:
    """Pydantic 模型 → MongoDB 文件。"""
    return mongo_encode(model.model_dump(**kwargs))


def clean(doc: dict | None) -> dict | None:
    """MongoDB 文件 → API 回應（把 ObjectId 轉字串）。"""
    if doc is None:
        return None
    out = dict(doc)
    if "_id" in out:
        out["id"] = str(out.pop("_id"))
    return out


def clean_all(docs: list[dict]) -> list[dict]:
    return [clean(d) for d in docs]


class MESModel(BaseModel):
    """所有請求／回應模型的基底。"""

    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=False,
        str_strip_whitespace=True,
    )


class Page(MESModel, Generic[T]):
    """統一分頁結構。"""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    skip: int = 0
    limit: int = 50


class OkResponse(MESModel):
    ok: bool = True
    message: str = ""
    data: dict | None = None
