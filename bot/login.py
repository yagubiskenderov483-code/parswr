"""One-time Telethon login for MRKT / Portal / Telegram Market.

Usage:
  python -m bot.login
"""

from __future__ import annotations

import asyncio
import sys

from bot.config import get_settings
from bot.parsers.registry import build_telethon, fetch_mrkt_token, fetch_portals_auth
from bot.utils import setup_logging


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.api_id or not settings.api_hash:
        print("Set API_ID and API_HASH in .env", file=sys.stderr)
        raise SystemExit(1)

    client = build_telethon(settings)
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}) id={me.id}")

    try:
        token = await fetch_mrkt_token(client)
        print(f"MRKT_TOKEN={token}")
    except Exception as exc:  # noqa: BLE001
        print(f"MRKT auth failed: {exc}")

    try:
        auth = await fetch_portals_auth(client)
        print(f"PORTALS_AUTH={auth[:100]}...")
    except Exception as exc:  # noqa: BLE001
        print(f"Portal auth failed: {exc}")

    await client.disconnect()
    print(f"Session saved: {settings.session_path}.session")


if __name__ == "__main__":
    asyncio.run(main())
