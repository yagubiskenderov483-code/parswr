"""Hardcoded Telegram credentials + профили скорости."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

MIN_STARS = 2000
MAX_STARS = 5000

PREVIEW_COUNT = 30
SHOW_LIMIT = 30
# 0 = только списком (как в примере), без карточек по одной
NOTIFY_CARDS = 0

AFK_USER_CAP = 5_000_000
AFK_STATUS_EVERY = 25.0

# Профили скорости: тихо бережёт сессию, норм — баланс, быстро — шустрее но аккуратно
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
        "FILTER_DB_LIMIT": 30,
        "FILTER_EARLY_SHOW_AT": 25,
        "AFK_PAGE_LIMIT": 30,
        "AFK_GAP": 0.35,
        "AFK_PARALLEL": 2,
    },
    "norm": {
        "label": "⚖️ Норм",
        "BURST_PARALLEL": 8,
        "BURST_PER_COLLECTION": 8,
        "BURST_MAX_COLLECTIONS": 0,
        "BURST_GAP": 0.12,
        "API_TIMEOUT": 6.0,
        "BURST_EARLY_SHOW_AT": 30,
        "CHECK_INTERVAL": 1.2,
        "CHECK_PARALLEL": 6,
        "CHECK_PER_COLLECTION": 6,
        "CHECK_BATCH": 20,
        "CHECK_GAP": 0.14,
        "OWNER_TIMEOUT": 0.75,
        "ENRICH_PARALLEL": 5,
        "FILTER_BURST_PARALLEL": 8,
        "FILTER_BURST_PER_COLLECTION": 8,
        "FILTER_BURST_MAX_COLLECTIONS": 0,
        "FILTER_BURST_GAP": 0.12,
        "FILTER_LIMIT": 30,
        "FILTER_DB_LIMIT": 30,
        "FILTER_EARLY_SHOW_AT": 30,
        "AFK_PAGE_LIMIT": 40,
        "AFK_GAP": 0.22,
        "AFK_PARALLEL": 2,
    },
    "fast": {
        "label": "⚡ Быстро",
        "BURST_PARALLEL": 12,
        "BURST_PER_COLLECTION": 8,
        "BURST_MAX_COLLECTIONS": 0,
        "BURST_GAP": 0.08,
        "API_TIMEOUT": 5.0,
        "BURST_EARLY_SHOW_AT": 35,
        "CHECK_INTERVAL": 0.8,
        "CHECK_PARALLEL": 8,
        "CHECK_PER_COLLECTION": 6,
        "CHECK_BATCH": 25,
        "CHECK_GAP": 0.09,
        "OWNER_TIMEOUT": 0.65,
        "ENRICH_PARALLEL": 6,
        "FILTER_BURST_PARALLEL": 12,
        "FILTER_BURST_PER_COLLECTION": 8,
        "FILTER_BURST_MAX_COLLECTIONS": 0,
        "FILTER_BURST_GAP": 0.08,
        "FILTER_LIMIT": 30,
        "FILTER_DB_LIMIT": 30,
        "FILTER_EARLY_SHOW_AT": 35,
        "AFK_PAGE_LIMIT": 40,
        "AFK_GAP": 0.18,
        "AFK_PARALLEL": 3,
    },
}

DEFAULT_SPEED = "norm"


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
