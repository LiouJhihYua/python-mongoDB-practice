"""治具壽命管理。

打線毛細管、切割刀這類耗材是以「累計加工顆數」計壽命的：超壽命繼續用，
不良率會悄悄爬升卻找不到原因。系統在出站時自動累計，到期直接把機台停下來。
"""

from __future__ import annotations

from app.database import COL_EQUIPMENTS, COL_TOOL_LOGS, COL_TOOLS
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import clean, clean_all, utcnow
from app.models.enums import EquipmentState, ToolStatus
from app.services import equipment_service
from app.services.crud import CRUD

tools = CRUD(COL_TOOLS, "tool_id", "治具")


async def create_tool(db, payload: dict, actor: str) -> dict:
    doc = dict(payload)
    doc.update(
        status=ToolStatus.IDLE.value,
        eq_id=None,
        used_count=0,
        mounted_at=None,
        mount_count=0,
    )
    return await tools.create(db, doc, actor)


async def get_tool(db, tool_id: str) -> dict:
    doc = await db[COL_TOOLS].find_one({"tool_id": tool_id})
    if doc is None:
        raise NotFoundError(f"找不到治具：{tool_id}")
    return doc


async def _log(db, tool: dict, action: str, actor: str, **extra) -> None:
    doc = {
        "tool_id": tool["tool_id"],
        "tool_type": tool.get("tool_type"),
        "eq_id": tool.get("eq_id"),
        "action": action,
        "used_count": tool.get("used_count", 0),
        "life_limit": tool.get("life_limit"),
        "operator": actor,
        "timestamp": utcnow(),
    }
    doc.update(extra)
    await db[COL_TOOL_LOGS].insert_one(doc)


async def mount(db, tool_id: str, eq_id: str, actor: str, remark: str = "") -> dict:
    """治具上機。"""
    tool = await get_tool(db, tool_id)
    if not tool.get("active", True):
        raise StateError(f"治具 {tool_id} 已停用")
    if tool["status"] == ToolStatus.MOUNTED.value:
        raise StateError(f"治具 {tool_id} 已掛載於 {tool.get('eq_id')}")
    if tool["status"] in {ToolStatus.EXPIRED.value, ToolStatus.SCRAPPED.value}:
        raise StateError(f"治具 {tool_id} 狀態為 {tool['status']}，不可上機")

    eq = await equipment_service.get_equipment(db, eq_id)
    capability = set(eq.get("op_codes") or [])
    applicable = set(tool.get("op_codes") or [])
    if applicable and not (applicable & capability):
        raise ValidationError(
            f"治具 {tool_id} 適用站別 {sorted(applicable)} 與設備 {eq_id} 的能力 {sorted(capability)} 不符"
        )

    now = utcnow()
    await db[COL_TOOLS].update_one(
        {"tool_id": tool_id},
        {
            "$set": {
                "status": ToolStatus.MOUNTED.value, "eq_id": eq_id,
                "mounted_at": now, "updated_at": now, "updated_by": actor,
            },
            "$inc": {"mount_count": 1},
        },
    )
    await _log(db, {**tool, "eq_id": eq_id}, "MOUNT", actor, remark=remark)
    return clean(await db[COL_TOOLS].find_one({"tool_id": tool_id}))


async def unmount(db, tool_id: str, actor: str, scrap: bool = False, remark: str = "") -> dict:
    """治具下機；壽命到期者直接報廢，未到期者回庫可續用。"""
    tool = await get_tool(db, tool_id)
    # 壽命到期（EXPIRED）的治具仍掛在機台上，正是需要拆下來的狀態
    attached = tool["status"] in {ToolStatus.MOUNTED.value, ToolStatus.EXPIRED.value} and tool.get("eq_id")
    if not attached and not scrap:
        raise StateError(f"治具 {tool_id} 未掛載於任何設備")

    expired = int(tool.get("used_count", 0)) >= int(tool["life_limit"])
    status = ToolStatus.SCRAPPED if (scrap or expired) else ToolStatus.IDLE
    now = utcnow()
    await db[COL_TOOLS].update_one(
        {"tool_id": tool_id},
        {"$set": {"status": status.value, "eq_id": None, "mounted_at": None,
                  "updated_at": now, "updated_by": actor}},
    )
    await _log(db, tool, "UNMOUNT", actor, remark=remark, result=status.value)
    return clean(await db[COL_TOOLS].find_one({"tool_id": tool_id}))


async def consume(db, eq_id: str, qty: int, lot_id: str, actor: str) -> list[dict]:
    """出站時累計該設備上所有治具的使用量，回傳需要注意的治具。

    到期的治具會把設備轉為計畫停機（待換刀），避免超壽命繼續生產。
    """
    if qty <= 0:
        return []
    mounted = await db[COL_TOOLS].find(
        {"eq_id": eq_id, "status": ToolStatus.MOUNTED.value}
    ).to_list(length=None)

    alerts: list[dict] = []
    for tool in mounted:
        used = int(tool.get("used_count", 0)) + int(qty)
        limit = int(tool["life_limit"])
        ratio = used / limit if limit else 0.0
        expired = used >= limit
        await db[COL_TOOLS].update_one(
            {"tool_id": tool["tool_id"]},
            {"$set": {
                "used_count": used,
                "status": ToolStatus.EXPIRED.value if expired else ToolStatus.MOUNTED.value,
                "updated_at": utcnow(),
            }},
        )
        if expired or ratio >= float(tool.get("warning_ratio", 0.9)):
            alerts.append({
                "tool_id": tool["tool_id"],
                "tool_type": tool.get("tool_type"),
                "eq_id": eq_id,
                "used_count": used,
                "life_limit": limit,
                "usage_ratio": round(ratio, 4),
                "expired": expired,
                "message": (
                    f"治具 {tool['tool_id']} 已達壽命上限（{used}/{limit}），設備已停機待換"
                    if expired
                    else f"治具 {tool['tool_id']} 使用率 {ratio:.0%}，請準備更換"
                ),
            })
            await _log(
                db, {**tool, "used_count": used}, "EXPIRE" if expired else "WARN",
                actor, lot_id=lot_id, usage_ratio=round(ratio, 4),
            )
        if expired:
            await equipment_service.set_state(
                db, eq_id, EquipmentState.SCHEDULED_DOWN, actor,
                reason_code="TOOL_EXPIRED",
                remark=f"治具 {tool['tool_id']} 壽命到期，待更換",
            )
    return alerts


async def replace(db, old_tool_id: str, new_tool_id: str, actor: str, remark: str = "") -> dict:
    """換刀：舊治具下機、新治具上同一台設備，並讓設備回到待機。"""
    old = await get_tool(db, old_tool_id)
    eq_id = old.get("eq_id")
    if not eq_id:
        raise StateError(f"治具 {old_tool_id} 並未掛載於任何設備")

    await unmount(db, old_tool_id, actor, remark=remark)
    new_tool = await mount(db, new_tool_id, eq_id, actor, remark=f"更換 {old_tool_id}")
    eq = await equipment_service.get_equipment(db, eq_id)
    if eq.get("current_state") == EquipmentState.SCHEDULED_DOWN.value:
        await equipment_service.set_state(
            db, eq_id, EquipmentState.STANDBY, actor, "TOOL_REPLACED", f"已換上 {new_tool_id}"
        )
    return {"equipment": eq_id, "removed": old_tool_id, "installed": new_tool}


async def list_tools(db, status: str | None = None, eq_id: str | None = None, limit: int = 200) -> list[dict]:
    filt = {k: v for k, v in {"status": status, "eq_id": eq_id}.items() if v}
    rows = clean_all(
        await db[COL_TOOLS].find(filt).sort([("tool_id", 1)]).limit(limit).to_list(length=limit)
    )
    for row in rows:
        limit_count = int(row.get("life_limit") or 0)
        used = int(row.get("used_count") or 0)
        row["usage_ratio"] = round(used / limit_count, 4) if limit_count else 0.0
        row["remaining"] = max(0, limit_count - used)
    return rows


async def attention_list(db) -> list[dict]:
    """需要處理的治具：已到期或接近壽命。"""
    rows = await list_tools(db)
    watch = [
        row for row in rows
        if row["status"] in {ToolStatus.EXPIRED.value, ToolStatus.MOUNTED.value}
        and row["usage_ratio"] >= float(row.get("warning_ratio", 0.9))
    ]
    return sorted(watch, key=lambda r: -r["usage_ratio"])


async def tool_logs(db, tool_id: str, limit: int = 100) -> list[dict]:
    cursor = db[COL_TOOL_LOGS].find({"tool_id": tool_id}).sort([("timestamp", -1)]).limit(limit)
    return clean_all(await cursor.to_list(length=limit))
