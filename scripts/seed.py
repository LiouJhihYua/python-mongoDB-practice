"""建立一座 OSAT 廠的示範主檔。

用法：
    python -m scripts.seed            # 建立主檔（已存在則略過）
    python -m scripts.seed --reset    # 先清空資料庫再建立
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import connect_db, truncate_all  # noqa: E402
from app.errors import DuplicateError  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.models.enums import (  # noqa: E402
    DefectCategory,
    DispositionType,
    EquipmentState,
    MaterialType,
    OperationType,
    Role,
    ToolType,
    UnitTransform,
    UnitType,
)
from app.services import (  # noqa: E402
    equipment_service,
    master_service,
    spc_service,
    tool_service,
    user_service,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed")

ACTOR = "seed"

# ── 使用者 ──────────────────────────────────────────────────
USERS = [
    ("planner01", "王生管", "PLN001", "生產管理部", [Role.PLANNER], []),
    ("eng01", "李工程", "ENG001", "製程工程部", [Role.ENGINEER], []),
    ("qc01", "陳品保", "QC001", "品質保證部", [Role.QC], []),
    ("op001", "張作業", "OP001", "封裝一課", [Role.OPERATOR],
     ["WFR_RCV", "WFR_MOUNT", "WFR_SAW", "DIE_ATTACH", "WIRE_BOND"]),
    ("op002", "林作業", "OP002", "封裝二課", [Role.OPERATOR],
     ["MOLD", "PMC", "LASER_MARK", "BALL_MOUNT", "SINGULATION"]),
    ("op003", "黃作業", "OP003", "測試課", [Role.OPERATOR],
     ["VISUAL_INSP", "FT", "TAPE_REEL", "PACKING"]),
    ("viewer01", "訪客帳號", "VIS001", "管理部", [Role.VIEWER], []),
]

CUSTOMERS = [
    ("MTK", "聯發科技", "mtk-scm@example.com"),
    ("QCM", "高通半導體", "qcm-ops@example.com"),
    ("NXP", "恩智浦半導體", "nxp-buy@example.com"),
    ("RTK", "瑞昱半導體", "rtk-scm@example.com"),
]

PACKAGES = [
    ("QFN48", "QFN", 48, "7x7x0.85", "LEADFRAME"),
    ("BGA256", "BGA", 256, "17x17x1.4", "SUBSTRATE"),
    ("LQFP144", "QFP", 144, "20x20x1.4", "LEADFRAME"),
    ("DFN8", "DFN", 8, "3x3x0.75", "LEADFRAME"),
]

# op_code, 名稱, 類型, 換算, 產出單位, 需設備, 需資格, 標準工時(秒), Q-Time(分), 是否測試站
OPERATIONS = [
    ("WFR_RCV", "晶圓進料檢查", OperationType.WAFER, UnitTransform.NONE, UnitType.WAFER, False, False, 600, 0, False),
    ("WFR_MOUNT", "晶圓貼片", OperationType.WAFER, UnitTransform.NONE, UnitType.WAFER, True, True, 900, 0, False),
    ("WFR_SAW", "晶圓切割", OperationType.WAFER, UnitTransform.WAFER_TO_DIE, UnitType.DIE, True, True, 3600, 480, False),
    ("DIE_ATTACH", "黏晶", OperationType.ASSEMBLY, UnitTransform.DIE_TO_UNIT, UnitType.UNIT, True, True, 5400, 720, False),
    ("WIRE_BOND", "打線接合", OperationType.ASSEMBLY, UnitTransform.NONE, None, True, True, 7200, 480, False),
    ("MOLD", "封膠成型", OperationType.ASSEMBLY, UnitTransform.NONE, None, True, True, 2700, 240, False),
    ("PMC", "後固化烘烤", OperationType.ASSEMBLY, UnitTransform.NONE, None, True, False, 14400, 300, False),
    ("LASER_MARK", "雷射印字", OperationType.ASSEMBLY, UnitTransform.NONE, None, True, False, 1800, 0, False),
    ("BALL_MOUNT", "植球", OperationType.ASSEMBLY, UnitTransform.NONE, None, True, True, 3600, 0, False),
    ("SINGULATION", "切單", OperationType.ASSEMBLY, UnitTransform.NONE, None, True, True, 2400, 0, False),
    ("VISUAL_INSP", "外觀檢驗", OperationType.QC, UnitTransform.NONE, None, True, False, 1800, 0, False),
    ("FT", "最終測試", OperationType.TEST, UnitTransform.NONE, None, True, True, 9000, 0, True),
    ("TAPE_REEL", "編帶包裝", OperationType.LOGISTICS, UnitTransform.NONE, None, True, False, 2400, 0, False),
    ("PACKING", "裝箱入庫", OperationType.LOGISTICS, UnitTransform.NONE, None, False, False, 1200, 0, False),
]

ROUTES = {
    "RT-LF-STD": (
        "導線架標準流程（QFN / QFP / DFN）",
        "LEADFRAME",
        ["WFR_RCV", "WFR_MOUNT", "WFR_SAW", "DIE_ATTACH", "WIRE_BOND", "MOLD", "PMC",
         "LASER_MARK", "SINGULATION", "VISUAL_INSP", "FT", "TAPE_REEL", "PACKING"],
    ),
    "RT-BGA-STD": (
        "基板標準流程（BGA，含植球）",
        "SUBSTRATE",
        ["WFR_RCV", "WFR_MOUNT", "WFR_SAW", "DIE_ATTACH", "WIRE_BOND", "MOLD", "PMC",
         "LASER_MARK", "BALL_MOUNT", "SINGULATION", "VISUAL_INSP", "FT", "TAPE_REEL", "PACKING"],
    ),
}

# device_id, 客戶, 封裝, 流程, 每片晶粒, 每條顆數, 每卷顆數, 打線根數, 目標良率
DEVICES = [
    ("MT6893-QFN48", "MTK", "QFN48", "RT-LF-STD", 1250, 60, 4000, 48, 0.985),
    ("QC8550-BGA256", "QCM", "BGA256", "RT-BGA-STD", 480, 24, 1500, 256, 0.972),
    ("NX1120-LQFP144", "NXP", "LQFP144", "RT-LF-STD", 760, 40, 2500, 144, 0.978),
    ("RTL8168-DFN8", "RTK", "DFN8", "RT-LF-STD", 3200, 120, 8000, 8, 0.991),
]

# eq_id, 名稱, 型號, 區域, 可執行站別, 理論單位工時(秒)
EQUIPMENTS = [
    ("WM-01", "晶圓貼片機 01", "ASM AWM-3000", "WAFER", ["WFR_MOUNT"], 0.6),
    ("WM-02", "晶圓貼片機 02", "ASM AWM-3000", "WAFER", ["WFR_MOUNT"], 0.6),
    ("DS-01", "晶圓切割機 01", "DISCO DAD3350", "WAFER", ["WFR_SAW"], 2.4),
    ("DS-02", "晶圓切割機 02", "DISCO DAD3350", "WAFER", ["WFR_SAW"], 2.4),
    ("DB-01", "黏晶機 01", "ASM AD-8312", "ASSEMBLY-1", ["DIE_ATTACH"], 0.045),
    ("DB-02", "黏晶機 02", "ASM AD-8312", "ASSEMBLY-1", ["DIE_ATTACH"], 0.045),
    ("DB-03", "黏晶機 03", "Besi Datacon 2200", "ASSEMBLY-1", ["DIE_ATTACH"], 0.05),
    ("WB-01", "打線機 01", "K&S IConn Plus", "ASSEMBLY-1", ["WIRE_BOND"], 0.09),
    ("WB-02", "打線機 02", "K&S IConn Plus", "ASSEMBLY-1", ["WIRE_BOND"], 0.09),
    ("WB-03", "打線機 03", "K&S IConn Plus", "ASSEMBLY-1", ["WIRE_BOND"], 0.09),
    ("WB-04", "打線機 04", "ASM Eagle60", "ASSEMBLY-2", ["WIRE_BOND"], 0.095),
    ("WB-05", "打線機 05", "ASM Eagle60", "ASSEMBLY-2", ["WIRE_BOND"], 0.095),
    ("MD-01", "封膠機 01", "TOWA YPS-3000", "ASSEMBLY-2", ["MOLD"], 0.03),
    ("MD-02", "封膠機 02", "TOWA YPS-3000", "ASSEMBLY-2", ["MOLD"], 0.03),
    ("OV-01", "後固化烤箱 01", "Despatch LCC", "ASSEMBLY-2", ["PMC"], 0.16),
    ("OV-02", "後固化烤箱 02", "Despatch LCC", "ASSEMBLY-2", ["PMC"], 0.16),
    ("OV-03", "後固化烤箱 03", "Blue-M PCO", "ASSEMBLY-2", ["PMC"], 0.17),
    ("LM-01", "雷射印字機 01", "Coherent E-1000", "ASSEMBLY-2", ["LASER_MARK"], 0.02),
    ("BM-01", "植球機 01", "Shibuya BM-500", "ASSEMBLY-2", ["BALL_MOUNT"], 0.04),
    ("SG-01", "切單機 01", "DISCO DFD6362", "ASSEMBLY-2", ["SINGULATION"], 0.026),
    ("SG-02", "切單機 02", "DISCO DFD6362", "ASSEMBLY-2", ["SINGULATION"], 0.026),
    ("AOI-01", "外觀檢驗機 01", "Camtek Falcon", "TEST", ["VISUAL_INSP"], 0.02),
    ("FT-01", "測試機 01", "Advantest V93000", "TEST", ["FT"], 0.1),
    ("FT-02", "測試機 02", "Advantest V93000", "TEST", ["FT"], 0.1),
    ("FT-03", "測試機 03", "Teradyne UltraFLEX", "TEST", ["FT"], 0.11),
    ("TR-01", "編帶機 01", "Ismeca NY20", "TEST", ["TAPE_REEL"], 0.026),
]

# code, 名稱, 分類, 適用站別, 預設處置
DEFECT_CODES = [
    ("SAW-CHIP", "切割崩角", DefectCategory.VISUAL, ["WFR_SAW"], DispositionType.SCRAP),
    ("SAW-CRACK", "晶片裂痕", DefectCategory.VISUAL, ["WFR_SAW"], DispositionType.SCRAP),
    ("DA-VOID", "黏晶空洞", DefectCategory.ASSEMBLY, ["DIE_ATTACH"], DispositionType.SCRAP),
    ("DA-TILT", "晶片傾斜", DefectCategory.ASSEMBLY, ["DIE_ATTACH"], DispositionType.REWORK),
    ("DA-SHIFT", "晶片偏移", DefectCategory.ASSEMBLY, ["DIE_ATTACH"], DispositionType.REWORK),
    ("WB-NSOP", "銲線未附著（NSOP）", DefectCategory.ASSEMBLY, ["WIRE_BOND"], DispositionType.REWORK),
    ("WB-SHORT", "金線短路", DefectCategory.ASSEMBLY, ["WIRE_BOND"], DispositionType.SCRAP),
    ("WB-SAG", "線弧下垂", DefectCategory.ASSEMBLY, ["WIRE_BOND"], DispositionType.REWORK),
    ("WB-LIFT", "銲球脫落", DefectCategory.ASSEMBLY, ["WIRE_BOND"], DispositionType.SCRAP),
    ("MD-VOID", "封膠空洞", DefectCategory.ASSEMBLY, ["MOLD"], DispositionType.SCRAP),
    ("MD-FLASH", "溢膠", DefectCategory.VISUAL, ["MOLD"], DispositionType.REWORK),
    ("MD-CRACK", "封膠裂痕", DefectCategory.ASSEMBLY, ["MOLD"], DispositionType.SCRAP),
    ("MK-ILLEG", "印字不清", DefectCategory.VISUAL, ["LASER_MARK"], DispositionType.REWORK),
    ("BM-MISS", "缺球", DefectCategory.ASSEMBLY, ["BALL_MOUNT"], DispositionType.REWORK),
    ("SG-BURR", "切單毛邊", DefectCategory.VISUAL, ["SINGULATION"], DispositionType.SCRAP),
    ("VI-SCRATCH", "外觀刮傷", DefectCategory.VISUAL, ["VISUAL_INSP"], DispositionType.SCRAP),
    ("VI-CONTAM", "表面髒污", DefectCategory.VISUAL, ["VISUAL_INSP"], DispositionType.REWORK),
    ("FT-OPEN", "電性開路", DefectCategory.ELECTRICAL, ["FT"], DispositionType.SCRAP),
    ("FT-SHORT", "電性短路", DefectCategory.ELECTRICAL, ["FT"], DispositionType.SCRAP),
    ("FT-FUNC", "功能失效", DefectCategory.ELECTRICAL, ["FT"], DispositionType.RETEST),
    ("FT-LEAK", "漏電流超規", DefectCategory.ELECTRICAL, ["FT"], DispositionType.SCRAP),
    ("FT-SPEED", "速度不足", DefectCategory.ELECTRICAL, ["FT"], DispositionType.USE_AS_IS),
    ("MAT-NG", "來料不良", DefectCategory.MATERIAL, [], DispositionType.SCRAP),
    ("HND-DROP", "搬運掉落損傷", DefectCategory.HANDLING, [], DispositionType.SCRAP),
]

# item_code, 名稱, 站別, 單位, LSL, USL, target, 子群大小
MEASUREMENT_ITEMS = [
    ("SAW-KERF", "切割道寬度", "WFR_SAW", "um", 25.0, 45.0, None, 5),
    ("DA-BLT", "黏晶推力", "DIE_ATTACH", "gf", 500.0, None, None, 5),
    ("DA-THK", "黏晶膠厚", "DIE_ATTACH", "um", 15.0, 35.0, None, 5),
    ("WB-PULL", "銲線拉力", "WIRE_BOND", "gf", 3.0, 12.0, 7.0, 5),
    ("WB-BALL", "銲球直徑", "WIRE_BOND", "um", 50.0, 75.0, None, 5),
    ("MD-THK", "封膠厚度", "MOLD", "mm", 0.85, 0.95, None, 5),
    ("MK-DEPTH", "雷射印字深度", "LASER_MARK", "um", 8.0, 20.0, None, 3),
]

# 前綴, 類型, 名稱, 規格, 適用站別, 壽命（累計加工顆數）, 備品數量
TOOL_POOLS = [
    ("BLD", ToolType.BLADE, "切割刀", "NBC-ZH 205O-SE", ["WFR_SAW"], 300_000, 10),
    ("CLT", ToolType.COLLET, "黏晶吸嘴", "Rubber tip 3x3", ["DIE_ATTACH"], 800_000, 10),
    ("CAP", ToolType.CAPILLARY, "打線毛細管", "SU-1520-31-ZP38", ["WIRE_BOND"], 500_000, 16),
    ("SKT", ToolType.TEST_SOCKET, "測試座", "QFN/BGA universal", ["FT"], 400_000, 10),
]

# material_id, 名稱, 類型, 規格, 單位, 現有量, 安全庫存
MATERIALS = [
    ("LF-QFN48-01", "QFN48 導線架", MaterialType.LEADFRAME, "Cu, 60 units/strip", "PCS", 240000, 40000),
    ("SUB-BGA256-01", "BGA256 基板", MaterialType.SUBSTRATE, "4L, 24 units/strip", "PCS", 96000, 20000),
    ("LF-LQFP144-01", "LQFP144 導線架", MaterialType.LEADFRAME, "Cu, 40 units/strip", "PCS", 120000, 20000),
    ("LF-DFN8-01", "DFN8 導線架", MaterialType.LEADFRAME, "Cu, 120 units/strip", "PCS", 400000, 60000),
    ("WIRE-AU-08", "金線 0.8 mil", MaterialType.WIRE, "Au 99.99%, 0.8mil", "M", 500000, 80000),
    ("WIRE-CU-10", "銅線 1.0 mil", MaterialType.WIRE, "Cu coated Pd, 1.0mil", "M", 800000, 100000),
    ("DAF-A100", "黏晶膜", MaterialType.DIE_ATTACH, "DAF 20um", "PCS", 300000, 50000),
    ("EMC-G700", "封膠料", MaterialType.MOLD_COMPOUND, "Green EMC", "G", 1500000, 200000),
    ("BALL-SAC305", "錫球 SAC305", MaterialType.SOLDER_BALL, "0.35mm", "PCS", 5000000, 800000),
    ("TAPE-12MM", "載帶 12mm", MaterialType.TAPE_REEL, "12mm carrier tape", "M", 60000, 10000),
]


async def _try(coro, label: str) -> bool:
    try:
        await coro
        return True
    except DuplicateError:
        return False
    except Exception as exc:  # pragma: no cover
        log.error("建立 %s 失敗：%s", label, exc)
        return False


async def seed(reset: bool = False) -> None:
    pool = await connect_db()
    async with pool.acquire() as db:
        await _seed(db, reset)


async def _seed(db, reset: bool) -> None:
    if reset:
        await truncate_all(db)
        log.info("已清空資料庫")

    await user_service.ensure_bootstrap_admin(db)
    created = 0
    for username, full_name, emp, dept, roles, certs in USERS:
        ok = await _try(
            user_service.create_user(
                db,
                {
                    "username": username,
                    "password": f"{username}1234",
                    "full_name": full_name,
                    "employee_no": emp,
                    "department": dept,
                    "roles": [r.value for r in roles],
                    "certifications": certs,
                    "active": True,
                },
                ACTOR,
            ),
            username,
        )
        created += ok
    log.info("使用者：新增 %d / 共 %d", created, len(USERS))

    for code, name, email in CUSTOMERS:
        await _try(
            master_service.customers.create(
                db, {"code": code, "name": name, "contact": "", "email": email, "active": True}, ACTOR
            ),
            code,
        )
    log.info("客戶：%d 筆", len(CUSTOMERS))

    for code, family, leads, size, sub in PACKAGES:
        await _try(
            master_service.packages.create(
                db,
                {"package_code": code, "family": family, "lead_count": leads,
                 "body_size_mm": size, "substrate_type": sub, "active": True},
                ACTOR,
            ),
            code,
        )
    log.info("封裝型式：%d 筆", len(PACKAGES))

    for (op_code, name, op_type, transform, out_unit, need_eq, need_cert,
         cycle, qtime, is_test) in OPERATIONS:
        await _try(
            master_service.operations.create(
                db,
                {
                    "op_code": op_code,
                    "name": name,
                    "op_type": op_type.value,
                    "area": "",
                    "unit_transform": transform.value,
                    "output_unit": out_unit.value if out_unit else None,
                    "requires_equipment": need_eq,
                    "requires_certification": need_cert,
                    "standard_cycle_time_sec": cycle,
                    "max_queue_minutes": qtime,
                    "is_test": is_test,
                    "pass_bins": [1],
                    "sampling_rate": 1.0,
                    "allow_rework": True,
                    "active": True,
                },
                ACTOR,
            ),
            op_code,
        )
    log.info("站別：%d 筆", len(OPERATIONS))

    for route_code, (desc, family, op_codes) in ROUTES.items():
        await _try(
            master_service.create_route(
                db,
                {
                    "route_code": route_code,
                    "version": 1,
                    "description": desc,
                    "package_family": family,
                    "steps": [
                        {"seq": (i + 1) * 10, "op_code": code, "standard_yield": 0.998, "note": ""}
                        for i, code in enumerate(op_codes)
                    ],
                    "active": True,
                },
                ACTOR,
            ),
            route_code,
        )
    log.info("製程流程：%d 條", len(ROUTES))

    for device_id, cust, pkg, route, gross, per_strip, per_reel, wires, target in DEVICES:
        await _try(
            master_service.create_device(
                db,
                {
                    "device_id": device_id,
                    "description": f"{cust} {pkg} 產品",
                    "customer_code": cust,
                    "customer_device": device_id.split("-")[0],
                    "package_code": pkg,
                    "route_code": route,
                    "wafer_size_inch": 12,
                    "gross_die_per_wafer": gross,
                    "units_per_strip": per_strip,
                    "units_per_reel": per_reel,
                    "die_size_mm": "5.2x5.2",
                    "wire_per_unit": wires,
                    "target_yield": target,
                    "active": True,
                },
                ACTOR,
            ),
            device_id,
        )
    log.info("產品料號：%d 筆", len(DEVICES))

    for eq_id, name, model, area, op_codes, ct in EQUIPMENTS:
        if await _try(
            master_service.create_equipment(
                db,
                {
                    "eq_id": eq_id, "name": name, "model": model, "vendor": model.split()[0],
                    "area": area, "op_codes": op_codes, "ideal_cycle_time_sec": ct,
                    "pm_interval_days": 90, "active": True,
                },
                ACTOR,
            ),
            eq_id,
        ):
            await equipment_service.set_state(db, eq_id, EquipmentState.STANDBY, ACTOR, "開線")
    log.info("設備：%d 台", len(EQUIPMENTS))

    for code, name, category, op_codes, disp in DEFECT_CODES:
        await _try(
            master_service.create_defect_code(
                db,
                {"code": code, "name": name, "category": category.value, "op_codes": op_codes,
                 "default_disposition": disp.value, "active": True},
                ACTOR,
            ),
            code,
        )
    log.info("不良代碼：%d 筆", len(DEFECT_CODES))

    for mid, name, mtype, spec, uom, qty, safety in MATERIALS:
        await _try(
            master_service.materials.create(
                db,
                {"material_id": mid, "name": name, "material_type": mtype.value, "spec": spec,
                 "uom": uom, "on_hand_qty": qty, "safety_stock": safety, "vendor": "", "active": True},
                ACTOR,
            ),
            mid,
        )
    log.info("材料：%d 筆", len(MATERIALS))

    for item_code, name, op_code, unit, lsl, usl, target, n in MEASUREMENT_ITEMS:
        await _try(
            spc_service.create_item(
                db,
                {"item_code": item_code, "name": name, "op_code": op_code, "device_id": "",
                 "unit": unit, "lsl": lsl, "usl": usl, "target": target, "sample_size": n,
                 "auto_hold_on_violation": True, "active": True},
                ACTOR,
            ),
            item_code,
        )
    log.info("SPC 量測項目：%d 項", len(MEASUREMENT_ITEMS))

    tool_count = 0
    for prefix, tool_type, name, spec, op_codes, life, qty in TOOL_POOLS:
        for idx in range(1, qty + 1):
            tool_count += await _try(
                tool_service.create_tool(
                    db,
                    {"tool_id": f"{prefix}-{idx:03d}", "name": f"{name} {idx:02d}",
                     "tool_type": tool_type.value, "spec": spec, "op_codes": op_codes,
                     "life_limit": life, "warning_ratio": 0.85, "active": True},
                    ACTOR,
                ),
                prefix,
            )
    log.info("治具：新增 %d 支", tool_count)

    rng = random.Random(20250801)
    wafer_count = 0
    for device_id, _, _, _, gross, *_rest in DEVICES:
        for lot_no in range(1, 7):
            wafer_lot = f"W{device_id.split('-')[0]}{lot_no:03d}"
            for slot in range(1, 26):
                cp_yield = rng.uniform(0.93, 0.995)
                ok = await _try(
                    master_service.create_wafer(
                        db,
                        {
                            "wafer_id": f"{wafer_lot}-{slot:02d}",
                            "wafer_lot_id": wafer_lot,
                            "device_id": device_id,
                            "fab": "TSMC-F14",
                            "gross_die": gross,
                            "cp_good_die": int(gross * cp_yield),
                            "cp_yield": None,
                        },
                        ACTOR,
                    ),
                    wafer_lot,
                )
                wafer_count += ok
    log.info("晶圓：新增 %d 片", wafer_count)

    await _seed_wafer_maps(db, rng)
    await _seed_sops(db)
    await _seed_secs(db)
    await _seed_erp(db)
    await _seed_recipes(db)
    await _seed_sampling(db)
    await _seed_carriers(db)
    log.info("主檔建立完成。預設帳號：admin / admin1234，其餘帳號密碼為「帳號+1234」")


# ── 晶圓 Map ────────────────────────────────────────────────
def make_cp_map(gross_die: int, rng: random.Random, cp_yield: float) -> list[list[int]]:
    """依理論粒數產生一張像樣的 CP map。

    刻意做出 OSAT 現場真的會看到的兩種樣態：邊緣良率偏低，
    以及一塊群聚不良 —— 這樣分析頁一打開就有東西可看。
    """
    size = max(8, int((gross_die / 0.785) ** 0.5))  # 圓形面積約佔外接正方形 78.5%
    radius, center = size / 2 - 0.5, (size - 1) / 2
    grid: list[list[int]] = []
    for y in range(size):
        row: list[int] = []
        for x in range(size):
            distance = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
            if distance > radius:
                row.append(-1)
                continue
            # 越靠邊緣越容易掛，這是晶圓廠的常態
            edge_penalty = max(0.0, (distance / radius - 0.82)) * 0.6
            if rng.random() < (1 - cp_yield) + edge_penalty:
                row.append(rng.choice([2, 2, 3, 4, 5]))
            else:
                row.append(1)
        grid.append(row)

    if rng.random() < 0.35:  # 三成的晶圓種一塊群聚不良
        cx, cy = rng.randint(4, size - 6), rng.randint(4, size - 6)
        for y in range(cy, min(cy + rng.randint(3, 5), size)):
            for x in range(cx, min(cx + rng.randint(3, 5), size)):
                if grid[y][x] != -1:
                    grid[y][x] = 7
    return grid


async def _seed_wafer_maps(db, rng: random.Random) -> None:
    """為第一個晶圓母批上傳 CP map（全部上傳太慢，示範用足夠）。"""
    from app.services import wafermap_service

    count = 0
    for device_id, _, _, _, gross, *_rest in DEVICES:
        wafer_lot = f"W{device_id.split('-')[0]}001"
        wafers = await db.fetch(
            "SELECT wafer_id, gross_die FROM wafers WHERE wafer_lot_id = $1 ORDER BY wafer_id LIMIT 6",
            wafer_lot,
        )
        for wafer in wafers:
            grid = make_cp_map(int(wafer["gross_die"]), rng, rng.uniform(0.93, 0.99))
            count += await _try(
                wafermap_service.upload_map(
                    db,
                    {"wafer_id": wafer["wafer_id"], "source": "CP", "grid": grid,
                     "pass_bins": [1], "null_bin": -1, "update_wafer": True, "remark": "示範資料"},
                    ACTOR,
                ),
                wafer["wafer_id"],
            )
    log.info("晶圓 Map：新增 %d 張", count)


# ── e-SOP ───────────────────────────────────────────────────
SOPS = [
    ("SOP-WB-001", "打線接合標準作業", "WIRE_BOND",
     [("確認機台完成暖機並取得當日首件核可", "首件拉力 ≧ 3.0 gf"),
      ("依工單核對治具編號與毛細管壽命", "毛細管累計顆數未達 85%"),
      ("裝載 Magazine，確認方向標記朝上", "料號與工單一致"),
      ("每 30 分鐘抽測銲線拉力並登錄 SPC", "落在管制界限內"),
      ("結批前確認機台無異常告警", "無 Alarm")],
     "高溫夾治具、金線靜電", ["防靜電衣", "防靜電手環", "手套"]),
    ("SOP-SAW-001", "晶圓切割標準作業", "WFR_SAW",
     [("確認切割刀已完成 dressing", "刀具跳動 < 5 um"),
      ("設定切割道寬度並試切三刀", "切割道寬度 25–45 um"),
      ("確認冷卻水流量與水質", "流量 ≧ 1.0 L/min"),
      ("每片檢查崩角情形", "崩角 < 30 um")],
     "高速旋轉刀具、切削水", ["護目鏡", "手套"]),
    ("SOP-FT-001", "最終測試標準作業", "FT",
     [("確認測試程式版本與工單一致", "程式版本正確"),
      ("執行 Correlation 樣品並比對結果", "與標準片誤差 < 2%"),
      ("確認測試座壽命未超標", "測試座累計顆數未達上限"),
      ("結批前輸出 Bin 分佈並上傳 MES", "Bin 合計等於進站量")],
     "高溫測試座", ["防靜電衣", "手套"]),
]


async def _seed_sops(db) -> None:
    from app.services import sop_service

    created = 0
    for sop_code, title, op_code, steps, hazards, ppe in SOPS:
        ok = await _try(
            sop_service.create_sop(
                db,
                {
                    "sop_code": sop_code, "title": title, "op_code": op_code, "device_id": "",
                    "summary": f"{op_code} 站標準作業程序",
                    "steps": [
                        {"seq": i, "instruction": text, "detail": "", "image": "",
                         "checkpoint": check, "duration_sec": 60}
                        for i, (text, check) in enumerate(steps, start=1)
                    ],
                    "hazards": hazards, "ppe": ppe, "attachments": [], "require_ack": True,
                },
                ACTOR,
            ),
            sop_code,
        )
        if ok:
            # 生效日往前壓，示範資料才不會出現「SOP 還沒生效就開始生產」
            await _try(
                sop_service.release_sop(
                    db, sop_code, 1, {"effective_from": utcnow() - timedelta(days=90)}, ACTOR
                ),
                sop_code,
            )
            created += 1

    # 打線站示範「未簽認就不能進站」的管制
    await db.execute("UPDATE operations SET require_sop_ack = TRUE WHERE op_code = 'WIRE_BOND'")
    for username in ("op001", "eng01"):
        await _try(
            sop_service.acknowledge(db, {"sop_code": "SOP-WB-001", "version": 1}, username), username
        )
    log.info("e-SOP：新增 %d 份（打線站已開啟簽認管制）", created)


# ── SECS/GEM ────────────────────────────────────────────────
#: eq_id, HSMS 埠
SECS_LINKS = [("WB-01", 5001), ("WB-02", 5002), ("DB-01", 5003), ("FT-01", 5004)]

#: ceid, 名稱, 動作, 參數
SECS_RULES = [
    (2001, "開始加工", "EQ_STATE", {"state": "PRODUCTIVE"}),
    (2002, "加工結束", "TRACK_OUT_READY", {}),
    (2003, "設備狀態變更", "EQ_STATE", {"state_vid": "1.0"}),
    (2004, "設備異常", "ALARM", {"remark": "設備回報異常"}),
    (2005, "換料完成", "LOG_ONLY", {}),
]


async def _seed_secs(db) -> None:
    from app.services import secs_service

    for eq_id, port in SECS_LINKS:
        await _try(
            secs_service.upsert_link(
                db, eq_id,
                {"host": "127.0.0.1", "port": port, "session_id": 0, "mode": "ACTIVE",
                 "enabled": False},  # 預設不自動連線，避免沒有機台時一直重試
                ACTOR,
            ),
            eq_id,
        )
    for ceid, name, action, params in SECS_RULES:
        await _try(
            secs_service.upsert_rule(
                db, {"eq_id": "", "ceid": ceid, "name": name, "action": action,
                     "params": params, "enabled": True},
                ACTOR,
            ),
            f"CEID {ceid}",
        )
    log.info("SECS/GEM：連線設定 %d 台、事件規則 %d 條（連線預設停用）",
             len(SECS_LINKS), len(SECS_RULES))


# ── 配方 ────────────────────────────────────────────────────
#: 站別 → 配方參數樣板；實際數值依機型與料號調整
RECIPE_TEMPLATES = {
    "WFR_SAW": {"刀具轉速rpm": 30000, "進給速度mm_s": 50, "切割深度um": 80, "冷卻水流量L_min": 1.2},
    "DIE_ATTACH": {"銲頭溫度C": 150, "停留時間ms": 120, "壓力gf": 300, "膠量mg": 1.5},
    "WIRE_BOND": {"銲頭溫度C": 175, "超音波功率mW": 95, "壓力gf": 25, "停留時間ms": 12},
    "FT": {"測試溫度C": 25, "測試程式版本": "v3.2", "接觸壓力gf": 200, "重測次數": 1},
}


async def _seed_recipes(db) -> None:
    """為每個料號 × 關鍵站別建立一支核可配方，並開啟打線站的配方比對。"""
    from app.services import recipe_service

    created = 0
    for device_id, *_rest in DEVICES:
        short = device_id.split("-")[0]
        for op_code, params in RECIPE_TEMPLATES.items():
            ppid = f"{op_code[:2]}-{short}"
            ok = await _try(
                recipe_service.create_recipe(
                    db,
                    {
                        "ppid": ppid, "name": f"{device_id} {op_code} 配方", "op_code": op_code,
                        "device_id": device_id, "eq_model": "",
                        "parameters": params,
                        # 校驗碼在實機上由機台回報；示範資料留空表示不比對
                        "checksum": "", "remark": "示範配方",
                    },
                    ACTOR,
                ),
                ppid,
            )
            if ok:
                await _try(
                    recipe_service.release_recipe(
                        db, ppid, 1, {"effective_from": utcnow() - timedelta(days=90)}, ACTOR
                    ),
                    ppid,
                )
                created += 1

    # 打線站示範「配方沒對上就不能進站」的管制
    await db.execute("UPDATE operations SET require_recipe_check = TRUE WHERE op_code = 'WIRE_BOND'")
    log.info("配方：新增 %d 支（打線站已開啟配方比對）", created)


# ── 抽樣檢驗 ────────────────────────────────────────────────
#: 外觀檢的示範抽樣計畫。
#: 這是「看起來合理」的示範值，不是任何標準的權威版本 ——
#: 正式使用前務必依與客戶談定的計畫調整級距與 Ac/Re。
VI_SAMPLING_LEVELS = [
    {"lot_size_from": 1, "lot_size_to": 500, "code_letter": "H",
     "sample_size": 50, "accept_number": 1, "reject_number": 2},
    {"lot_size_from": 501, "lot_size_to": 3_200, "code_letter": "K",
     "sample_size": 125, "accept_number": 3, "reject_number": 4},
    {"lot_size_from": 3_201, "lot_size_to": 35_000, "code_letter": "M",
     "sample_size": 315, "accept_number": 7, "reject_number": 8},
    {"lot_size_from": 35_001, "lot_size_to": None, "code_letter": "N",
     "sample_size": 500, "accept_number": 10, "reject_number": 11},
]


async def _seed_sampling(db) -> None:
    from app.services import sampling_service

    ok = await _try(
        sampling_service.create_plan(
            db,
            {
                "plan_code": "VI-AQL10", "name": "外觀檢 AQL 1.0 抽樣", "op_code": "VISUAL_INSP",
                "device_id": "", "customer_code": "", "plan_type": "AQL",
                "lot_interval": 1, "aql": 1.0, "inspection_level": "II",
                "levels": VI_SAMPLING_LEVELS,
                "remark": "示範用；正式使用前請依客戶合約調整", "active": True,
            },
            ACTOR,
        ),
        "VI-AQL10",
    )
    await db.execute(
        "UPDATE operations SET require_sampling_decision = TRUE WHERE op_code = 'VISUAL_INSP'"
    )
    log.info("抽樣計畫：%s（外觀檢已開啟抽檢決策）", "已建立" if ok else "已存在")


# ── 載具 ────────────────────────────────────────────────────
#: 前綴, 類型, 容量, 清洗週期（使用次數）, 數量
CARRIER_POOLS = [
    ("MAG", "MAGAZINE", 25, 40, 30),
    ("BOAT", "BOAT", 12, 25, 12),
    ("TRAY", "TRAY", 50, 60, 16),
]


async def _seed_carriers(db) -> None:
    from app.services import carrier_service

    count = 0
    for prefix, carrier_type, capacity, clean_interval, qty in CARRIER_POOLS:
        for index in range(1, qty + 1):
            count += await _try(
                carrier_service.create_carrier(
                    db,
                    {
                        "carrier_id": f"{prefix}-{index:03d}", "carrier_type": carrier_type,
                        "capacity": capacity, "location": "ASSY", "clean_interval": clean_interval,
                        "remark": "", "active": True,
                    },
                    ACTOR,
                ),
                prefix,
            )
    log.info("載具：新增 %d 個", count)


# ── ERP 介接 ────────────────────────────────────────────────
async def _seed_erp(db) -> None:
    """放一筆待處理的下行單據，讓介接頁面一打開就有東西可操作。"""
    from app.services import erp_service

    await _try(
        erp_service.receive(
            db,
            {
                "doc_type": "CUSTOMER", "external_id": "ERP-CUST-DEMO",
                "payload": {"code": "AMD", "name": "超微半導體", "contact": "Lisa",
                            "email": "amd-scm@example.com"},
                "source": "REST",
            },
            ACTOR,
        ),
        "ERP-CUST-DEMO",
    )
    log.info("ERP：已放入 1 筆待處理的下行單據")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="建立 OSAT MES 示範主檔")
    parser.add_argument("--reset", action="store_true", help="先清空資料庫")
    args = parser.parse_args()
    asyncio.run(seed(args.reset))
