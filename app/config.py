"""系統設定：全部可用 MES_ 開頭的環境變數覆寫。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="MES_", extra="ignore", case_sensitive=False
    )

    app_name: str = "OSAT MES"
    app_version: str = "2.0.0"

    # ── 資料庫（PostgreSQL）────────────────────────────────
    # postgres = 連線既有的 PostgreSQL（正式環境）
    # embedded = 用 pgserver 就地啟動一個 PostgreSQL（展示／測試，免安裝）
    db_backend: str = "postgres"
    database_url: str = "postgresql://mes:mes@localhost:5432/osat_mes"
    database_name: str = "osat_mes"
    embedded_data_dir: str = "./data/pgdata"
    db_pool_min: int = 1
    db_pool_max: int = 10

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
