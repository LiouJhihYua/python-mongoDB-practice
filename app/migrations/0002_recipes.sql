-- ══════════════════════════════════════════════════════════
--  配方管理（Recipe / PPID）
--
--  封測廠最常見的品質事故之一是「機台載錯配方」——
--  料號換了、配方沒換，整批做出來才發現。這組資料表把
--  「哪些配方核可用於哪個料號＋站別」與「機台現在載的是哪一個」
--  分開存放，進站時比對兩者，不符就擋下來。
-- ══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS recipes (
    ppid           TEXT NOT NULL,                       -- 機台端的配方名稱（Process Program ID）
    version        INTEGER NOT NULL CHECK (version > 0),
    name           TEXT NOT NULL,
    op_code        TEXT NOT NULL REFERENCES operations(op_code),
    device_id      TEXT NOT NULL DEFAULT '',            -- 空字串 = 全料號適用
    eq_model       TEXT NOT NULL DEFAULT '',            -- 空字串 = 不限機型
    parameters     JSONB NOT NULL DEFAULT '{}',         -- 配方參數（溫度、時間、力道…）
    checksum       TEXT NOT NULL DEFAULT '',            -- 機台端配方的校驗碼，用來確認內容沒被改過
    status         TEXT NOT NULL DEFAULT 'DRAFT',       -- DRAFT / RELEASED / OBSOLETE
    effective_from TIMESTAMPTZ,
    remark         TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL DEFAULT 'system',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT,
    released_by    TEXT,
    released_at    TIMESTAMPTZ,
    PRIMARY KEY (ppid, version)
);
CREATE INDEX IF NOT EXISTS ix_recipes_op ON recipes (op_code, device_id, status);

-- 機台目前載入的配方（一台一列，隨 SECS 事件或人工回報更新）
CREATE TABLE IF NOT EXISTS equipment_recipes (
    eq_id     TEXT PRIMARY KEY REFERENCES equipments(eq_id),
    ppid      TEXT NOT NULL DEFAULT '',
    version   INTEGER,
    checksum  TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT 'MANUAL',           -- MANUAL / SECS / DOWNLOAD
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    loaded_by TEXT NOT NULL DEFAULT 'system'
);

-- 進站配方比對紀錄 —— 客戶稽核時要拿得出「每一批都比對過」的證據
CREATE TABLE IF NOT EXISTS recipe_checks (
    id             BIGSERIAL PRIMARY KEY,
    lot_id         TEXT NOT NULL,
    eq_id          TEXT NOT NULL DEFAULT '',
    op_code        TEXT NOT NULL,
    device_id      TEXT NOT NULL DEFAULT '',
    expected_ppids TEXT[] NOT NULL DEFAULT '{}',
    loaded_ppid    TEXT NOT NULL DEFAULT '',
    passed         BOOLEAN NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    operator       TEXT NOT NULL DEFAULT '',
    timestamp      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rcpchk_lot ON recipe_checks (lot_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_rcpchk_eq ON recipe_checks (eq_id, timestamp DESC);

-- 站別是否要求進站前比對機台配方
ALTER TABLE operations ADD COLUMN IF NOT EXISTS require_recipe_check BOOLEAN NOT NULL DEFAULT FALSE;
