"""Hardcoded Telegram credentials."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

MIN_STARS = 2000
MAX_STARS = 5000

# Тихий режим — не жечь сессию
# маленький parallel + большие паузы

# Первый поиск
BURST_PARALLEL = 4
BURST_PER_COLLECTION = 6
BURST_MAX_COLLECTIONS = 0  # все коллекции, но потихоньку
BURST_GAP = 0.22
API_TIMEOUT = 8.0
BURST_EARLY_SHOW_AT = 25  # ранняя выдача, но без гонки

# Чеки: не все 149 разом — кусками, полный круг за несколько чеков
CHECK_INTERVAL = 2.5
CHECK_PARALLEL = 3
CHECK_PER_COLLECTION = 5
CHECK_BATCH = 12  # за чек ~12 коллекций, аккуратно
CHECK_GAP = 0.28
OWNER_TIMEOUT = 0.9

PREVIEW_COUNT = 30
SHOW_LIMIT = 30
NOTIFY_CARDS = 5

# Фильтр-поиск — тоже тихо
FILTER_BURST_PARALLEL = 4
FILTER_BURST_PER_COLLECTION = 6
FILTER_BURST_MAX_COLLECTIONS = 0
FILTER_BURST_GAP = 0.22
FILTER_LIMIT = 30
FILTER_DB_LIMIT = 30
FILTER_EARLY_SHOW_AT = 25

# AFK фарм — медленно копим юзов
AFK_USER_CAP = 5_000_000
AFK_PAGE_LIMIT = 30
AFK_GAP = 0.35
AFK_PARALLEL = 2
AFK_STATUS_EVERY = 25.0

# Enrich профилей
ENRICH_PARALLEL = 3
