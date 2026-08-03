"""Hardcoded Telegram credentials + tunables."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

MIN_STARS = 2000
MAX_STARS = 5000

STARS_PER_TON = 300.0

# Качество
MAX_ACCOUNT_LEVEL = 4
MAX_PROFILE_GIFTS = 40
MIN_RU_SCORE = 2
GIFTS_PROBE_LIMIT = 8

# online | recent | any
ONLINE_MODE = "recent"

# Макс. надбавка над floor (мин. ценой коллекции) по режиму поиска.
# None = не фильтровать по флору.
# Пример: floor Snoop=87k, режим 60-100k → delta=8k → берём только ≤95k.
FLOOR_DELTA_BY_RANGE = {
    (2000, 5000): 0,
    (5000, 15000): 1000,
    (15000, 30000): 2000,
    (30000, 60000): 5000,
    (60000, 100000): 8000,
    (0, 2000): None,  # пох
}

# Свежесть
FRESH_MAX_AGE_SEC = 120
FRESH_MAX_RANK = 2

# Live-парс: обходим много коллекций (не 10–15)
BURST_PARALLEL = 40
BURST_PER_COLLECTION = 16
BURST_MAX_COLLECTIONS = 400
BURST_GAP = 0.0
BURST_TIME_BUDGET = 12.0
API_TIMEOUT = 2.5
RESULT_LIMIT = 100
PREVIEW_COUNT = 100

# Чеки — широкий срез за тик
CHECK_INTERVAL = 0.55
CHECK_PARALLEL = 28
CHECK_PER_COLLECTION = 12
CHECK_BATCH = 80
CHECK_GAP = 0.0

OWNER_TIMEOUT = 1.8
OWNER_PARALLEL = 20
PAID_DM_TIMEOUT = 1.4
PROFILE_TIMEOUT = 1.5
PROFILE_PARALLEL = 14

NOTIFY_PER_MIN = 50
NOTIFY_GAP = 0.08
# минимум сколько других лотов между одинаковым title / seller
EMIT_GAP = 5
AUTO_BLACKLIST = True
