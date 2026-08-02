"""資料庫結構升級。

服務啟動時會自動套用尚未執行的 migration，這支腳本是給維運手動確認／執行用的：

    python -m scripts.migrate            # 套用所有待執行的 migration
    python -m scripts.migrate --status   # 只看狀態，不執行

新增結構變更時，在 ``app/migrations/`` 放一個檔名遞增的 SQL 檔即可
（例如 ``0005_add_xxx.sql``），內容請寫成可重複執行的形式。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import (  # noqa: E402
    apply_schema,
    close_db,
    connect_db,
    migration_status,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("migrate")


async def main(status_only: bool) -> None:
    pool = await connect_db()  # connect_db 本身就會套用待執行的 migration
    try:
        async with pool.acquire() as db:
            if not status_only:
                # connect_db 已經跑過一次，這裡再跑只會回報「沒有待執行的項目」
                applied = await apply_schema(db)
                if applied:
                    log.info("本次套用：%s", ", ".join(applied))
                else:
                    log.info("沒有待執行的 migration")
            rows = await migration_status(db)
            width = max(len(r["version"]) for r in rows)
            for row in rows:
                mark = "✓" if row["applied"] else "×"
                when = f"{row['applied_at']:%Y-%m-%d %H:%M:%S}" if row["applied_at"] else "尚未套用"
                log.info("%s %-*s  %s", mark, width, row["version"], when)
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="套用資料庫結構變更")
    parser.add_argument("--status", action="store_true", help="只顯示狀態，不執行")
    args = parser.parse_args()
    asyncio.run(main(args.status))
