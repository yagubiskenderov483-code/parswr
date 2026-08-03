from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Always load .env from project root (even if cwd differs)
load_dotenv(ROOT_DIR / ".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    bot_token: str = Field(
        default="8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc",
        alias="BOT_TOKEN",
    )
    api_id: int = Field(default=36101343, alias="API_ID")
    api_hash: str = Field(
        default="116195fa5e0459d25a9a6266b40807d7",
        alias="API_HASH",
    )
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

    @field_validator("api_id", mode="before")
    @classmethod
    def _parse_api_id(cls, value):  # noqa: ANN001
        if value is None or value == "":
            return 0
        return int(value)

    @field_validator("api_hash", mode="before")
    @classmethod
    def _strip_hash(cls, value):  # noqa: ANN001
        return (value or "").strip().strip('"').strip("'")

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

    def require_telethon(self) -> None:
        api_id = self.api_id or int(os.getenv("API_ID") or 36101343)
        api_hash = (
            self.api_hash
            or os.getenv("API_HASH", "").strip()
            or "116195fa5e0459d25a9a6266b40807d7"
        )
        if not api_id or not api_hash:
            raise RuntimeError(
                "Не заданы API_ID/API_HASH.\n"
                "Открой файл .env в корне проекта и пропиши:\n"
                "API_ID=...\n"
                "API_HASH=...\n"
                "(взять на https://my.telegram.org)"
            )
        object.__setattr__(self, "api_id", int(api_id))
        object.__setattr__(self, "api_hash", api_hash)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    # Fallbacks if pydantic missed env for any reason
    if not settings.bot_token:
        object.__setattr__(
            settings,
            "bot_token",
            os.getenv("BOT_TOKEN", "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"),
        )
    if not settings.api_id:
        object.__setattr__(settings, "api_id", int(os.getenv("API_ID") or 36101343))
    if not settings.api_hash:
        object.__setattr__(
            settings,
            "api_hash",
            os.getenv("API_HASH", "116195fa5e0459d25a9a6266b40807d7").strip(),
        )
    return settings
