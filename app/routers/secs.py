"""SECS/GEM 設備連線 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.errors import ValidationError
from app.models.enums import Role
from app.models.secs import (
    SECSCommandIn,
    SECSDecodeIn,
    SECSEventRuleIn,
    SECSLinkIn,
    SECSSimulateIn,
)
from app.routers.deps import DB
from app.security import CurrentUser, get_current_user, require_roles
from app.services import secs, secs_service

router = APIRouter(prefix="/api/secs", tags=["SECS/GEM 設備連線"])

CanEdit = Depends(require_roles(Role.ENGINEER))
CanRead = Depends(get_current_user)


# ── 連線設定 ────────────────────────────────────────────────
@router.get("/links", dependencies=[CanRead], summary="設備連線清單")
async def list_links(db: DB):
    return await secs_service.list_links(db)


@router.get("/status", dependencies=[CanRead], summary="連線看板")
async def status(db: DB):
    return await secs_service.status(db)


@router.put("/links/{eq_id}", dependencies=[CanEdit], summary="設定設備連線參數")
async def upsert_link(db: DB, eq_id: str, payload: SECSLinkIn, user: CurrentUser):
    return await secs_service.upsert_link(db, eq_id, payload.model_dump(), user["username"])


@router.get("/links/{eq_id}", dependencies=[CanRead], summary="設備連線明細")
async def get_link(db: DB, eq_id: str):
    return await secs_service.get_link(db, eq_id)


@router.delete("/links/{eq_id}", dependencies=[CanEdit], summary="刪除設備連線設定")
async def delete_link(db: DB, eq_id: str, user: CurrentUser):
    return await secs_service.delete_link(db, eq_id, user["username"])


@router.post("/links/{eq_id}/connect", dependencies=[CanEdit], summary="建立連線")
async def connect(db: DB, eq_id: str, user: CurrentUser):
    return await secs_service.start_link(db, eq_id, user["username"])


@router.post("/links/{eq_id}/disconnect", dependencies=[CanEdit], summary="中斷連線")
async def disconnect(db: DB, eq_id: str, user: CurrentUser):
    return await secs_service.stop_link(db, eq_id, user["username"])


# ── 事件規則 ────────────────────────────────────────────────
@router.get("/rules", dependencies=[CanRead], summary="事件對應規則清單")
async def list_rules(db: DB, eq_id: str | None = None):
    return await secs_service.list_rules(db, eq_id)


@router.put("/rules", dependencies=[CanEdit], summary="新增／更新事件對應規則")
async def upsert_rule(db: DB, payload: SECSEventRuleIn, user: CurrentUser):
    return await secs_service.upsert_rule(db, payload.model_dump(), user["username"])


@router.delete("/rules/{rule_id}", dependencies=[CanEdit], summary="刪除事件對應規則")
async def delete_rule(db: DB, rule_id: int, user: CurrentUser):
    return await secs_service.delete_rule(db, rule_id, user["username"])


# ── 訊息與操作 ──────────────────────────────────────────────
@router.get("/messages", dependencies=[CanRead], summary="SECS 訊息記錄")
async def messages(
    db: DB,
    eq_id: str | None = None,
    stream: int | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    return await secs_service.list_messages(db, eq_id, stream, limit)


@router.post("/links/{eq_id}/simulate-event", dependencies=[CanEdit], summary="模擬設備事件（S6F11）")
async def simulate(db: DB, eq_id: str, payload: SECSSimulateIn, user: CurrentUser):
    return await secs_service.simulate_event(
        db, eq_id, payload.model_dump(), user["username"]
    )


@router.post("/links/{eq_id}/command", dependencies=[CanEdit], summary="遠端指令（S2F41）")
async def command(db: DB, eq_id: str, payload: SECSCommandIn, user: CurrentUser):
    return await secs_service.send_command(db, eq_id, payload.model_dump(), user["username"])


@router.post("/links/{eq_id}/sync-clock", dependencies=[CanEdit], summary="設備對時（S2F31）")
async def sync_clock(db: DB, eq_id: str, user: CurrentUser):
    return await secs_service.sync_clock(db, eq_id, user["username"])


@router.post("/decode", dependencies=[CanRead], summary="解析 HSMS 十六進位訊息")
async def decode(payload: SECSDecodeIn):
    text = "".join(payload.hex.split()).replace("0x", "")
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ValidationError("hex 不是合法的十六進位字串")
    try:
        frames, rest = secs.split_frames(raw)
        frame = frames[0] if frames else raw
        message = secs.decode_message(frame)
    except (secs.SECSError, IndexError) as exc:
        raise ValidationError(f"無法解析：{exc}")
    return {
        "name": secs.message_name(message),
        "message": message,
        "sml": secs.to_sml(message["body"]) if message["body"] else "",
        "python": secs.to_python(message["body"]),
        "trailing_bytes": len(rest),
    }
