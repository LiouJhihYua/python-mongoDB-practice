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
