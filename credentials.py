"""Hardcoded Telegram credentials + профили скорости."""

import os

BOT_TOKEN = "8966504132:AAEM2--YD439w7zJWot2mnbeNJpSci4yIaI"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

# БД: после деплоя не слетает — нужен volume на /data (или GIFTS_DB_PATH).
# Код сам берёт самую жирную копию и не прыгает в пустой файл.
GIFTS_DB_PATH = os.environ.get("GIFTS_DB_PATH", "")

# кто может пользоваться ботом
OWNER_ID = 741904495
ALLOWED_USER_IDS: set[int] = {
    741904495,
    8860370086,
    8959759145,
}

MIN_STARS = 2000
MAX_STARS = 5000

# выдача ПО ТИПАМ (коллекциям), не фикс 30 лотов
PER_TYPE = 1  # по одному с каждого типа NFT
MAX_TYPES = 0  # 0 = все найденные типы (без потолка)
# цель ранней выдачи / фильтр-лимит
SHOW_LIMIT = 30
PREVIEW_COUNT = 30

# 0 = только списком, без карточек по одной
NOTIFY_CARDS = 0

# параллельный парсинг + фарм БД сразу с нескольких Telethon-акков
PARSE_ACCOUNTS = 6
# максимум сохранённых аккаунтов в боте
MAX_ACCOUNTS = 6

AFK_USER_CAP = 5_000_000
AFK_STATUS_EVERY = 25.0

BRAND = "Neptun Parser"

# Профили: quietly / norm / turbo
SPEED_PROFILES: dict[str, dict] = {
    "quiet": {
        "label": "🐢 Тихо",
        "BURST_PARALLEL": 4,
        "BURST_PER_COLLECTION": 6,
        "BURST_MAX_COLLECTIONS": 0,
        "BURST_GAP": 0.22,
        "API_TIMEOUT": 8.0,
        "BURST_EARLY_SHOW_AT": 25,
        "CHECK_INTERVAL": 2.5,
        "CHECK_PARALLEL": 3,
        "CHECK_PER_COLLECTION": 5,
        "CHECK_BATCH": 12,
        "CHECK_GAP": 0.28,
        "OWNER_TIMEOUT": 0.9,
        "ENRICH_PARALLEL": 3,
        "FILTER_BURST_PARALLEL": 4,
        "FILTER_BURST_PER_COLLECTION": 6,
        "FILTER_BURST_MAX_COLLECTIONS": 0,
        "FILTER_BURST_GAP": 0.22,
        "FILTER_LIMIT": 30,
        "FILTER_DB_LIMIT": 800,
        "FILTER_EARLY_SHOW_AT": 25,
        "AFK_PAGE_LIMIT": 50,
        "AFK_GAP": 0.2,
        "AFK_PARALLEL": 6,
    },
    "norm": {
        "label": "⚖️ Норм",
        "BURST_PARALLEL": 16,
        "BURST_PER_COLLECTION": 12,
        "BURST_MAX_COLLECTIONS": 0,
        "BURST_GAP": 0.03,
        "API_TIMEOUT": 4.5,
        "BURST_EARLY_SHOW_AT": 18,
        "CHECK_INTERVAL": 0.6,
        "CHECK_PARALLEL": 12,
        "CHECK_PER_COLLECTION": 10,
        "CHECK_BATCH": 40,
        "CHECK_GAP": 0.04,
        "OWNER_TIMEOUT": 0.5,
        "ENRICH_PARALLEL": 12,
        "FILTER_BURST_PARALLEL": 16,
        "FILTER_BURST_PER_COLLECTION": 12,
        "FILTER_BURST_MAX_COLLECTIONS": 0,
        "FILTER_BURST_GAP": 0.03,
        "FILTER_LIMIT": 40,
        "FILTER_DB_LIMIT": 1200,
        "FILTER_EARLY_SHOW_AT": 18,
        "AFK_PAGE_LIMIT": 60,
        "AFK_GAP": 0.08,
        "AFK_PARALLEL": 8,
    },
    "fast": {
        "label": "⚡ Turbo",
        "BURST_PARALLEL": 56,
        "BURST_PER_COLLECTION": 25,
        "BURST_MAX_COLLECTIONS": 0,
        "BURST_GAP": 0.0,
        "API_TIMEOUT": 2.5,
        "BURST_EARLY_SHOW_AT": 14,
        "CHECK_INTERVAL": 0.1,
        "CHECK_PARALLEL": 40,
        "CHECK_PER_COLLECTION": 20,
        "CHECK_BATCH": 100,
        "CHECK_GAP": 0.0,
        "OWNER_TIMEOUT": 0.3,
        "ENRICH_PARALLEL": 32,
        "FILTER_BURST_PARALLEL": 48,
        "FILTER_BURST_PER_COLLECTION": 25,
        "FILTER_BURST_MAX_COLLECTIONS": 0,
        "FILTER_BURST_GAP": 0.0,
        "FILTER_LIMIT": 60,
        "FILTER_DB_LIMIT": 2000,
        "FILTER_EARLY_SHOW_AT": 14,
        "AFK_PAGE_LIMIT": 80,
        "AFK_GAP": 0.02,
        "AFK_PARALLEL": 12,
    },
}

DEFAULT_SPEED = "fast"


def apply_speed(name: str) -> str:
    """Применить профиль скорости к module-level константам. Возвращает label."""
    global BURST_PARALLEL, BURST_PER_COLLECTION, BURST_MAX_COLLECTIONS, BURST_GAP
    global API_TIMEOUT, BURST_EARLY_SHOW_AT
    global CHECK_INTERVAL, CHECK_PARALLEL, CHECK_PER_COLLECTION, CHECK_BATCH, CHECK_GAP
    global OWNER_TIMEOUT, ENRICH_PARALLEL
    global FILTER_BURST_PARALLEL, FILTER_BURST_PER_COLLECTION, FILTER_BURST_MAX_COLLECTIONS
    global FILTER_BURST_GAP, FILTER_LIMIT, FILTER_DB_LIMIT, FILTER_EARLY_SHOW_AT
    global AFK_PAGE_LIMIT, AFK_GAP, AFK_PARALLEL

    key = name if name in SPEED_PROFILES else DEFAULT_SPEED
    p = SPEED_PROFILES[key]
    BURST_PARALLEL = p["BURST_PARALLEL"]
    BURST_PER_COLLECTION = p["BURST_PER_COLLECTION"]
    BURST_MAX_COLLECTIONS = p["BURST_MAX_COLLECTIONS"]
    BURST_GAP = p["BURST_GAP"]
    API_TIMEOUT = p["API_TIMEOUT"]
    BURST_EARLY_SHOW_AT = p["BURST_EARLY_SHOW_AT"]
    CHECK_INTERVAL = p["CHECK_INTERVAL"]
    CHECK_PARALLEL = p["CHECK_PARALLEL"]
    CHECK_PER_COLLECTION = p["CHECK_PER_COLLECTION"]
    CHECK_BATCH = p["CHECK_BATCH"]
    CHECK_GAP = p["CHECK_GAP"]
    OWNER_TIMEOUT = p["OWNER_TIMEOUT"]
    ENRICH_PARALLEL = p["ENRICH_PARALLEL"]
    FILTER_BURST_PARALLEL = p["FILTER_BURST_PARALLEL"]
    FILTER_BURST_PER_COLLECTION = p["FILTER_BURST_PER_COLLECTION"]
    FILTER_BURST_MAX_COLLECTIONS = p["FILTER_BURST_MAX_COLLECTIONS"]
    FILTER_BURST_GAP = p["FILTER_BURST_GAP"]
    FILTER_LIMIT = p["FILTER_LIMIT"]
    FILTER_DB_LIMIT = p["FILTER_DB_LIMIT"]
    FILTER_EARLY_SHOW_AT = p["FILTER_EARLY_SHOW_AT"]
    AFK_PAGE_LIMIT = p["AFK_PAGE_LIMIT"]
    AFK_GAP = p["AFK_GAP"]
    AFK_PARALLEL = p["AFK_PARALLEL"]
    return str(p["label"])


# defaults on import
_speed_label = apply_speed(DEFAULT_SPEED)
