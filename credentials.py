"""Hardcoded Telegram credentials."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"
# v2 — старая сессия сброшена, бот снова попросит номер
SESSION = "data/market_session_v2"

# Price filter (Stars)
MIN_STARS = 2000
MAX_STARS = 100_000

# Ultra-fast poll between full-market scans
POLL_INTERVAL = 0.12

# Top newest from each collection per scan
PER_COLLECTION = 12

# Show this many freshest on Start, then only brand-new listings
PREVIEW_LOTS = 12
