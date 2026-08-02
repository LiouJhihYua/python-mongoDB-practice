-- ══════════════════════════════════════════════════════════
--  登入失敗節流與帳號鎖定
--
--  PBKDF2 240k 輪讓單次猜測很慢，但那不是「可以無限次猜」的理由。
--  連續失敗數次就暫時鎖住帳號，成功登入時歸零。
-- ══════════════════════════════════════════════════════════
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_logins  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until   TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_failed_at TIMESTAMPTZ;

-- 稽核紀錄會無限成長；歸檔前先確保依時間刪除跑得動
CREATE INDEX IF NOT EXISTS ix_audit_kind_time ON audit_logs (kind, timestamp);
