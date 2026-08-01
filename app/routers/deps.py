"""路由共用相依與工具。"""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, Query

from app.database import get_db
from app.models.base import utcnow

DB = Annotated[Any, Depends(get_db)]


def parse_window(
    start: Annotated[datetime | None, Query(description="起始時間（ISO 8601）")] = None,
    end: Annotated[datetime | None, Query(description="結束時間（ISO 8601）")] = None,
    hours: Annotated[int, Query(ge=1, le=24 * 90, description="未指定起訖時間時，往回推 N 小時")] = 24,
) -> tuple[datetime, datetime]:
    """統一的報表時間區間解析：優先用 start/end，否則取最近 N 小時。"""
    now = utcnow()
    resolved_end = end or now
    resolved_start = start or (resolved_end - timedelta(hours=hours))
    return resolved_start, resolved_end


Window = Annotated[tuple[datetime, datetime], Depends(parse_window)]
