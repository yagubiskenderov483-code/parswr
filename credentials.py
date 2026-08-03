"""Hardcoded Telegram credentials."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

MIN_STARS = 2000
MAX_STARS = 5000

# TON (nanograms) → Stars для фильтра цен
STARS_PER_TON = 300.0

# Качество выдачи
# lvl 5 / 6 / 8+ режем — оставляем только ≤4
MAX_ACCOUNT_LEVEL = 4
# киты с кучей гифтов на профиле
MAX_PROFILE_GIFTS = 40
MIN_RU_SCORE = 2  # кириллица в био/имени/канале/подарках
GIFTS_PROBE_LIMIT = 10

# Быстрый первый выброс — жёсткий потолок ~3с
BURST_PARALLEL = 24
BURST_PER_COLLECTION = 8
BURST_MAX_COLLECTIONS = 80
BURST_GAP = 0.005
BURST_TIME_BUDGET = 3.0
API_TIMEOUT = 2.8
RESULT_LIMIT = 100
PREVIEW_COUNT = 100

# Чеки
CHECK_INTERVAL = 0.8
CHECK_PARALLEL = 12
CHECK_PER_COLLECTION = 8
CHECK_BATCH = 30
CHECK_GAP = 0.025

OWNER_TIMEOUT = 0.55
OWNER_PARALLEL = 14
PAID_DM_TIMEOUT = 1.4
PROFILE_TIMEOUT = 1.6
PROFILE_PARALLEL = 10
NOTIFY_GAP = 0.1  # пауза между карточками, чтобы не шли пачкой
