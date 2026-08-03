"""Hardcoded Telegram credentials."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

MIN_STARS = 2000
MAX_STARS = 5000

# TON (nanograms) → Stars для фильтра цен
# StarsTonAmount.amount = nanotons (1 TON = 1e9)
STARS_PER_TON = 300.0

# Быстрый первый выброс (~как FreeGiftsParser)
# один GetResale на коллекцию (Stars+TON), без двойного запроса
BURST_PARALLEL = 16
BURST_PER_COLLECTION = 15
BURST_MAX_COLLECTIONS = 120
BURST_GAP = 0.012
BURST_TIME_BUDGET = 6.0
API_TIMEOUT = 7.0
RESULT_LIMIT = 100
PREVIEW_COUNT = 100

# Дальше чеки ~раз в секунду
CHECK_INTERVAL = 1.0
CHECK_PARALLEL = 8
CHECK_PER_COLLECTION = 12
CHECK_BATCH = 24
CHECK_GAP = 0.05
OWNER_TIMEOUT = 0.85
PAID_DM_TIMEOUT = 2.5
