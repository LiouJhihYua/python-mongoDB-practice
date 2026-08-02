-- ══════════════════════════════════════════════════════════
--  抽樣檢驗（Sampling / AQL）
--
--  站別主檔原本就有 sampling_rate 欄位，但沒有任何邏輯讀它 ——
--  設了「抽檢 10%」其實還是全檢。這組資料表把抽樣真正做完：
--  抽樣計畫決定「這一批要不要驗、驗幾顆、幾顆不良就退」，
--  檢驗紀錄則留下每一次的判定依據。
--
--  各家客戶談定的抽樣計畫都不一樣，因此允收水準是「資料」而不是
--  寫死在程式裡的常數 —— 品保可以依合約自行維護級距。
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sampling_plans (
    plan_code     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    op_code       TEXT NOT NULL REFERENCES operations(op_code),
    device_id     TEXT NOT NULL DEFAULT '',          -- 空字串 = 全料號適用
    customer_code TEXT NOT NULL DEFAULT '',          -- 空字串 = 全客戶適用
    plan_type     TEXT NOT NULL DEFAULT 'AQL',       -- FULL（全檢）/ SKIP_LOT（跳批）/ AQL（抽樣）
    lot_interval  INTEGER NOT NULL DEFAULT 1 CHECK (lot_interval > 0), -- 每 N 批驗 1 批
    aql           DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    inspection_level TEXT NOT NULL DEFAULT 'II',
    remark        TEXT NOT NULL DEFAULT '',
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT NOT NULL DEFAULT 'system',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT
);
CREATE INDEX IF NOT EXISTS ix_splan_op ON sampling_plans (op_code, device_id, active);

-- 抽樣計畫的批量級距：批量落在哪一段 → 抽幾顆、允收幾顆、拒收幾顆
CREATE TABLE IF NOT EXISTS sampling_levels (
    id            BIGSERIAL PRIMARY KEY,
    plan_code     TEXT NOT NULL REFERENCES sampling_plans(plan_code) ON DELETE CASCADE,
    lot_size_from BIGINT NOT NULL CHECK (lot_size_from >= 0),
    lot_size_to   BIGINT,                            -- NULL = 沒有上限
    code_letter   TEXT NOT NULL DEFAULT '',          -- Z1.4 樣本大小代字，僅供對照
    sample_size   INTEGER NOT NULL CHECK (sample_size > 0),
    accept_number INTEGER NOT NULL CHECK (accept_number >= 0),
    reject_number INTEGER NOT NULL CHECK (reject_number > 0),
    UNIQUE (plan_code, lot_size_from),
    CONSTRAINT ck_splevel_range CHECK (lot_size_to IS NULL OR lot_size_to >= lot_size_from),
    CONSTRAINT ck_splevel_acre CHECK (reject_number > accept_number)
);
CREATE INDEX IF NOT EXISTS ix_splevel_plan ON sampling_levels (plan_code, lot_size_from);

-- 每一批的檢驗紀錄：驗了沒、抽幾顆、判允收還是拒收
CREATE TABLE IF NOT EXISTS inspection_records (
    id            BIGSERIAL PRIMARY KEY,
    lot_id        TEXT NOT NULL,
    wo_no         TEXT,
    device_id     TEXT NOT NULL DEFAULT '',
    customer_code TEXT NOT NULL DEFAULT '',
    op_code       TEXT NOT NULL,
    seq           INTEGER,
    eq_id         TEXT,
    plan_code     TEXT,
    decision      TEXT NOT NULL,                     -- FULL / SAMPLED / SKIPPED
    lot_size      BIGINT NOT NULL DEFAULT 0,
    sample_size   INTEGER NOT NULL DEFAULT 0,
    accept_number INTEGER,
    reject_number INTEGER,
    defect_found  INTEGER,
    result        TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / ACCEPT / REJECT / NOT_REQUIRED
    defects       JSONB NOT NULL DEFAULT '[]',
    remark        TEXT NOT NULL DEFAULT '',
    inspector     TEXT NOT NULL DEFAULT '',
    timestamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
    shift         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_insp_lot ON inspection_records (lot_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_insp_op_time ON inspection_records (op_code, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_insp_result ON inspection_records (result, timestamp DESC);

-- 站別是否要在進站時做抽檢決策
ALTER TABLE operations ADD COLUMN IF NOT EXISTS require_sampling_decision BOOLEAN NOT NULL DEFAULT FALSE;
