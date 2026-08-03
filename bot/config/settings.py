from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(alias="BOT_TOKEN")
    api_id: int = Field(default=0, alias="API_ID")
    api_hash: str = Field(default="", alias="API_HASH")
    telethon_session: str = Field(default="data/market_session", alias="TELETHON_SESSION")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/bot.db",
        alias="DATABASE_URL",
    )
    admin_ids: str = Field(default="", alias="ADMIN_IDS")
    default_min_stars: float = Field(default=2000, alias="DEFAULT_MIN_STARS")
    default_max_stars: float = Field(default=100000, alias="DEFAULT_MAX_STARS")
    default_poll_interval: float = Field(default=2.0, alias="DEFAULT_POLL_INTERVAL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def admin_id_list(self) -> list[int]:
        if not self.admin_ids.strip():
            return []
        return [int(x.strip()) for x in self.admin_ids.split(",") if x.strip()]

    @property
    def session_path(self) -> Path:
        path = Path(self.telethon_session)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
