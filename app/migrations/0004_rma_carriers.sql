-- ══════════════════════════════════════════════════════════
--  客訴（RMA）與 8D
--
--  追溯的「能力」原本就有，但沒有把手 —— 客訴進來時沒有單可開、
--  沒有地方記錄圈選出來的受影響範圍、也沒有 8D 的進度。
--  這張表把客訴從收件到結案串成一條可稽核的流程。
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS complaints (
    complaint_no     TEXT PRIMARY KEY,
    customer_code    TEXT NOT NULL REFERENCES customers(code),
    customer_ref     TEXT NOT NULL DEFAULT '',        -- 客戶端的客訴單號
    device_id        TEXT NOT NULL DEFAULT '',
    lot_ids          TEXT[] NOT NULL DEFAULT '{}',    -- 客戶申告的批號
    unit_seqs        INTEGER[] NOT NULL DEFAULT '{}', -- 退回品的成品序號（有 die 綁定才填得出來）
    qty              INTEGER NOT NULL DEFAULT 0,
    severity         TEXT NOT NULL DEFAULT 'MINOR',   -- CRITICAL / MAJOR / MINOR
    category         TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'OPEN',    -- OPEN / INVESTIGATING / ACTION / CLOSED / REJECTED
    owner            TEXT NOT NULL DEFAULT '',
    due_date         TIMESTAMPTZ,
    -- 8D 各步驟的內容與完成時間，逐步填寫
    d8               JSONB NOT NULL DEFAULT '{}',
    impact           JSONB NOT NULL DEFAULT '{}',     -- 圈選出來的受影響批號／出貨單／晶圓
    root_cause       TEXT NOT NULL DEFAULT '',
    corrective_action TEXT NOT NULL DEFAULT '',
    closed_at        TIMESTAMPTZ,
    closed_by        TEXT,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT NOT NULL DEFAULT 'system',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       TEXT
);
CREATE INDEX IF NOT EXISTS ix_complaints_status ON complaints (status, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_complaints_customer ON complaints (customer_code, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_complaints_lots ON complaints USING GIN (lot_ids);

-- 客訴處理歷程：誰在什麼時候做了什麼，稽核時要能還原整條時間軸
CREATE TABLE IF NOT EXISTS complaint_events (
    id           BIGSERIAL PRIMARY KEY,
    complaint_no TEXT NOT NULL REFERENCES complaints(complaint_no) ON DELETE CASCADE,
    step         TEXT NOT NULL DEFAULT '',            -- D1..D8 或 STATUS / NOTE
    action       TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL DEFAULT '',
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cevents_no ON complaint_events (complaint_no, timestamp);

-- ══════════════════════════════════════════════════════════
--  載具管理（Magazine / Boat / Tray / FOUP）
--
--  原本 lots.carrier_id 只是一個自由文字欄位，擋不住「同一個
--  Magazine 同時掛兩批」這種現場實際會發生的錯誤。
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS carriers (
    carrier_id     TEXT PRIMARY KEY,
    carrier_type   TEXT NOT NULL DEFAULT 'MAGAZINE',  -- MAGAZINE / BOAT / TRAY / FOUP / REEL_BOX
    capacity       INTEGER NOT NULL DEFAULT 0,        -- 0 = 不限
    status         TEXT NOT NULL DEFAULT 'EMPTY',     -- EMPTY / IN_USE / DIRTY / MAINTENANCE / SCRAPPED
    current_lot_id TEXT,
    location       TEXT NOT NULL DEFAULT '',
    use_count      INTEGER NOT NULL DEFAULT 0,        -- 累計使用次數，達上限要清洗
    clean_interval INTEGER NOT NULL DEFAULT 0,        -- 0 = 不管制
    last_cleaned_at TIMESTAMPTZ,
    remark         TEXT NOT NULL DEFAULT '',
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL DEFAULT 'system',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT
);
CREATE INDEX IF NOT EXISTS ix_carriers_status ON carriers (status, carrier_type);

-- 一個載具同時只能掛一個批號：以部分唯一索引在資料庫層擋下來，
-- 不是靠應用程式記得檢查
CREATE UNIQUE INDEX IF NOT EXISTS ux_carrier_lot
    ON carriers (current_lot_id) WHERE current_lot_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS carrier_logs (
    id         BIGSERIAL PRIMARY KEY,
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    action     TEXT NOT NULL,                         -- ASSIGN / RELEASE / CLEAN / MAINTAIN / SCRAP
    lot_id     TEXT,
    status     TEXT NOT NULL DEFAULT '',
    remark     TEXT NOT NULL DEFAULT '',
    operator   TEXT NOT NULL DEFAULT '',
    timestamp  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_clogs_carrier ON carrier_logs (carrier_id, timestamp DESC);
