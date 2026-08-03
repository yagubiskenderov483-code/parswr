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

# Мгновенный live-парс (без ожидания конца скана)
BURST_PARALLEL = 28
BURST_PER_COLLECTION = 12
BURST_MAX_COLLECTIONS = 100
BURST_GAP = 0.0
BURST_TIME_BUDGET = 3.0
API_TIMEOUT = 2.2
RESULT_LIMIT = 100
PREVIEW_COUNT = 100

# Чеки
CHECK_INTERVAL = 0.7
CHECK_PARALLEL = 14
CHECK_PER_COLLECTION = 10
CHECK_BATCH = 32
CHECK_GAP = 0.02

OWNER_TIMEOUT = 0.55
OWNER_PARALLEL = 16
PAID_DM_TIMEOUT = 1.2
PROFILE_TIMEOUT = 1.3
PROFILE_PARALLEL = 12

NOTIFY_PER_MIN = 50
NOTIFY_GAP = 0.08
AUTO_BLACKLIST = True
