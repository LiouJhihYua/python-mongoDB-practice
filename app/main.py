"""FastAPI 應用進入點。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import close_db, connect_db, get_db
from app.errors import MESError
from app.routers import (
    auth,
    equipment,
    lots,
    master,
    materials,
    quality,
    reports,
    trace,
    workorders,
)
from app.services.user_service import ensure_bootstrap_admin

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
logger = logging.getLogger("mes")

STATIC_DIR = Path(__file__).parent / "static"

#: 連線與初始化完成後要執行的額外工作；scripts/demo.py 用它塞入示範資料
startup_hooks: list[Callable[[], Awaitable[None]]] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    await ensure_bootstrap_admin(get_db())
    for hook in startup_hooks:
        await hook()
    logger.info(
        "%s v%s 啟動完成（廠區 %s / 後端 %s）",
        settings.app_name, settings.app_version, settings.factory_code, settings.db_backend,
    )
    yield
    await close_db()


app = FastAPI(
    title=f"{settings.app_name} — OSAT 封裝測試廠製造執行系統",
    version=settings.app_version,
    description=(
        "涵蓋主檔、工單、批號進出站、拆併批、扣留放行、設備 OEE、"
        "品質分析、材料耗用與正逆向追溯的 MES API。"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(MESError)
async def mes_error_handler(request: Request, exc: MESError) -> JSONResponse:
    """商業邏輯錯誤 → 結構化回應，前端可直接顯示 message。"""
    logger.info("%s %s → %s: %s", request.method, request.url.path, exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message, "detail": exc.detail},
    )


for module in (auth, master, workorders, lots, equipment, quality, materials, trace, reports):
    app.include_router(module.router)


@app.get("/api/health", tags=["系統"], summary="健康檢查")
async def health():
    db = get_db()
    collections = await db.list_collection_names()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "factory": settings.factory_code,
        "db_backend": settings.db_backend,
        "collections": len(collections),
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")
