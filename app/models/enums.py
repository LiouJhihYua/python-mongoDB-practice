"""OSAT（封裝測試代工廠）領域用語與狀態定義。"""

from enum import StrEnum


class Role(StrEnum):
    """系統角色。"""

    ADMIN = "admin"  # 系統管理者
    PLANNER = "planner"  # 生管：工單、開批、拆併批
    OPERATOR = "operator"  # 線上作業員：進出站
    QC = "qc"  # 品保：Hold/Release、不良判定
    ENGINEER = "engineer"  # 製程／設備工程師：主檔、設備狀態、重工
    VIEWER = "viewer"  # 唯讀：報表看板


class OperationType(StrEnum):
    """站別類型。"""

    WAFER = "WAFER"  # 晶圓端作業（進料、貼片、切割）
    ASSEMBLY = "ASSEMBLY"  # 封裝作業（黏晶、打線、封膠…）
    TEST = "TEST"  # 測試（CP / FT / Burn-in）
    QC = "QC"  # 品檢站（抽檢、外觀檢）
    LOGISTICS = "LOGISTICS"  # 包裝、出貨


class UnitType(StrEnum):
    """批號當下的計量單位 —— OSAT 的關鍵：同一批在不同站單位不同。"""

    WAFER = "WAFER"  # 片
    DIE = "DIE"  # 顆（切割後的晶粒）
    STRIP = "STRIP"  # 條（基板／導線架）
    UNIT = "UNIT"  # 顆（切單後的成品）
    REEL = "REEL"  # 卷


class UnitTransform(StrEnum):
    """站別的單位換算方式，於 Track-Out 時套用。"""

    NONE = "NONE"  # 不換算（多數封裝站）
    WAFER_TO_DIE = "WAFER_TO_DIE"  # 切割：1 片 → device.gross_die_per_wafer 顆
    DIE_TO_UNIT = "DIE_TO_UNIT"  # 黏晶：晶粒上基板後改以「顆」計
    STRIP_TO_UNIT = "STRIP_TO_UNIT"  # 切單：1 條 → device.units_per_strip 顆
    UNIT_TO_REEL = "UNIT_TO_REEL"  # 編帶：device.units_per_reel 顆 → 1 卷（無條件進位）


class WorkOrderStatus(StrEnum):
    DRAFT = "DRAFT"  # 建立中
    RELEASED = "RELEASED"  # 已下達，可開批
    IN_PROGRESS = "IN_PROGRESS"  # 已有批號在線
    CLOSED = "CLOSED"  # 結案
    CANCELLED = "CANCELLED"  # 取消


class LotStatus(StrEnum):
    WAITING = "WAITING"  # 待進站（在站點佇列中）
    RUNNING = "RUNNING"  # 加工中（已 Track-In）
    HOLD = "HOLD"  # 扣留中
    COMPLETED = "COMPLETED"  # 全流程完成
    SCRAPPED = "SCRAPPED"  # 整批報廢
    SHIPPED = "SHIPPED"  # 已出貨
    MERGED = "MERGED"  # 已被併入其他批（歷史批）
    SPLIT = "SPLIT"  # 已拆分完畢（歷史批）


ACTIVE_LOT_STATUSES = frozenset(
    {LotStatus.WAITING, LotStatus.RUNNING, LotStatus.HOLD}
)


class LotAction(StrEnum):
    """批號履歷動作。"""

    CREATE = "CREATE"
    TRACK_IN = "TRACK_IN"
    TRACK_OUT = "TRACK_OUT"
    HOLD = "HOLD"
    RELEASE = "RELEASE"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    SCRAP = "SCRAP"
    REWORK = "REWORK"
    SHIP = "SHIP"


class EquipmentState(StrEnum):
    """SEMI E10 設備六大狀態。"""

    PRODUCTIVE = "PRODUCTIVE"  # 生產中
    STANDBY = "STANDBY"  # 待機（可用但無料）
    ENGINEERING = "ENGINEERING"  # 工程試作
    SCHEDULED_DOWN = "SCHEDULED_DOWN"  # 計畫停機（PM、換線）
    UNSCHEDULED_DOWN = "UNSCHEDULED_DOWN"  # 非計畫停機（故障）
    NON_SCHEDULED = "NON_SCHEDULED"  # 非排程時間（停工、無班）


#: 可接受 Track-In 的設備狀態
RUNNABLE_EQUIPMENT_STATES = frozenset(
    {EquipmentState.STANDBY, EquipmentState.PRODUCTIVE, EquipmentState.ENGINEERING}
)
#: OEE 稼動率分母排除的狀態（非排程時間不計入）
NON_SCHEDULED_STATES = frozenset({EquipmentState.NON_SCHEDULED})
#: 計為「設備可用」的狀態
UPTIME_STATES = frozenset(
    {EquipmentState.PRODUCTIVE, EquipmentState.STANDBY, EquipmentState.ENGINEERING}
)


class DefectCategory(StrEnum):
    """不良分類。"""

    ASSEMBLY = "ASSEMBLY"  # 封裝不良（推力不足、金線短路…）
    ELECTRICAL = "ELECTRICAL"  # 電性不良（測試 fail bin）
    VISUAL = "VISUAL"  # 外觀不良（崩角、印字不良）
    MATERIAL = "MATERIAL"  # 來料不良
    HANDLING = "HANDLING"  # 人為／搬運損傷


class DispositionType(StrEnum):
    """不良品處置。"""

    SCRAP = "SCRAP"  # 報廢
    REWORK = "REWORK"  # 重工
    USE_AS_IS = "USE_AS_IS"  # 特採
    RETEST = "RETEST"  # 重測


class HoldStatus(StrEnum):
    OPEN = "OPEN"
    RELEASED = "RELEASED"


class HoldReason(StrEnum):
    QUALITY = "QUALITY"  # 品質異常
    ENGINEERING = "ENGINEERING"  # 工程評估
    CUSTOMER = "CUSTOMER"  # 客戶要求
    MATERIAL = "MATERIAL"  # 材料問題
    QTIME = "QTIME"  # Q-Time 逾時
    EQUIPMENT = "EQUIPMENT"  # 設備異常


class MaterialType(StrEnum):
    """封裝主要耗材。"""

    SUBSTRATE = "SUBSTRATE"  # 基板
    LEADFRAME = "LEADFRAME"  # 導線架
    WIRE = "WIRE"  # 金線／銅線
    DIE_ATTACH = "DIE_ATTACH"  # 黏晶膠／膜
    MOLD_COMPOUND = "MOLD_COMPOUND"  # 封膠料
    SOLDER_BALL = "SOLDER_BALL"  # 錫球
    TAPE_REEL = "TAPE_REEL"  # 載帶／捲盤


class ToolType(StrEnum):
    """治具／耗材類型 —— OSAT 以「加工顆數」計算壽命的關鍵管理項目。"""

    CAPILLARY = "CAPILLARY"  # 打線毛細管
    WEDGE = "WEDGE"  # 劈刀
    BLADE = "BLADE"  # 切割刀
    COLLET = "COLLET"  # 吸嘴
    MOLD_CHASE = "MOLD_CHASE"  # 封膠模具
    TEST_SOCKET = "TEST_SOCKET"  # 測試座


class ToolStatus(StrEnum):
    IDLE = "IDLE"  # 在庫可用
    MOUNTED = "MOUNTED"  # 已上機
    EXPIRED = "EXPIRED"  # 壽命到期，待更換
    SCRAPPED = "SCRAPPED"  # 已報廢


class SPCRule(StrEnum):
    """統計製程管制的判異規則（Nelson rules 常用子集）。"""

    OUT_OF_SPEC = "OUT_OF_SPEC"  # 量測值超出規格上下限
    BEYOND_3SIGMA = "BEYOND_3SIGMA"  # 單點超出管制界限
    RUN_9_SAME_SIDE = "RUN_9_SAME_SIDE"  # 連續 9 點在中心線同側
    TREND_6 = "TREND_6"  # 連續 6 點持續上升或下降
    TWO_OF_THREE_2SIGMA = "TWO_OF_THREE_2SIGMA"  # 三點中有兩點落在同側 2σ 外


class PMStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    OVERDUE = "OVERDUE"


class ERPDocType(StrEnum):
    """ERP → MES 的下行單據類型。"""

    CUSTOMER = "CUSTOMER"  # 客戶主檔
    DEVICE = "DEVICE"  # 產品料號主檔
    MATERIAL = "MATERIAL"  # 材料主檔
    WORK_ORDER = "WORK_ORDER"  # 生產訂單


class ERPOutboundType(StrEnum):
    """MES → ERP 的上行單據類型。"""

    PRODUCTION_REPORT = "PRODUCTION_REPORT"  # 生產／完工回報
    MATERIAL_ISSUE = "MATERIAL_ISSUE"  # 材料領用（扣帳）
    SHIPMENT = "SHIPMENT"  # 出貨（開立發票依據）
    SCRAP = "SCRAP"  # 報廢（沖銷在製）


class ERPInboundStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class ERPOutboundStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    FAILED = "FAILED"


class SECSConnectionState(StrEnum):
    """HSMS 連線狀態（SEMI E37）。"""

    NOT_CONNECTED = "NOT_CONNECTED"
    CONNECTED = "CONNECTED"  # TCP 已通，尚未 Select
    SELECTED = "SELECTED"  # 已建立 HSMS Session，可收發資料訊息
    DISCONNECTED = "DISCONNECTED"


class SECSEventAction(StrEnum):
    """收到設備事件（CEID）後 MES 要做的事。"""

    EQ_STATE = "EQ_STATE"  # 更新設備 E10 狀態
    TRACK_OUT_READY = "TRACK_OUT_READY"  # 加工結束，提示可出站
    ALARM = "ALARM"  # 設備異常，轉非計畫停機
    RECIPE_LOADED = "RECIPE_LOADED"  # 機台回報載入的配方，寫回 MES 供進站比對
    LOG_ONLY = "LOG_ONLY"  # 只留紀錄
