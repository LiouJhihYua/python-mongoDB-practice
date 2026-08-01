"""一鍵展示：內嵌 PostgreSQL + 主檔 + 模擬生產 + 啟動網頁。

不需要事先安裝 PostgreSQL —— pgserver 會就地啟動一個實例：

    python -m scripts.demo            # http://127.0.0.1:8000
    python -m scripts.demo --days 5   # 模擬 5 天份的生產資料
    python -m scripts.demo --keep     # 沿用上次的資料，不重新產生
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必須在匯入 app 之前設定，才能讓 Settings 讀到
os.environ["MES_DB_BACKEND"] = "embedded"
os.environ.setdefault("MES_EMBEDDED_DATA_DIR", "./data/pgdata-demo")


def main() -> None:
    parser = argparse.ArgumentParser(description="OSAT MES 一鍵展示")
    parser.add_argument("--days", type=int, default=3, help="模擬幾天份的生產資料")
    parser.add_argument("--keep", action="store_true", help="沿用既有資料，不重新產生")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    from app.database import acquire, truncate_all
    from app.main import app, startup_hooks
    from scripts.seed import seed
    from scripts.simulate import simulate

    async def load_demo_data() -> None:
        if args.keep:
            return
        async with acquire() as conn:
            await truncate_all(conn)
        await seed(reset=False)
        await simulate(days=args.days)

    startup_hooks.append(load_demo_data)
    print(f"\n展示模式啟動中，請稍候（內嵌 PostgreSQL，模擬 {args.days} 天生產資料）…")
    print(f"完成後開啟 http://{args.host}:{args.port}  帳號 admin / admin1234\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
