"""Runtime configuration, read from environment variables (prefix QAL_) or a .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QAL_", env_file=".env", extra="ignore")

    # SQLite is the zero-setup default for development; Docker Compose uses PostgreSQL.
    database_url: str = "sqlite:///./var/qal.db"
    data_dir: Path = Path("./var/data")

    # Background jobs and scheduled downloads run inside the API process in the MVP.
    enable_scheduler: bool = True
    scheduler_poll_seconds: float = 5.0
    job_max_attempts: int = 3

    # Provider credentials. Never exposed to research agents (see DECISIONS.md D-008).
    ccxt_exchange: str = "binance"
    ccxt_api_key: str | None = None
    ccxt_api_secret: str | None = None
    stooq_api_key: str | None = None
    mt5_terminal_path: str | None = None
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None

    # Real order execution is not implemented and stays disabled (principle 8).
    live_trading_enabled: bool = False

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()
