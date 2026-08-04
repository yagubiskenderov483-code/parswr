"""Hardcoded Telegram credentials."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

MIN_STARS = 2000
MAX_STARS = 5000

# Быстрый первый выброс — все 149, но выдача как только набрали пул
BURST_PARALLEL = 35
BURST_PER_COLLECTION = 8
BURST_MAX_COLLECTIONS = 0  # 0 = все (149/149)
BURST_GAP = 0.005
API_TIMEOUT = 3.2
# показать лоты сразу, когда в диапазоне набралось столько (не ждать конца 149)
BURST_EARLY_SHOW_AT = 45

# Чеки: полный круг 149/149, но легче по глубине — быстрее
CHECK_INTERVAL = 0.05
CHECK_PARALLEL = 35
CHECK_PER_COLLECTION = 6
CHECK_BATCH = 0  # 0 = все коллекции за один чек
CHECK_GAP = 0.005
OWNER_TIMEOUT = 0.55
# Лимит выдачи в чат — чтобы не жечь API/БД впустую
PREVIEW_COUNT = 30
SHOW_LIMIT = 30
NOTIFY_CARDS = 8  # карточек с кнопкой «Блок»

# Отдельный фильтр-поиск (не трогает парсер)
FILTER_BURST_PARALLEL = 35
FILTER_BURST_PER_COLLECTION = 8
FILTER_BURST_MAX_COLLECTIONS = 0  # 0 = все 149/149
FILTER_BURST_GAP = 0.005
FILTER_LIMIT = 30
FILTER_DB_LIMIT = 30
FILTER_EARLY_SHOW_AT = 45

# AFK фарм юзов по всем коллекциям
AFK_USER_CAP = 5_000_000
AFK_PAGE_LIMIT = 50
AFK_GAP = 0.05
AFK_PARALLEL = 4
AFK_STATUS_EVERY = 15.0
