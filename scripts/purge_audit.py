"""稽核紀錄歸檔與清除。

稽核紀錄會無限成長，但刪掉就回不來了 —— 所以這支腳本預設**只試算**，
而且要求先把資料匯出成 JSON Lines 才允許真的刪除。

    python -m scripts.purge_audit                          # 試算
    python -m scripts.purge_audit --archive audit.jsonl    # 歸檔（不刪）
    python -m scripts.purge_audit --archive audit.jsonl --confirm   # 歸檔後刪除
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.database import close_db, connect_db  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.services import audit_service  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("purge-audit")


async def main(days: int, archive: str | None, confirm: bool) -> None:
    pool = await connect_db()
    try:
        async with pool.acquire() as db:
            status = await audit_service.retention_status(db)
            log.info(
                "目前共 %s 筆稽核紀錄，最舊 %s；保存期限 %s 天",
                status["total"], status["oldest"] or "（無）", status["retention_days"],
            )

            before = utcnow() - timedelta(days=days)
            if archive:
                content = await audit_service.export_jsonl(db, before)
                path = Path(archive)
                path.write_text(content, encoding="utf-8")
                lines = content.count("\n") + 1 if content else 0
                log.info("已歸檔 %s 筆到 %s", lines, path.resolve())

            if confirm and not archive:
                log.error("為避免誤刪，--confirm 必須搭配 --archive 一起使用")
                return

            result = await audit_service.purge(db, before, dry_run=not confirm)
            if result["dry_run"]:
                log.info(
                    "試算：%s 之前共 %s 筆可清除（加上 --archive 與 --confirm 才會實際刪除）",
                    f"{before:%Y-%m-%d}", result["matched"],
                )
            else:
                log.info("已清除 %s 筆 %s 之前的稽核紀錄", result["deleted"], f"{before:%Y-%m-%d}")
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="稽核紀錄歸檔與清除")
    parser.add_argument(
        "--days", type=int, default=settings.audit_retention_days,
        help=f"清除幾天前的資料（預設 {settings.audit_retention_days}）",
    )
    parser.add_argument("--archive", help="先把資料匯出到這個檔案（JSON Lines）")
    parser.add_argument("--confirm", action="store_true", help="真的刪除；必須搭配 --archive")
    args = parser.parse_args()
    asyncio.run(main(args.days, args.archive, args.confirm))
