"""治具壽命管理：上下機、出站累計、到期停機與換刀。"""

import pytest

from app.errors import StateError, ValidationError
from app.models.enums import EquipmentState, ToolStatus
from app.services import lot_service, tool_service
from tests.helpers import advance_to, make_lot, run_step


async def _lot_running_at_wire_bond(db, users, qty: int = 1):
    lot = await make_lot(db, qty=qty)
    await advance_to(db, users, lot["lot_id"], "WIRE_BOND")
    await lot_service.track_in(db, {"lot_id": lot["lot_id"], "eq_id": "WB-01"}, users["op001"])
    return lot["lot_id"]


async def _track_out_all(db, users, lot_id: str) -> dict:
    current = await lot_service.get_lot(db, lot_id, raw=True)
    return await lot_service.track_out(
        db,
        {"lot_id": lot_id, "good_qty": current["qty"], "reject_qty": 0, "defects": [],
         "materials": [], "bin_map": None, "remark": ""},
        users["op001"],
    )


async def test_mount_and_unmount(factory):
    db = factory
    mounted = await tool_service.mount(db, "CAP-001", "WB-01", "eng01", "開線")
    assert mounted["status"] == ToolStatus.MOUNTED.value
    assert mounted["eq_id"] == "WB-01"
    assert mounted["mount_count"] == 1

    removed = await tool_service.unmount(db, "CAP-001", "eng01")
    assert removed["status"] == ToolStatus.IDLE.value
    assert removed["eq_id"] is None

    logs = await tool_service.tool_logs(db, "CAP-001")
    assert {log["action"] for log in logs} == {"MOUNT", "UNMOUNT"}


async def test_mount_checks_equipment_capability(factory):
    db = factory
    with pytest.raises(ValidationError, match="不符"):
        await tool_service.mount(db, "CAP-001", "DS-01", "eng01")  # 毛細管裝到切割機


async def test_cannot_double_mount(factory):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "eng01")
    with pytest.raises(StateError, match="已掛載"):
        await tool_service.mount(db, "CAP-001", "WB-01", "eng01")


async def test_unmount_requires_mounted(factory):
    db = factory
    with pytest.raises(StateError, match="未掛載"):
        await tool_service.unmount(db, "CAP-001", "eng01")


async def test_usage_accumulates_on_track_out(factory, users):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "eng01")
    lot_id = await _lot_running_at_wire_bond(db, users, qty=1)  # 1 片 → 1000 顆
    result = await _track_out_all(db, users, lot_id)

    assert result["tool_alerts"] == []  # 1000 / 10000 尚未到警戒
    tool = await tool_service.get_tool(db, "CAP-001")
    assert tool["used_count"] == 1000
    assert tool["status"] == ToolStatus.MOUNTED.value


async def test_warning_before_expiry(factory, users):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "eng01")
    await db.execute("UPDATE tools SET used_count = 7500 WHERE tool_id = 'CAP-001'")
    lot_id = await _lot_running_at_wire_bond(db, users, qty=1)
    result = await _track_out_all(db, users, lot_id)

    assert len(result["tool_alerts"]) == 1
    alert = result["tool_alerts"][0]
    assert alert["expired"] is False
    assert alert["usage_ratio"] == pytest.approx(0.85)
    assert "請準備更換" in alert["message"]
    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'WB-01'")
    assert eq["current_state"] == EquipmentState.STANDBY.value  # 尚未停機


async def test_expiry_stops_equipment(factory, users):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "eng01")
    await db.execute("UPDATE tools SET used_count = 9500 WHERE tool_id = 'CAP-001'")
    lot_id = await _lot_running_at_wire_bond(db, users, qty=1)
    result = await _track_out_all(db, users, lot_id)

    alert = result["tool_alerts"][0]
    assert alert["expired"] is True
    assert "停機待換" in alert["message"]

    tool = await tool_service.get_tool(db, "CAP-001")
    assert tool["status"] == ToolStatus.EXPIRED.value
    assert tool["used_count"] == 10500

    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'WB-01'")
    assert eq["current_state"] == EquipmentState.SCHEDULED_DOWN.value
    assert eq["state_reason"] == "TOOL_EXPIRED"
    assert "CAP-001" in eq["state_remark"]


async def test_expired_equipment_blocks_track_in(factory, users):
    """超壽命的機台不該再接料。"""
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "eng01")
    await db.execute("UPDATE tools SET used_count = 9999 WHERE tool_id = 'CAP-001'")
    lot_id = await _lot_running_at_wire_bond(db, users, qty=1)
    await _track_out_all(db, users, lot_id)

    other = await make_lot(db, qty=1)
    await advance_to(db, users, other["lot_id"], "WIRE_BOND")
    with pytest.raises(StateError, match="不可投料"):
        await lot_service.track_in(db, {"lot_id": other["lot_id"], "eq_id": "WB-01"}, users["op001"])


async def test_replace_expired_tool_restores_equipment(factory, users):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "eng01")
    await db.execute("UPDATE tools SET used_count = 9999 WHERE tool_id = 'CAP-001'")
    lot_id = await _lot_running_at_wire_bond(db, users, qty=1)
    await _track_out_all(db, users, lot_id)

    result = await tool_service.replace(db, "CAP-001", "CAP-002", "eng01", "壽命到期")
    assert result["equipment"] == "WB-01"
    assert result["installed"]["tool_id"] == "CAP-002"
    assert result["installed"]["status"] == ToolStatus.MOUNTED.value

    old = await tool_service.get_tool(db, "CAP-001")
    assert old["status"] == ToolStatus.SCRAPPED.value  # 到期治具直接報廢，不會被誤用
    assert old["eq_id"] is None

    eq = await db.fetchrow("SELECT * FROM equipments WHERE eq_id = 'WB-01'")
    assert eq["current_state"] == EquipmentState.STANDBY.value


async def test_expired_tool_cannot_be_remounted(factory):
    db = factory
    await db.execute(
        "UPDATE tools SET status = $1 WHERE tool_id = 'CAP-001'", ToolStatus.EXPIRED.value
    )
    with pytest.raises(StateError, match="不可上機"):
        await tool_service.mount(db, "CAP-001", "WB-01", "eng01")


async def test_attention_list(factory, users):
    db = factory
    await tool_service.mount(db, "CAP-001", "WB-01", "eng01")
    await db.execute("UPDATE tools SET used_count = 9000 WHERE tool_id = 'CAP-001'")
    watch = await tool_service.attention_list(db)
    assert [t["tool_id"] for t in watch] == ["CAP-001"]
    assert watch[0]["usage_ratio"] == pytest.approx(0.9)
    assert watch[0]["remaining"] == 1000


async def test_tools_without_mount_are_untouched(factory, users):
    """沒上機的治具不該被出站量計入。"""
    db = factory
    lot = await make_lot(db, qty=1)
    await run_step(db, users, lot["lot_id"])
    await run_step(db, users, lot["lot_id"], "DS-01")
    assert (await tool_service.get_tool(db, "BLD-001"))["used_count"] == 0
