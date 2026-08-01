"""系統設定：全部可用 MES_ 開頭的環境變數覆寫。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="MES_", extra="ignore", case_sensitive=False
    )

    app_name: str = "OSAT MES"
    app_version: str = "1.0.0"

    # ── 資料庫 ──────────────────────────────────────────────
    # mongo  = 連線真實 MongoDB
    # memory = 使用 mongomock 的記憶體資料庫（展示／測試用，重啟即清空）
    db_backend: str = "mongo"
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db: str = "osat_mes"

    # ── 認證 ────────────────────────────────────────────────
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_expire_minutes: int = 480  # 一個班別
    bootstrap_admin: str = "admin"
    bootstrap_admin_password: str = "admin1234"

    # ── 廠務 ────────────────────────────────────────────────
    factory_code: str = "OSAT-KH1"
    tz_offset_hours: int = 8  # 台灣時間
    shift_start_hours: list[int] = [8, 20]  # 早班 08:00 / 夜班 20:00

    # 逾時未進站（Q-Time）超限時是否自動 Hold 批號
    auto_hold_on_qtime_violation: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
