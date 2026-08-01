"""FastAPI 應用進入點。"""

from __future__ import annotations

import logging
import time
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
    audit,
    auth,
    dispatch,
    equipment,
    lots,
    master,
    materials,
    quality,
    reports,
    spc,
    tools,
    trace,
    workorders,
)
from app.security import decode_access_token
from app.services import audit_service
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


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _actor_from_request(request: Request) -> str | None:
    """從 Bearer Token 取出操作者；解不開就當作未登入，不影響請求本身。"""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        return decode_access_token(header.split(" ", 1)[1]).get("sub")
    except Exception:
        return None


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """所有異動類請求都留下稽核軌跡（不記錄請求內容，避免寫入密碼）。"""
    if request.method not in MUTATING_METHODS or request.url.path.startswith(audit_service.SKIP_PATHS):
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    try:
        await audit_service.record_api(
            get_db(),
            _actor_from_request(request),
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request.url.query,
        )
    except Exception as exc:  # 稽核失敗不可影響正常作業
        logger.warning("寫入稽核紀錄失敗：%s", exc)
    return response


@app.exception_handler(MESError)
async def mes_error_handler(request: Request, exc: MESError) -> JSONResponse:
    """商業邏輯錯誤 → 結構化回應，前端可直接顯示 message。"""
    logger.info("%s %s → %s: %s", request.method, request.url.path, exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message, "detail": exc.detail},
    )


for module in (
    auth, master, workorders, lots, dispatch, equipment, tools,
    quality, spc, materials, trace, reports, audit,
):
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
