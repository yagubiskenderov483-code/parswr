"""Hardcoded Telegram credentials."""

BOT_TOKEN = "8952681622:AAGEe2m5L6jWxlFcw-gF_NIl9UbGDTW33Vc"
API_ID = 36101343
API_HASH = "116195fa5e0459d25a9a6266b40807d7"

# Defaults (overridden when user picks a range)
MIN_STARS = 2000
MAX_STARS = 100_000

# Parser tuning — not too parallel (FloodWait kills everything)
POLL_INTERVAL = 0.25
PER_COLLECTION = 15
WAVE_BATCH = 12
CONCURRENCY = 8
