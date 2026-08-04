"""Hardcoded Telegram credentials."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

MIN_STARS = 2000
MAX_STARS = 5000

# Быстрый первый выброс — тоже все коллекции 149/149
BURST_PARALLEL = 25
BURST_PER_COLLECTION = 12
BURST_MAX_COLLECTIONS = 0  # 0 = все (149/149)
BURST_GAP = 0.01
API_TIMEOUT = 5.0

# Чеки: каждый чек = ВСЕ коллекции (149/149)
CHECK_INTERVAL = 0.2
CHECK_PARALLEL = 18
CHECK_PER_COLLECTION = 10
CHECK_BATCH = 0  # 0 = все коллекции за один чек
CHECK_GAP = 0.02
OWNER_TIMEOUT = 0.9
# Лимит выдачи в чат — чтобы не жечь API/БД впустую
PREVIEW_COUNT = 30
SHOW_LIMIT = 30
NOTIFY_CARDS = 10  # карточек с кнопкой «Блок»

# Отдельный фильтр-поиск (не трогает парсер)
FILTER_BURST_PARALLEL = 25
FILTER_BURST_PER_COLLECTION = 12
FILTER_BURST_MAX_COLLECTIONS = 0  # 0 = все 149/149
FILTER_BURST_GAP = 0.01
FILTER_LIMIT = 30
FILTER_DB_LIMIT = 30

# AFK фарм юзов по всем коллекциям
AFK_USER_CAP = 5_000_000
AFK_PAGE_LIMIT = 50
AFK_GAP = 0.05
AFK_PARALLEL = 4
AFK_STATUS_EVERY = 15.0
