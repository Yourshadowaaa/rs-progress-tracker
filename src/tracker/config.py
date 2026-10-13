"""Runtime configuration, read from environment variables (Railway service variables)."""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HiscoreTable = Literal["hiscore", "hiscore_ironman", "hiscore_hardcore_ironman"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/rs_tracker"
    player_name: str = "Get Grouped"
    hiscore_table: HiscoreTable = "hiscore_ironman"
    display_tz: str = "Europe/Berlin"
    user_agent: str = "rs-progress-tracker/0.1 (personal hourly tracker; one request per source per hour)"
    http_timeout_s: float = 20.0
    http_retries: int = 3
    # Minimum minutes between runs of the same job; protects against double-fired crons.
    min_run_interval_min: int = 50

    # Force-update button (/admin). Disabled unless ADMIN_PASSWORD is set.
    admin_username: str = "admin"
    admin_password: str | None = None
    force_update_cooldown_min: int = 5

    # Backups (optional): pg_dump once a day at BACKUP_HOUR_UTC to S3 and/or a local dir.
    backup_hour_utc: int = 3
    backup_retention_days: int = 30
    backup_dir: str | None = None
    backup_s3_bucket: str | None = None
    backup_s3_endpoint: str | None = None
    backup_s3_access_key_id: str | None = None
    backup_s3_secret_access_key: str | None = None
    backup_s3_region: str = "auto"

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        # Railway injects postgres:// or postgresql:// URLs; SQLAlchemy needs the driver name.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
