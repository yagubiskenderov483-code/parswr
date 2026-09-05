"""Жёсткие настройки гифт-трекера."""

from __future__ import annotations

import os
from pathlib import Path


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


# @jsjeigiejwhnewbot
BOT_USERNAME = "jsjeigiejwhnewbot"
BOT_TOKEN = "8825465611:AAGVEabGitYdpQeACvJDkN3pkmrGqK9Ze5g"

# User API (my.telegram.org) — из реп Stars / ParserUs / Zayavki
# старый 36101343 (ParserGift) забанен
API_ID = 28687552
API_HASH = "1abf9a58d0c22f62437bec89bd6b27a3"

CHANNEL_ID = -1003784435307

# Лимит выдачи: 5000–25000 Stars (жёстко, env не перебивает)
MIN_STARS = 5000
MAX_STARS = 25000
# Допуск к цене КОНКРЕТНОГО лота (не floor модели). 0 = жёсткий диапазон.
LISTING_PRICE_TOLERANCE = env_float("LISTING_PRICE_TOLERANCE", 0.0)
# Реальный min resale модели/варианта. Дешёвая модель за 8000⭐ не проходит.
MIN_MODEL_FLOOR = env_int("MIN_MODEL_FLOOR", 4000)
MAX_MODEL_FLOOR = env_int("MAX_MODEL_FLOOR", 27000)
FLOOR_CACHE_TTL = env_float("FLOOR_CACHE_TTL", 1800.0)  # сек; не на каждом scan round
FLOOR_REFRESH_MAX_PAGES = env_int("FLOOR_REFRESH_MAX_PAGES", 6)
FLOOR_REFRESH_PAGE_SIZE = env_int("FLOOR_REFRESH_PAGE_SIZE", 50)
MAX_ACCOUNT_LEVEL = env_int("MAX_ACCOUNT_LEVEL", 2)
MAX_NFTS = env_int("MAX_NFTS", 6)  # уникальные/дорогие; дешёвые безлимитные не считаем
POST_INTERVAL = env_float("POST_INTERVAL", 4.0)  # только между отправками в канал, не scan round

# Женский gate: якорь имени/отчества/female-name в нике. Эмодзи/фото/подарки не пол.
GIRL_MIN_SCORE = env_int("GIRL_MIN_SCORE", 5)
GIRL_REQUIRE_IDENTITY = True

TON_RATE = 0.0102
TZ_OFFSET = 3.0  # МСК

POLL_INTERVAL = env_float("POLL_INTERVAL", 0.05)  # между проходами сканера ≠ POST_INTERVAL
PAGE_LIMIT = env_int("PAGE_LIMIT", 12)  # верх newest одной страницы
SCAN_PARALLEL = env_int("SCAN_PARALLEL", 12)
# 0 = все eligible коллекции за round. Env >0 — кольцо по N.
SCAN_BATCH = env_int("SCAN_BATCH", 0)
# Реальный in-flight GetResaleStarGifts. Не поднимаем ради «быстрее» — FloodWait.
RPC_CONCURRENCY = max(1, min(env_int("RPC_CONCURRENCY", 6), SCAN_PARALLEL))
# 0 = все eligible модели одним RPC (новые лоты сразу наверху newest).
SCAN_MODEL_CHUNK = env_int("SCAN_MODEL_CHUNK", 0)
# Live: newest-страницы до первого уже известного лота. 2 — если page1 вся новая.
SCAN_MAX_PAGES = env_int("SCAN_MAX_PAGES", 2)
# Первый обход коллекции / sync: глубже снимок, чтобы старые лоты не всплыли как NEW.
SCAN_SEED_PAGES = env_int("SCAN_SEED_PAGES", 8)
# Накопленный снимок id коллекции (не только последние 12) — меньше ложных fresh.
PAGE_SNAPSHOT_KEEP = env_int("PAGE_SNAPSHOT_KEEP", 120)
REQUEST_GAP = env_float("REQUEST_GAP", 0.02)
# 4s на GetResaleStarGifts давало retry-шторм (to≈90). 8s — тот же RPC, меньше таймаутов.
REQUEST_TIMEOUT = env_float("REQUEST_TIMEOUT", 8.0)
ENRICH_TIMEOUT = env_float("ENRICH_TIMEOUT", 4.0)
MIN_COLLECTIONS = env_int("MIN_COLLECTIONS", 50)

TRACKER_VERSION = "5.13.2"
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_SAFE_LIMIT = 3900
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


def floor_cache_path() -> Path:
    return data_dir() / "model_floors.json"


def bot_token() -> str:
    return (BOT_TOKEN or os.environ.get("BOT_TOKEN") or "").strip()


def api_id() -> int:
    return env_int("API_ID", API_ID)


def api_hash() -> str:
    return (os.environ.get("API_HASH") or API_HASH).strip()


def channel_id() -> int:
    return int(CHANNEL_ID)
