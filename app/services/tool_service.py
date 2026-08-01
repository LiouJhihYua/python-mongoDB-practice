"""治具壽命管理。

打線毛細管、切割刀這類耗材是以「累計加工顆數」計壽命的：超壽命繼續用，
不良率會悄悄爬升卻找不到原因。系統在出站時自動累計，到期直接把機台停下來。
"""

from __future__ import annotations

from app.database import T_TOOL_LOGS, T_TOOLS, fetch_all, fetch_one
from app.errors import NotFoundError, StateError, ValidationError
from app.models.base import utcnow
from app.models.enums import EquipmentState, ToolStatus
from app.services import equipment_service
from app.services.crud import Table

tools = Table(T_TOOLS, "tool_id", "治具")


async def create_tool(db, payload: dict, actor: str) -> dict:
    doc = dict(payload)
    doc.update(status=ToolStatus.IDLE.value, eq_id=None, used_count=0, mounted_at=None, mount_count=0)
    return await tools.create(db, doc, actor)


async def get_tool(db, tool_id: str) -> dict:
    row = await fetch_one(db, f"SELECT * FROM {T_TOOLS} WHERE tool_id = $1", tool_id)
    if row is None:
        raise NotFoundError(f"找不到治具：{tool_id}")
    return row


async def _log(db, tool: dict, action: str, actor: str, **extra) -> None:
    await db.execute(
        f"""
        INSERT INTO {T_TOOL_LOGS}
            (tool_id, tool_type, eq_id, action, used_count, life_limit,
             usage_ratio, lot_id, operator, remark, result, timestamp)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        tool["tool_id"], tool.get("tool_type"), tool.get("eq_id"), action,
        int(tool.get("used_count") or 0), tool.get("life_limit"),
        extra.get("usage_ratio"), extra.get("lot_id"), actor,
        extra.get("remark", ""), extra.get("result"), utcnow(),
    )


async def mount(db, tool_id: str, eq_id: str, actor: str, remark: str = "") -> dict:
    """治具上機。"""
    async with db.transaction():
        tool = await fetch_one(db, f"SELECT * FROM {T_TOOLS} WHERE tool_id = $1 FOR UPDATE", tool_id)
        if tool is None:
            raise NotFoundError(f"找不到治具：{tool_id}")
        if not tool["active"]:
            raise StateError(f"治具 {tool_id} 已停用")
        if tool["status"] == ToolStatus.MOUNTED.value:
            raise StateError(f"治具 {tool_id} 已掛載於 {tool['eq_id']}")
        if tool["status"] in {ToolStatus.EXPIRED.value, ToolStatus.SCRAPPED.value}:
            raise StateError(f"治具 {tool_id} 狀態為 {tool['status']}，不可上機")

        eq = await equipment_service.get_equipment(db, eq_id)
        capability = set(eq["op_codes"] or [])
        applicable = set(tool["op_codes"] or [])
        if applicable and not (applicable & capability):
            raise ValidationError(
                f"治具 {tool_id} 適用站別 {sorted(applicable)} 與設備 {eq_id} 的能力 {sorted(capability)} 不符"
            )

        now = utcnow()
        updated = await fetch_one(
            db,
            f"""
            UPDATE {T_TOOLS}
            SET status = $1, eq_id = $2, mounted_at = $3, mount_count = mount_count + 1,
                updated_at = $3, updated_by = $4
            WHERE tool_id = $5 RETURNING *
            """,
            ToolStatus.MOUNTED.value, eq_id, now, actor, tool_id,
        )
        await _log(db, updated, "MOUNT", actor, remark=remark)
        return updated


async def unmount(db, tool_id: str, actor: str, scrap: bool = False, remark: str = "") -> dict:
    """治具下機；壽命到期者直接報廢，未到期者回庫可續用。"""
    async with db.transaction():
        tool = await fetch_one(db, f"SELECT * FROM {T_TOOLS} WHERE tool_id = $1 FOR UPDATE", tool_id)
        if tool is None:
            raise NotFoundError(f"找不到治具：{tool_id}")
        # 壽命到期（EXPIRED）的治具仍掛在機台上，正是需要拆下來的狀態
        attached = tool["status"] in {ToolStatus.MOUNTED.value, ToolStatus.EXPIRED.value} and tool["eq_id"]
        if not attached and not scrap:
            raise StateError(f"治具 {tool_id} 未掛載於任何設備")

        expired = int(tool["used_count"]) >= int(tool["life_limit"])
        status = ToolStatus.SCRAPPED if (scrap or expired) else ToolStatus.IDLE
        updated = await fetch_one(
            db,
            f"""
            UPDATE {T_TOOLS}
            SET status = $1, eq_id = NULL, mounted_at = NULL, updated_at = now(), updated_by = $2
            WHERE tool_id = $3 RETURNING *
            """,
            status.value, actor, tool_id,
        )
        await _log(db, tool, "UNMOUNT", actor, remark=remark, result=status.value)
        return updated


async def consume(db, eq_id: str, qty: int, lot_id: str, actor: str) -> list[dict]:
    """出站時累計該設備上所有治具的使用量，回傳需要注意的治具。

    到期的治具會把設備轉為計畫停機（待換刀），避免超壽命繼續生產。
    """
    if qty <= 0:
        return []
    mounted = await fetch_all(
        db,
        f"SELECT * FROM {T_TOOLS} WHERE eq_id = $1 AND status = $2 FOR UPDATE",
        eq_id, ToolStatus.MOUNTED.value,
    )

    alerts: list[dict] = []
    for tool in mounted:
        used = int(tool["used_count"]) + int(qty)
        limit = int(tool["life_limit"])
        ratio = used / limit if limit else 0.0
        expired = used >= limit
        await db.execute(
            f"UPDATE {T_TOOLS} SET used_count = $1, status = $2, updated_at = now() WHERE tool_id = $3",
            used, ToolStatus.EXPIRED.value if expired else ToolStatus.MOUNTED.value, tool["tool_id"],
        )
        if expired or ratio >= float(tool["warning_ratio"] or 0.9):
            alerts.append({
                "tool_id": tool["tool_id"],
                "tool_type": tool["tool_type"],
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
    async with db.transaction():
        old = await get_tool(db, old_tool_id)
        eq_id = old["eq_id"]
        if not eq_id:
            raise StateError(f"治具 {old_tool_id} 並未掛載於任何設備")

        await unmount(db, old_tool_id, actor, remark=remark)
        new_tool = await mount(db, new_tool_id, eq_id, actor, remark=f"更換 {old_tool_id}")
        eq = await equipment_service.get_equipment(db, eq_id)
        if eq["current_state"] == EquipmentState.SCHEDULED_DOWN.value:
            await equipment_service.set_state(
                db, eq_id, EquipmentState.STANDBY, actor, "TOOL_REPLACED", f"已換上 {new_tool_id}"
            )
        return {"equipment": eq_id, "removed": old_tool_id, "installed": new_tool}


def _decorate(row: dict) -> dict:
    limit = int(row.get("life_limit") or 0)
    used = int(row.get("used_count") or 0)
    row["usage_ratio"] = round(used / limit, 4) if limit else 0.0
    row["remaining"] = max(0, limit - used)
    return row


async def list_tools(db, status: str | None = None, eq_id: str | None = None, limit: int = 200) -> list[dict]:
    rows = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_TOOLS}
        WHERE ($1::text IS NULL OR status = $1) AND ($2::text IS NULL OR eq_id = $2)
        ORDER BY tool_id LIMIT $3
        """,
        status, eq_id, limit,
    )
    return [_decorate(r) for r in rows]


async def attention_list(db) -> list[dict]:
    """需要處理的治具：已到期或接近壽命。"""
    rows = await fetch_all(
        db,
        f"""
        SELECT * FROM {T_TOOLS}
        WHERE status = ANY($1::text[])
          AND life_limit > 0
          AND used_count::float / life_limit >= warning_ratio
        ORDER BY used_count::float / life_limit DESC
        """,
        [ToolStatus.EXPIRED.value, ToolStatus.MOUNTED.value],
    )
    return [_decorate(r) for r in rows]


async def tool_logs(db, tool_id: str, limit: int = 100) -> list[dict]:
    return await fetch_all(
        db,
        f"SELECT * FROM {T_TOOL_LOGS} WHERE tool_id = $1 ORDER BY timestamp DESC, id DESC LIMIT $2",
        tool_id, limit,
    )
