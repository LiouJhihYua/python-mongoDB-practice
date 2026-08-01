"""模擬產線跑批，產生可觀察的示範資料。

以離散時間推進（可注入時鐘），讓批號真的排隊、上機、產出良率與不良，
藉此把 WIP、良率、OEE、Q-Time、追溯等報表一次餵滿。

用法：
    python -m scripts.simulate --days 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import (  # noqa: E402
    COL_DEVICES,
    COL_EQUIPMENTS,
    COL_LOTS,
    COL_OPERATIONS,
    COL_USERS,
    COL_WAFERS,
    COL_WORK_ORDERS,
    connect_db,
)
from app.errors import MESError  # noqa: E402
from app.models import base as base_models  # noqa: E402
from app.models.enums import EquipmentState, LotStatus, WorkOrderStatus  # noqa: E402
from app.services import (  # noqa: E402
    equipment_service,
    lot_service,
    master_service,
    workorder_service,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("simulate")

TICK_SECONDS = 600  # 每次推進 10 分鐘
TARGET_WIP_LOTS = 16  # 維持的在製批數，讓站別之間自然形成佇列

DEFECTS_BY_OP: dict[str, list[str]] = {
    "WFR_SAW": ["SAW-CHIP", "SAW-CRACK"],
    "DIE_ATTACH": ["DA-VOID", "DA-TILT", "DA-SHIFT"],
    "WIRE_BOND": ["WB-NSOP", "WB-SHORT", "WB-SAG", "WB-LIFT"],
    "MOLD": ["MD-VOID", "MD-FLASH", "MD-CRACK"],
    "LASER_MARK": ["MK-ILLEG"],
    "BALL_MOUNT": ["BM-MISS"],
    "SINGULATION": ["SG-BURR"],
    "VISUAL_INSP": ["VI-SCRATCH", "VI-CONTAM"],
    "FT": ["FT-OPEN", "FT-SHORT", "FT-FUNC", "FT-LEAK", "FT-SPEED"],
}
REJECT_RATE: dict[str, float] = {
    "WFR_SAW": 0.004, "DIE_ATTACH": 0.006, "WIRE_BOND": 0.009, "MOLD": 0.005,
    "LASER_MARK": 0.002, "BALL_MOUNT": 0.003, "SINGULATION": 0.004,
    "VISUAL_INSP": 0.006, "FT": 0.028,
}
#: FT 不良代碼對應的 Bin 編號
FT_BIN = {"FT-OPEN": 2, "FT-SHORT": 3, "FT-FUNC": 4, "FT-LEAK": 5, "FT-SPEED": 6}


class SimClock:
    """可推進的模擬時鐘。"""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _users(db) -> dict[str, dict]:
    rows = await db[COL_USERS].find({}).to_list(length=None)
    return {u["username"]: u for u in rows}


def _pick_operator(users: dict[str, dict], op_code: str, rng: random.Random) -> dict:
    candidates = [u for u in users.values() if op_code in (u.get("certifications") or [])]
    return rng.choice(candidates) if candidates else users["eng01"]


async def _free_equipment(db, op_code: str, rng: random.Random) -> dict | None:
    rows = await db[COL_EQUIPMENTS].find(
        {
            "op_codes": op_code,
            "active": True,
            "current_lot_id": None,
            "current_state": {"$in": [EquipmentState.STANDBY.value, EquipmentState.PRODUCTIVE.value]},
        }
    ).to_list(length=None)
    return rng.choice(rows) if rows else None


def _split_rejects(reject: int, codes: list[str], rng: random.Random) -> list[dict]:
    """把不良數隨機拆給幾個不良代碼。"""
    if reject <= 0 or not codes:
        return []
    picked = rng.sample(codes, k=min(len(codes), rng.randint(1, min(3, len(codes)))))
    remaining, out = reject, []
    for code in picked[:-1]:
        take = rng.randint(0, remaining)
        if take:
            out.append({"defect_code": code, "qty": take})
            remaining -= take
    if remaining:
        out.append({"defect_code": picked[-1], "qty": remaining})
    return out


async def _create_work_orders(db, rng: random.Random, clock: SimClock, count: int) -> list[str]:
    devices = await db[COL_DEVICES].find({"active": True}).to_list(length=None)
    wo_nos = []
    for _ in range(count):
        device = rng.choice(devices)
        wo = await workorder_service.create_work_order(
            db,
            {
                "device_id": device["device_id"],
                "plan_qty": rng.choice([25, 50, 75]),
                "unit_type": "WAFER",
                "due_date": clock.now + timedelta(days=rng.randint(5, 20)),
                "priority": rng.choice([2, 3, 5, 5, 7]),
                "customer_po": f"PO{rng.randint(100000, 999999)}",
                "remark": "模擬資料",
            },
            "planner01",
        )
        await workorder_service.change_status(db, wo["wo_no"], WorkOrderStatus.RELEASED, "planner01")
        wo_nos.append(wo["wo_no"])
    return wo_nos


async def _openable_work_orders(db) -> list[str]:
    """尚有剩餘可投料量的工單。"""
    rows = await db[COL_WORK_ORDERS].find(
        {"status": {"$in": [WorkOrderStatus.RELEASED.value, WorkOrderStatus.IN_PROGRESS.value]}}
    ).to_list(length=None)
    return [r["wo_no"] for r in rows if int(r["plan_qty"]) > int(r.get("released_qty", 0))]


async def _replenish_wafers(db, device_id: str, need: int, rng: random.Random) -> None:
    """晶圓不足時模擬新的晶圓進料（每卡匣 25 片）。"""
    device = await db[COL_DEVICES].find_one({"device_id": device_id})
    prefix = device_id.split("-")[0]
    while await db[COL_WAFERS].count_documents(
        {"device_id": device_id, "consumed": {"$ne": True}}
    ) < need:
        existing = await db[COL_WAFERS].count_documents({"device_id": device_id})
        wafer_lot = f"W{prefix}{existing // 25 + 1:03d}"
        for slot in range(1, 26):
            cp_yield = rng.uniform(0.93, 0.995)
            try:
                await master_service.create_wafer(
                    db,
                    {
                        "wafer_id": f"{wafer_lot}-{slot:02d}",
                        "wafer_lot_id": wafer_lot,
                        "device_id": device_id,
                        "fab": "TSMC-F14",
                        "gross_die": device["gross_die_per_wafer"],
                        "cp_good_die": int(device["gross_die_per_wafer"] * cp_yield),
                        "cp_yield": None,
                    },
                    "planner01",
                )
            except MESError:
                pass


async def _open_lot(db, wo_no: str, rng: random.Random) -> dict | None:
    wo = await workorder_service.get_work_order(db, wo_no, raw=True)
    remaining = int(wo["plan_qty"]) - int(wo.get("released_qty", 0))
    if remaining <= 0:
        return None
    qty = min(remaining, rng.choice([12, 25, 25]))
    await _replenish_wafers(db, wo["device_id"], qty, rng)
    wafers = await db[COL_WAFERS].find(
        {"device_id": wo["device_id"], "consumed": {"$ne": True}}
    ).limit(qty).to_list(length=qty)
    if len(wafers) < qty:
        return None
    try:
        return await lot_service.create_lot(
            db,
            {
                "wo_no": wo_no,
                "qty": qty,
                "wafer_ids": [w["wafer_id"] for w in wafers],
                "carrier_id": f"MAG{rng.randint(100, 999)}",
                "remark": "",
            },
            "planner01",
        )
    except MESError:
        return None


async def _do_track_out(db, lot: dict, users: dict, rng: random.Random) -> None:
    operation = await db[COL_OPERATIONS].find_one({"op_code": lot["current_op"]})
    device = await db[COL_DEVICES].find_one({"device_id": lot["device_id"]})
    op_code = operation["op_code"]
    expected, _unit = lot_service.compute_expected_output(
        int(lot["qty"]), operation, device, lot["unit_type"]
    )
    rate = REJECT_RATE.get(op_code, 0.0) * rng.uniform(0.4, 2.2)
    reject = min(expected, int(expected * rate))
    good = expected - reject
    defects = _split_rejects(reject, DEFECTS_BY_OP.get(op_code, []), rng)

    payload: dict = {
        "lot_id": lot["lot_id"],
        "good_qty": good,
        "reject_qty": reject,
        "defects": defects,
        "materials": [],
        "bin_map": None,
        "remark": "",
    }
    if operation.get("is_test"):
        bin_map = {"1": good}
        for item in defects:
            bin_no = str(FT_BIN.get(item["defect_code"], 9))
            bin_map[bin_no] = bin_map.get(bin_no, 0) + item["qty"]
        payload["bin_map"] = bin_map
        payload["good_qty"] = None
    if op_code == "WIRE_BOND" and device.get("wire_per_unit"):
        wire = "WIRE-AU-08" if device["package_code"].startswith("BGA") else "WIRE-CU-10"
        payload["materials"] = [
            {"material_id": wire, "material_lot": f"{wire}-L{rng.randint(1, 4):02d}",
             "qty": round(expected * device["wire_per_unit"] * 0.0032, 2)}
        ]
    elif op_code == "DIE_ATTACH":
        pkg = device["package_code"]
        mat = {"QFN48": "LF-QFN48-01", "BGA256": "SUB-BGA256-01",
               "LQFP144": "LF-LQFP144-01", "DFN8": "LF-DFN8-01"}.get(pkg)
        if mat:
            payload["materials"] = [
                {"material_id": mat, "material_lot": f"{mat}-L{rng.randint(1, 3):02d}",
                 "qty": float(max(1, expected // max(1, device.get("units_per_strip", 1))))}
            ]

    operator = _pick_operator(users, op_code, rng)
    await lot_service.track_out(db, payload, operator)


async def simulate(days: int = 3, seed: int = 20250801) -> None:
    rng = random.Random(seed)
    db = await connect_db()

    if await db[COL_OPERATIONS].count_documents({}) == 0:
        log.error("尚未建立主檔，請先執行：python -m scripts.seed")
        return

    users = await _users(db)
    if "planner01" not in users:
        log.error("找不到示範帳號，請先執行：python -m scripts.seed")
        return

    end = datetime.now(timezone.utc)
    clock = SimClock(end - timedelta(days=days))
    # 種子晶圓是以「現在」建檔的，往前拉到模擬起點之前，避免出現「投入早於進料」
    await db[COL_WAFERS].update_many(
        {"consumed": {"$ne": True}}, {"$set": {"received_at": clock.now - timedelta(days=1)}}
    )
    base_models.set_clock(clock)
    try:
        await _run(db, users, rng, clock, end)
    finally:
        base_models.set_clock(None)


async def _run(db, users: dict, rng: random.Random, clock: SimClock, end: datetime) -> None:
    wo_nos = await _create_work_orders(db, rng, clock, 14)
    log.info("建立 %d 張工單", len(wo_nos))

    stats = {"track_in": 0, "track_out": 0, "holds": 0, "released": 0, "downs": 0, "shipped": 0}
    ticks = 0
    while clock.now < end:
        ticks += 1

        # 0) 每天下達新工單
        if ticks % 144 == 0:
            wo_nos += await _create_work_orders(db, rng, clock, 5)

        # 1) 維持目標在製水位，不足就投新料
        if ticks % 3 == 0:
            active = await db[COL_LOTS].count_documents(
                {"status": {"$in": [LotStatus.WAITING.value, LotStatus.RUNNING.value, LotStatus.HOLD.value]}}
            )
            if active < TARGET_WIP_LOTS:
                openable = await _openable_work_orders(db)
                for wo_no in rng.sample(openable, k=min(3, len(openable))):
                    if await _open_lot(db, wo_no, rng):
                        break

        # 2) 加工完成的批號出站
        running = await db[COL_LOTS].find({"status": LotStatus.RUNNING.value}).to_list(length=None)
        for lot in running:
            operation = await db[COL_OPERATIONS].find_one({"op_code": lot["current_op"]})
            std = float(operation.get("standard_cycle_time_sec") or 600)
            elapsed = (clock.now - base_models.ensure_aware(lot["track_in_at"])).total_seconds()
            if elapsed >= std * rng.uniform(0.85, 1.3):
                try:
                    await _do_track_out(db, lot, users, rng)
                    stats["track_out"] += 1
                except MESError as exc:
                    log.debug("出站失敗 %s：%s", lot["lot_id"], exc)

        # 3) 待進站的批號找機台上線（依優先序）
        waiting = await db[COL_LOTS].find({"status": LotStatus.WAITING.value}).sort(
            [("priority", 1), ("last_track_out_at", 1)]
        ).to_list(length=None)
        for lot in waiting:
            operation = await db[COL_OPERATIONS].find_one({"op_code": lot["current_op"]})
            eq_id = ""
            if operation.get("requires_equipment", True):
                eq = await _free_equipment(db, lot["current_op"], rng)
                if eq is None:
                    continue  # 沒機台 → 排隊，Q-Time 開始累積
                eq_id = eq["eq_id"]
            operator = _pick_operator(users, lot["current_op"], rng)
            try:
                await lot_service.track_in(
                    db, {"lot_id": lot["lot_id"], "eq_id": eq_id, "remark": ""}, operator
                )
                stats["track_in"] += 1
            except MESError:
                stats["holds"] += 1  # 多為 Q-Time 逾時自動扣留

        # 4) 品保處理扣留：兩小時後放行
        if ticks % 12 == 0:
            held = await db[COL_LOTS].find({"status": LotStatus.HOLD.value}).to_list(length=None)
            for lot in held:
                if rng.random() < 0.6:
                    try:
                        await lot_service.release_lot(
                            db, {"lot_id": lot["lot_id"], "remark": "品保確認無異常，放行"}, users["qc01"]
                        )
                        stats["released"] += 1
                    except MESError:
                        pass

        # 5) 設備隨機故障與修復
        if rng.random() < 0.05:
            eq = await _free_equipment(db, rng.choice(list(REJECT_RATE)), rng)
            if eq:
                await equipment_service.set_state(
                    db, eq["eq_id"], EquipmentState.UNSCHEDULED_DOWN, "eng01", "EQ-ALARM", "模擬設備異常"
                )
                stats["downs"] += 1
        if rng.random() < 0.35:
            down = await db[COL_EQUIPMENTS].find(
                {"current_state": EquipmentState.UNSCHEDULED_DOWN.value}
            ).to_list(length=None)
            for eq in down:
                if rng.random() < 0.4:
                    await equipment_service.set_state(
                        db, eq["eq_id"], EquipmentState.STANDBY, "eng01", "EQ-FIXED", "已排除"
                    )

        # 6) 完工批號出貨
        if ticks % 24 == 0:
            done = await db[COL_LOTS].find({"status": LotStatus.COMPLETED.value}).to_list(length=None)
            by_customer: dict[str, list[str]] = {}
            for lot in done:
                by_customer.setdefault(lot["customer_code"], []).append(lot["lot_id"])
            for customer, lot_ids in by_customer.items():
                if rng.random() < 0.35:
                    continue  # 保留部分完工批待出貨
                try:
                    await lot_service.ship_lots(
                        db,
                        {"customer_code": customer, "lot_ids": lot_ids, "customer_po": "", "remark": "模擬出貨"},
                        users["planner01"],
                    )
                    stats["shipped"] += len(lot_ids)
                except MESError:
                    pass

        clock.advance(TICK_SECONDS)

    # 收尾：示範拆批與併批，讓族譜追溯有東西可看
    await _demo_split_merge(db, users, stats)

    wip = await db[COL_LOTS].count_documents(
        {"status": {"$in": [LotStatus.WAITING.value, LotStatus.RUNNING.value, LotStatus.HOLD.value]}}
    )
    log.info(
        "模擬完成：進站 %d / 出站 %d / 自動扣留 %d / 放行 %d / 設備異常 %d / 出貨 %d 批；目前在製 %d 批",
        stats["track_in"], stats["track_out"], stats["holds"],
        stats["released"], stats["downs"], stats["shipped"], wip,
    )


async def _demo_split_merge(db, users: dict, stats: dict) -> None:
    waiting = await db[COL_LOTS].find(
        {"status": LotStatus.WAITING.value, "qty": {"$gte": 4}}
    ).to_list(length=None)
    if not waiting:
        return
    lot = waiting[0]
    half = int(lot["qty"]) // 2
    try:
        result = await lot_service.split_lot(
            db,
            {"lot_id": lot["lot_id"], "quantities": [half, int(lot["qty"]) - half], "reason": "急單插單分流"},
            users["planner01"],
        )
        log.info("示範拆批：%s → %s", lot["lot_id"], [c["lot_id"] for c in result["children"]])
    except MESError as exc:
        log.debug("拆批略過：%s", exc)

    # 找兩個同料號同站的批號併批
    pipeline = [
        {"$match": {"status": LotStatus.WAITING.value}},
        {"$group": {"_id": {"d": "$device_id", "s": "$current_seq", "u": "$unit_type"},
                    "lots": {"$push": "$lot_id"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 2}}},
    ]
    groups = await db[COL_LOTS].aggregate(pipeline).to_list(length=None)
    if groups:
        lot_ids = groups[0]["lots"][:2]
        try:
            merged = await lot_service.merge_lots(
                db, {"lot_ids": lot_ids, "carrier_id": "", "reason": "湊滿載盤"}, users["planner01"]
            )
            log.info("示範併批：%s → %s", lot_ids, merged["lot_id"])
        except MESError as exc:
            log.debug("併批略過：%s", exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="模擬 OSAT 產線生產")
    parser.add_argument("--days", type=int, default=3, help="模擬幾天份的生產（預設 3）")
    parser.add_argument("--seed", type=int, default=20250801, help="亂數種子")
    args = parser.parse_args()
    asyncio.run(simulate(args.days, args.seed))
