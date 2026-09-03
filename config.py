"""Жёсткие настройки гифт-трекера."""

from __future__ import annotations

import os
from pathlib import Path

# @jsjeigiejwhnewbot
BOT_USERNAME = "jsjeigiejwhnewbot"
BOT_TOKEN = "8825465611:AAE8-3_hFqU32_-gUJLbvZC96i-MvDl3lNA"

# User API (my.telegram.org) — из реп Stars / ParserUs / Zayavki
# старый 36101343 (ParserGift) забанен
API_ID = 28687552
API_HASH = "1abf9a58d0c22f62437bec89bd6b27a3"

CHANNEL_ID = -1003784435307

# Цель 5000–25000, с запасом чуть дешевле/дороже
MIN_STARS = 4500
MAX_STARS = 27000
MAX_ACCOUNT_LEVEL = 2
MAX_NFTS = 6  # уникальные/дорогие; дешёвые безлимитные не считаем
POST_INTERVAL = 4.0

# Курс для строки «X Stars / Y TON» (как в tracker market)
TON_RATE = 0.0102
TZ_OFFSET = 3.0  # МСК

POLL_INTERVAL = 0.05
PAGE_LIMIT = 8  # верх newest: новый id спереди = только что выставили
SCAN_BATCH = 36  # кольцо коллекций
SCAN_PARALLEL = 8
REQUEST_GAP = 0.02
REQUEST_TIMEOUT = 5.0
ENRICH_TIMEOUT = 4.0
MIN_COLLECTIONS = 50  # Bot API даёт ~11; полный NFT-каталог ~100+

TRACKER_VERSION = "5.7.1"
# Подробный лог каждого fresh-лота: поля профиля + причина отсева
DEBUG_FILTERS = True
BASE_DIR = Path(__file__).resolve().parent


def data_dir() -> Path:
    bothost = Path("/app/data")
    if bothost.is_dir():
        return bothost
    local = BASE_DIR / "data"
    local.mkdir(parents=True, exist_ok=True)
    return local


def session_path() -> Path:
    raw = (os.environ.get("SESSION_FILE") or "").strip()
    if raw:
        return Path(raw)
    return data_dir() / "tracker_session.txt"


def state_path() -> Path:
    raw = (os.environ.get("STATE_FILE") or "").strip()
    if raw:
        return Path(raw)
    return data_dir() / "tracker_state.json"


def catalog_path() -> Path:
    return data_dir() / "tracker_catalog.json"


def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def bot_token() -> str:
    return (BOT_TOKEN or os.environ.get("BOT_TOKEN") or "").strip()


def api_id() -> int:
    return env_int("API_ID", API_ID)


def api_hash() -> str:
    return (os.environ.get("API_HASH") or API_HASH).strip()


def channel_id() -> int:
    return env_int("CHANNEL_ID", CHANNEL_ID)
