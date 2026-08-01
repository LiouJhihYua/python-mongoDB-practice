"""一鍵展示：記憶體資料庫 + 主檔 + 模擬生產 + 啟動網頁。

不需要安裝 MongoDB 即可看到完整系統：

    python -m scripts.demo            # http://127.0.0.1:8000
    python -m scripts.demo --days 5   # 模擬 5 天份的生產資料
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必須在匯入 app 之前設定，才能讓 Settings 讀到
os.environ["MES_DB_BACKEND"] = "memory"


def main() -> None:
    parser = argparse.ArgumentParser(description="OSAT MES 一鍵展示")
    parser.add_argument("--days", type=int, default=3, help="模擬幾天份的生產資料")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    from app.main import app, startup_hooks
    from scripts.seed import seed
    from scripts.simulate import simulate

    async def load_demo_data() -> None:
        await seed(reset=False)
        await simulate(days=args.days)

    startup_hooks.append(load_demo_data)
    print(f"\n展示模式啟動中，請稍候（模擬 {args.days} 天生產資料）…")
    print(f"完成後開啟 http://{args.host}:{args.port}  帳號 admin / admin1234\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
