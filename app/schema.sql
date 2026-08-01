-- OSAT MES 資料庫結構（PostgreSQL）
-- 全部使用 IF NOT EXISTS，啟動時可重複執行。

-- ── 連號產生器（工單／批號／出貨單）─────────────────────────
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    seq BIGINT NOT NULL DEFAULT 0
);

-- ── 使用者 ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    full_name       TEXT NOT NULL DEFAULT '',
    employee_no     TEXT NOT NULL DEFAULT '',
    department      TEXT NOT NULL DEFAULT '',
    roles           TEXT[] NOT NULL DEFAULT '{}',
    certifications  TEXT[] NOT NULL DEFAULT '{}',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT NOT NULL DEFAULT 'system',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT
);

-- ── 主檔 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    contact    TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL DEFAULT 'system',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS packages (
    package_code   TEXT PRIMARY KEY,
    family         TEXT NOT NULL DEFAULT '',
    lead_count     INTEGER NOT NULL CHECK (lead_count > 0),
    body_size_mm   TEXT NOT NULL DEFAULT '',
    substrate_type TEXT NOT NULL DEFAULT '',
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL DEFAULT 'system',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT
);

CREATE TABLE IF NOT EXISTS operations (
    op_code                 TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    op_type                 TEXT NOT NULL DEFAULT 'ASSEMBLY',
    area                    TEXT NOT NULL DEFAULT '',
    unit_transform          TEXT NOT NULL DEFAULT 'NONE',
    output_unit             TEXT,
    requires_equipment      BOOLEAN NOT NULL DEFAULT TRUE,
    requires_certification  BOOLEAN NOT NULL DEFAULT FALSE,
    standard_cycle_time_sec INTEGER NOT NULL DEFAULT 0,
    max_queue_minutes       INTEGER NOT NULL DEFAULT 0,
    is_test                 BOOLEAN NOT NULL DEFAULT FALSE,
    pass_bins               INTEGER[] NOT NULL DEFAULT '{1}',
    sampling_rate           DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    allow_rework            BOOLEAN NOT NULL DEFAULT TRUE,
    active                  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by              TEXT NOT NULL DEFAULT 'system',
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by              TEXT
);

CREATE TABLE IF NOT EXISTS routes (
    route_code     TEXT NOT NULL,
    version        INTEGER NOT NULL CHECK (version > 0),
    description    TEXT NOT NULL DEFAULT '',
    package_family TEXT NOT NULL DEFAULT '',
    steps          JSONB NOT NULL,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL DEFAULT 'system',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT,
    PRIMARY KEY (route_code, version)
);

CREATE TABLE IF NOT EXISTS devices (
    device_id           TEXT PRIMARY KEY,
    description         TEXT NOT NULL DEFAULT '',
    customer_code       TEXT NOT NULL REFERENCES customers(code),
    customer_device     TEXT NOT NULL DEFAULT '',
    package_code        TEXT NOT NULL REFERENCES packages(package_code),
    route_code          TEXT NOT NULL,
    wafer_size_inch     INTEGER NOT NULL DEFAULT 12,
    gross_die_per_wafer INTEGER NOT NULL CHECK (gross_die_per_wafer > 0),
    units_per_strip     INTEGER NOT NULL DEFAULT 1 CHECK (units_per_strip > 0),
    units_per_reel      INTEGER NOT NULL DEFAULT 1 CHECK (units_per_reel > 0),
    die_size_mm         TEXT NOT NULL DEFAULT '',
    wire_per_unit       INTEGER NOT NULL DEFAULT 0,
    target_yield        DOUBLE PRECISION NOT NULL DEFAULT 0.98,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          TEXT NOT NULL DEFAULT 'system',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by          TEXT
);
CREATE INDEX IF NOT EXISTS ix_devices_customer ON devices (customer_code);

CREATE TABLE IF NOT EXISTS equipments (
    eq_id                TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    model                TEXT NOT NULL DEFAULT '',
    vendor               TEXT NOT NULL DEFAULT '',
    area                 TEXT NOT NULL DEFAULT '',
    op_codes             TEXT[] NOT NULL DEFAULT '{}',
    ideal_cycle_time_sec DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (ideal_cycle_time_sec > 0),
    installed_at         TIMESTAMPTZ,
    pm_interval_days     INTEGER NOT NULL DEFAULT 90,
    current_state        TEXT NOT NULL DEFAULT 'NON_SCHEDULED',
    state_since          TIMESTAMPTZ NOT NULL DEFAULT now(),
    state_reason         TEXT NOT NULL DEFAULT '',
    state_remark         TEXT NOT NULL DEFAULT '',
    current_lot_id       TEXT,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by           TEXT NOT NULL DEFAULT 'system',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by           TEXT
);
CREATE INDEX IF NOT EXISTS ix_equipments_area_state ON equipments (area, current_state);
CREATE INDEX IF NOT EXISTS ix_equipments_op_codes ON equipments USING GIN (op_codes);

CREATE TABLE IF NOT EXISTS defect_codes (
    code                TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    category            TEXT NOT NULL DEFAULT 'ASSEMBLY',
    op_codes            TEXT[] NOT NULL DEFAULT '{}',
    default_disposition TEXT NOT NULL DEFAULT 'SCRAP',
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          TEXT NOT NULL DEFAULT 'system',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by          TEXT
);

CREATE TABLE IF NOT EXISTS materials (
    material_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    material_type TEXT NOT NULL,
    spec          TEXT NOT NULL DEFAULT '',
    uom           TEXT NOT NULL DEFAULT 'PCS',
    on_hand_qty   DOUBLE PRECISION NOT NULL DEFAULT 0,
    safety_stock  DOUBLE PRECISION NOT NULL DEFAULT 0,
    vendor        TEXT NOT NULL DEFAULT '',
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT NOT NULL DEFAULT 'system',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT
);

CREATE TABLE IF NOT EXISTS wafers (
    wafer_id        TEXT PRIMARY KEY,
    wafer_lot_id    TEXT NOT NULL,
    device_id       TEXT NOT NULL REFERENCES devices(device_id),
    fab             TEXT NOT NULL DEFAULT '',
    gross_die       INTEGER NOT NULL CHECK (gross_die > 0),
    cp_good_die     INTEGER NOT NULL DEFAULT 0,
    cp_yield        DOUBLE PRECISION,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    assembly_lot_id TEXT,
    consumed        BOOLEAN NOT NULL DEFAULT FALSE,
    consumed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT NOT NULL DEFAULT 'system',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT,
    CONSTRAINT ck_wafer_cp CHECK (cp_good_die <= gross_die)
);
CREATE INDEX IF NOT EXISTS ix_wafers_device ON wafers (device_id, consumed);
CREATE INDEX IF NOT EXISTS ix_wafers_asm_lot ON wafers (assembly_lot_id);

-- ── 工單與批號 ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS work_orders (
    wo_no         TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL REFERENCES devices(device_id),
    customer_code TEXT NOT NULL,
    package_code  TEXT NOT NULL,
    route_code    TEXT NOT NULL,
    plan_qty      INTEGER NOT NULL CHECK (plan_qty > 0),
    unit_type     TEXT NOT NULL DEFAULT 'WAFER',
    due_date      TIMESTAMPTZ NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 5,
    customer_po   TEXT NOT NULL DEFAULT '',
    remark        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'DRAFT',
    released_qty  INTEGER NOT NULL DEFAULT 0,
    lot_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT NOT NULL DEFAULT 'system',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT,
    CONSTRAINT ck_wo_released CHECK (released_qty >= 0 AND released_qty <= plan_qty)
);
CREATE INDEX IF NOT EXISTS ix_wo_status_due ON work_orders (status, due_date);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_no   TEXT PRIMARY KEY,
    customer_code TEXT NOT NULL,
    customer_po   TEXT NOT NULL DEFAULT '',
    lot_ids       TEXT[] NOT NULL DEFAULT '{}',
    device_ids    TEXT[] NOT NULL DEFAULT '{}',
    total_qty     BIGINT NOT NULL DEFAULT 0,
    remark        TEXT NOT NULL DEFAULT '',
    shipped_by    TEXT NOT NULL,
    shipped_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_shipments_lots ON shipments USING GIN (lot_ids);

CREATE TABLE IF NOT EXISTS lots (
    lot_id            TEXT PRIMARY KEY,
    wo_no             TEXT NOT NULL REFERENCES work_orders(wo_no),
    device_id         TEXT NOT NULL REFERENCES devices(device_id),
    customer_code     TEXT NOT NULL,
    package_code      TEXT NOT NULL,
    route_code        TEXT NOT NULL,
    route_version     INTEGER NOT NULL DEFAULT 1,
    current_seq       INTEGER NOT NULL,
    current_op        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'WAITING',
    qty               BIGINT NOT NULL DEFAULT 0 CHECK (qty >= 0),
    initial_qty       BIGINT NOT NULL DEFAULT 0,
    unit_type         TEXT NOT NULL DEFAULT 'WAFER',
    scrap_qty         BIGINT NOT NULL DEFAULT 0,
    carrier_id        TEXT NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 5,
    parent_lot_id     TEXT,
    child_lot_ids     TEXT[] NOT NULL DEFAULT '{}',
    merged_from       TEXT[] NOT NULL DEFAULT '{}',
    merged_into       TEXT,
    wafer_ids         TEXT[] NOT NULL DEFAULT '{}',
    eq_id             TEXT,
    operator          TEXT,
    track_in_at       TIMESTAMPTZ,
    last_track_out_at TIMESTAMPTZ,
    qtime_violations  INTEGER NOT NULL DEFAULT 0,
    qtime_waived_seq  INTEGER,
    rework_count      INTEGER NOT NULL DEFAULT 0,
    shipment_no       TEXT,
    remark            TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by        TEXT NOT NULL DEFAULT 'system',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_lots_status_op ON lots (status, current_op);
CREATE INDEX IF NOT EXISTS ix_lots_wo ON lots (wo_no);
CREATE INDEX IF NOT EXISTS ix_lots_device ON lots (device_id);
CREATE INDEX IF NOT EXISTS ix_lots_parent ON lots (parent_lot_id);
CREATE INDEX IF NOT EXISTS ix_lots_wafers ON lots USING GIN (wafer_ids);

CREATE TABLE IF NOT EXISTS lot_history (
    id                      BIGSERIAL PRIMARY KEY,
    lot_id                  TEXT NOT NULL,
    wo_no                   TEXT,
    device_id               TEXT,
    customer_code           TEXT,
    route_code              TEXT,
    seq                     INTEGER,
    op_code                 TEXT,
    action                  TEXT NOT NULL,
    operator                TEXT NOT NULL DEFAULT '',
    timestamp               TIMESTAMPTZ NOT NULL DEFAULT now(),
    shift                   TEXT NOT NULL DEFAULT '',
    eq_id                   TEXT,
    qty_in                  BIGINT NOT NULL DEFAULT 0,
    qty_expected            BIGINT NOT NULL DEFAULT 0,
    qty_good                BIGINT NOT NULL DEFAULT 0,
    qty_reject              BIGINT NOT NULL DEFAULT 0,
    unit_in                 TEXT,
    unit_out                TEXT,
    process_sec             DOUBLE PRECISION NOT NULL DEFAULT 0,
    queue_sec               DOUBLE PRECISION NOT NULL DEFAULT 0,
    qtime_limit_min         INTEGER NOT NULL DEFAULT 0,
    qtime_violation         BOOLEAN NOT NULL DEFAULT FALSE,
    step_yield              DOUBLE PRECISION,
    standard_cycle_time_sec INTEGER NOT NULL DEFAULT 0,
    defects                 JSONB NOT NULL DEFAULT '[]',
    bin_map                 JSONB,
    materials               JSONB NOT NULL DEFAULT '[]',
    remark                  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_hist_lot_time ON lot_history (lot_id, timestamp);
CREATE INDEX IF NOT EXISTS ix_hist_op_time ON lot_history (op_code, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_hist_eq_time ON lot_history (eq_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_hist_action_time ON lot_history (action, timestamp DESC);

-- ── 品質 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS holds (
    id                 BIGSERIAL PRIMARY KEY,
    lot_id             TEXT NOT NULL,
    wo_no              TEXT,
    device_id          TEXT,
    op_code            TEXT,
    seq                INTEGER,
    reason             TEXT NOT NULL DEFAULT 'QUALITY',
    remark             TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'OPEN',
    held_by            TEXT NOT NULL,
    held_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_by        TEXT,
    released_at        TIMESTAMPTZ,
    release_remark     TEXT NOT NULL DEFAULT '',
    status_before_hold TEXT
);
CREATE INDEX IF NOT EXISTS ix_holds_lot_status ON holds (lot_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_holds_open_lot ON holds (lot_id) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS defect_records (
    id                  BIGSERIAL PRIMARY KEY,
    lot_id              TEXT NOT NULL,
    wo_no               TEXT,
    device_id           TEXT,
    customer_code       TEXT,
    op_code             TEXT NOT NULL,
    seq                 INTEGER,
    eq_id               TEXT,
    defect_code         TEXT NOT NULL,
    defect_name         TEXT NOT NULL DEFAULT '',
    category            TEXT NOT NULL DEFAULT '',
    disposition         TEXT NOT NULL DEFAULT 'SCRAP',
    qty                 BIGINT NOT NULL CHECK (qty > 0),
    remark              TEXT NOT NULL DEFAULT '',
    operator            TEXT NOT NULL DEFAULT '',
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT now(),
    shift               TEXT NOT NULL DEFAULT '',
    dispositioned_by    TEXT,
    dispositioned_at    TIMESTAMPTZ,
    disposition_remark  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_defrec_lot ON defect_records (lot_id);
CREATE INDEX IF NOT EXISTS ix_defrec_code_time ON defect_records (defect_code, timestamp DESC);

-- ── SPC ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS measurement_items (
    item_code              TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    op_code                TEXT NOT NULL REFERENCES operations(op_code),
    device_id              TEXT NOT NULL DEFAULT '',
    unit                   TEXT NOT NULL DEFAULT '',
    usl                    DOUBLE PRECISION,
    lsl                    DOUBLE PRECISION,
    target                 DOUBLE PRECISION,
    sample_size            INTEGER NOT NULL DEFAULT 5,
    auto_hold_on_violation BOOLEAN NOT NULL DEFAULT TRUE,
    control_limits         JSONB,
    active                 BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by             TEXT NOT NULL DEFAULT 'system',
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by             TEXT,
    CONSTRAINT ck_item_spec CHECK (usl IS NOT NULL OR lsl IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_items_op ON measurement_items (op_code);

CREATE TABLE IF NOT EXISTS measurements (
    id                 BIGSERIAL PRIMARY KEY,
    item_code          TEXT NOT NULL REFERENCES measurement_items(item_code),
    item_name          TEXT NOT NULL DEFAULT '',
    lot_id             TEXT NOT NULL,
    device_id          TEXT,
    op_code            TEXT NOT NULL,
    eq_id              TEXT,
    operator           TEXT NOT NULL DEFAULT '',
    values             DOUBLE PRECISION[] NOT NULL,
    mean               DOUBLE PRECISION NOT NULL,
    range              DOUBLE PRECISION NOT NULL,
    min                DOUBLE PRECISION NOT NULL,
    max                DOUBLE PRECISION NOT NULL,
    stdev              DOUBLE PRECISION NOT NULL DEFAULT 0,
    out_of_spec        BOOLEAN NOT NULL DEFAULT FALSE,
    out_of_spec_values DOUBLE PRECISION[] NOT NULL DEFAULT '{}',
    violations         TEXT[] NOT NULL DEFAULT '{}',
    remark             TEXT NOT NULL DEFAULT '',
    timestamp          TIMESTAMPTZ NOT NULL DEFAULT now(),
    shift              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_meas_item_time ON measurements (item_code, timestamp);
CREATE INDEX IF NOT EXISTS ix_meas_lot ON measurements (lot_id);
CREATE INDEX IF NOT EXISTS ix_meas_eq_time ON measurements (eq_id, timestamp DESC);

-- ── 設備管理 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS equipment_state_logs (
    id           BIGSERIAL PRIMARY KEY,
    eq_id        TEXT NOT NULL REFERENCES equipments(eq_id),
    state        TEXT NOT NULL,
    reason_code  TEXT NOT NULL DEFAULT '',
    remark       TEXT NOT NULL DEFAULT '',
    lot_id       TEXT,
    operator     TEXT NOT NULL DEFAULT '',
    start_time   TIMESTAMPTZ NOT NULL DEFAULT now(),
    end_time     TIMESTAMPTZ,
    duration_sec DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_eqlog_eq_time ON equipment_state_logs (eq_id, start_time DESC);

CREATE TABLE IF NOT EXISTS pm_tasks (
    id           BIGSERIAL PRIMARY KEY,
    eq_id        TEXT NOT NULL REFERENCES equipments(eq_id),
    pm_type      TEXT NOT NULL,
    due_date     TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PLANNED',
    remark       TEXT NOT NULL DEFAULT '',
    done_at      TIMESTAMPTZ,
    performed_by TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS ix_pm_eq_due ON pm_tasks (eq_id, due_date);

-- ── 治具 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tools (
    tool_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    tool_type     TEXT NOT NULL,
    spec          TEXT NOT NULL DEFAULT '',
    op_codes      TEXT[] NOT NULL DEFAULT '{}',
    life_limit    BIGINT NOT NULL CHECK (life_limit > 0),
    warning_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.9,
    status        TEXT NOT NULL DEFAULT 'IDLE',
    eq_id         TEXT,
    used_count    BIGINT NOT NULL DEFAULT 0,
    mounted_at    TIMESTAMPTZ,
    mount_count   INTEGER NOT NULL DEFAULT 0,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT NOT NULL DEFAULT 'system',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT
);
CREATE INDEX IF NOT EXISTS ix_tools_eq_status ON tools (eq_id, status);
CREATE INDEX IF NOT EXISTS ix_tools_op_codes ON tools USING GIN (op_codes);

CREATE TABLE IF NOT EXISTS tool_logs (
    id          BIGSERIAL PRIMARY KEY,
    tool_id     TEXT NOT NULL,
    tool_type   TEXT,
    eq_id       TEXT,
    action      TEXT NOT NULL,
    used_count  BIGINT NOT NULL DEFAULT 0,
    life_limit  BIGINT,
    usage_ratio DOUBLE PRECISION,
    lot_id      TEXT,
    operator    TEXT NOT NULL DEFAULT '',
    remark      TEXT NOT NULL DEFAULT '',
    result      TEXT,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_toollog_tool_time ON tool_logs (tool_id, timestamp DESC);

-- ── 材料異動 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS material_transactions (
    id           BIGSERIAL PRIMARY KEY,
    material_id  TEXT NOT NULL REFERENCES materials(material_id),
    material_lot TEXT NOT NULL DEFAULT '',
    txn_type     TEXT NOT NULL,
    qty          DOUBLE PRECISION NOT NULL,
    lot_id       TEXT,
    device_id    TEXT,
    op_code      TEXT,
    operator     TEXT NOT NULL DEFAULT '',
    remark       TEXT NOT NULL DEFAULT '',
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT now(),
    shift        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_mtxn_lot ON material_transactions (lot_id);
CREATE INDEX IF NOT EXISTS ix_mtxn_mat_time ON material_transactions (material_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_mtxn_lotno ON material_transactions (material_lot);

-- ── 稽核 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    method      TEXT,
    path        TEXT,
    query       TEXT,
    status_code INTEGER,
    success     BOOLEAN NOT NULL DEFAULT TRUE,
    duration_ms DOUBLE PRECISION,
    action      TEXT,
    table_name  TEXT,
    key         TEXT,
    fields      TEXT[],
    payload     JSONB,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    shift       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_time ON audit_logs (timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_audit_actor_time ON audit_logs (actor, timestamp DESC);

-- ══════════════════════════════════════════════════════════
--  晶圓 Map 與 Die 級追溯
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS wafer_maps (
    wafer_id    TEXT PRIMARY KEY REFERENCES wafers(wafer_id),
    source      TEXT NOT NULL DEFAULT 'CP',            -- CP / AOI / FT
    rows        INTEGER NOT NULL CHECK (rows > 0),
    cols        INTEGER NOT NULL CHECK (cols > 0),
    origin      TEXT NOT NULL DEFAULT 'UPPER_LEFT',
    notch       TEXT NOT NULL DEFAULT 'DOWN',
    null_bin    INTEGER NOT NULL DEFAULT -1,           -- 晶圓外／無晶粒的位置
    pass_bins   INTEGER[] NOT NULL DEFAULT '{1}',
    die_count   INTEGER NOT NULL DEFAULT 0,            -- 實際存在的晶粒數
    pass_count  INTEGER NOT NULL DEFAULT 0,
    bin_counts  JSONB NOT NULL DEFAULT '{}',
    -- 每一列以 Run-Length Encoding 存成 "bin:count,bin:count,…"，
    -- 一片 12 吋晶圓上萬顆晶粒也只佔幾 KB
    rle         JSONB NOT NULL,
    remark      TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL DEFAULT 'system',
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS die_assignments (
    id          BIGSERIAL PRIMARY KEY,
    lot_id      TEXT NOT NULL,
    unit_seq    INTEGER NOT NULL CHECK (unit_seq > 0), -- 該批內的第幾顆成品
    wafer_id    TEXT NOT NULL REFERENCES wafers(wafer_id),
    die_x       INTEGER NOT NULL,
    die_y       INTEGER NOT NULL,
    cp_bin      INTEGER,
    ft_bin      INTEGER,
    status      TEXT NOT NULL DEFAULT 'ASSIGNED',      -- ASSIGNED / PASS / FAIL / SCRAPPED
    assigned_by TEXT NOT NULL DEFAULT 'system',
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lot_id, unit_seq),
    UNIQUE (wafer_id, die_x, die_y)
);
CREATE INDEX IF NOT EXISTS ix_die_lot ON die_assignments (lot_id);
CREATE INDEX IF NOT EXISTS ix_die_wafer ON die_assignments (wafer_id);

-- ══════════════════════════════════════════════════════════
--  e-SOP 電子作業指導書
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sops (
    sop_code       TEXT NOT NULL,
    version        INTEGER NOT NULL CHECK (version > 0),
    title          TEXT NOT NULL,
    op_code        TEXT NOT NULL REFERENCES operations(op_code),
    device_id      TEXT NOT NULL DEFAULT '',           -- 空字串 = 全料號適用
    summary        TEXT NOT NULL DEFAULT '',
    steps          JSONB NOT NULL DEFAULT '[]',
    hazards        TEXT NOT NULL DEFAULT '',
    ppe            TEXT[] NOT NULL DEFAULT '{}',       -- 應穿戴的防護具
    attachments    JSONB NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'DRAFT',      -- DRAFT / RELEASED / OBSOLETE
    effective_from TIMESTAMPTZ,
    require_ack    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL DEFAULT 'system',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT,
    released_by    TEXT,
    released_at    TIMESTAMPTZ,
    PRIMARY KEY (sop_code, version)
);
CREATE INDEX IF NOT EXISTS ix_sops_op ON sops (op_code, status);

CREATE TABLE IF NOT EXISTS sop_acknowledgements (
    id              BIGSERIAL PRIMARY KEY,
    sop_code        TEXT NOT NULL,
    version         INTEGER NOT NULL,
    username        TEXT NOT NULL,
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sop_code, version, username)
);
CREATE INDEX IF NOT EXISTS ix_sopack_user ON sop_acknowledgements (username);

-- ══════════════════════════════════════════════════════════
--  ERP 介接（收發交易表）
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS erp_inbound (
    id           BIGSERIAL PRIMARY KEY,
    external_id  TEXT NOT NULL,                        -- ERP 端的單號，用來做冪等
    doc_type     TEXT NOT NULL,                        -- CUSTOMER / DEVICE / WORK_ORDER / MATERIAL
    payload      JSONB NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING',      -- PENDING / PROCESSED / FAILED
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    result       JSONB,
    source       TEXT NOT NULL DEFAULT 'REST',         -- REST / FILE
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    UNIQUE (doc_type, external_id)
);
CREATE INDEX IF NOT EXISTS ix_erpin_status ON erp_inbound (status, received_at);

CREATE TABLE IF NOT EXISTS erp_outbound (
    id         BIGSERIAL PRIMARY KEY,
    doc_type   TEXT NOT NULL,                          -- PRODUCTION_REPORT / MATERIAL_ISSUE / SHIPMENT / SCRAP
    reference  TEXT NOT NULL,                          -- lot_id / shipment_no …
    payload    JSONB NOT NULL,
    status     TEXT NOT NULL DEFAULT 'PENDING',        -- PENDING / SENT / ACKED / FAILED
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at    TIMESTAMPTZ,
    acked_at   TIMESTAMPTZ,
    UNIQUE (doc_type, reference)
);
CREATE INDEX IF NOT EXISTS ix_erpout_status ON erp_outbound (status, created_at);

-- ══════════════════════════════════════════════════════════
--  SECS/GEM 設備連線
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS secs_links (
    eq_id             TEXT PRIMARY KEY REFERENCES equipments(eq_id),
    host              TEXT NOT NULL DEFAULT '127.0.0.1',
    port              INTEGER NOT NULL DEFAULT 5000,
    session_id        INTEGER NOT NULL DEFAULT 0,
    mode              TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE：由 MES 主動連線
    t3_timeout_sec    INTEGER NOT NULL DEFAULT 45,
    t5_timeout_sec    INTEGER NOT NULL DEFAULT 10,
    linktest_sec      INTEGER NOT NULL DEFAULT 30,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    connection_state  TEXT NOT NULL DEFAULT 'NOT_CONNECTED',
    last_connected_at TIMESTAMPTZ,
    last_message_at   TIMESTAMPTZ,
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS secs_messages (
    id           BIGSERIAL PRIMARY KEY,
    eq_id        TEXT NOT NULL,
    direction    TEXT NOT NULL,                        -- SEND / RECV
    stream       INTEGER NOT NULL,
    function     INTEGER NOT NULL,
    w_bit        BOOLEAN NOT NULL DEFAULT FALSE,
    system_bytes BIGINT NOT NULL DEFAULT 0,
    description  TEXT NOT NULL DEFAULT '',
    body         JSONB,
    raw_hex      TEXT,
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_secsmsg_eq_time ON secs_messages (eq_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS secs_event_rules (
    id       BIGSERIAL PRIMARY KEY,
    eq_id    TEXT NOT NULL DEFAULT '',                 -- 空字串 = 套用到所有設備
    ceid     BIGINT NOT NULL,
    name     TEXT NOT NULL,
    action   TEXT NOT NULL,                            -- EQ_STATE / TRACK_OUT_READY / ALARM / LOG_ONLY
    params   JSONB NOT NULL DEFAULT '{}',
    enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (eq_id, ceid)
);

-- 站別是否要求作業員先確認 e-SOP 才能進站
ALTER TABLE operations ADD COLUMN IF NOT EXISTS require_sop_ack BOOLEAN NOT NULL DEFAULT FALSE;
