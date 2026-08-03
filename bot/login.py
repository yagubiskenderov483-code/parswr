"""Одноразовый логин Telethon для MRKT / Portals.

Запуск:
  python -m bot.login
"""

from __future__ import annotations

import asyncio
import sys

from bot.auth import (
    auth_mrkt_token,
    auth_portals_token,
    auth_tonnel_data,
    build_telethon_client,
)
from bot.config import load_settings


async def main() -> None:
    settings = load_settings()
    if not settings.api_id or not settings.api_hash:
        print("Задай API_ID и API_HASH в .env", file=sys.stderr)
        raise SystemExit(1)

    client = build_telethon_client(
        settings.api_id, settings.api_hash, settings.session_path
    )
    await client.start()
    me = await client.get_me()
    print(f"OK: вошли как {me.first_name} (@{me.username}) id={me.id}")

    try:
        mrkt = await auth_mrkt_token(client)
        print(f"MRKT_TOKEN={mrkt}")
    except Exception as exc:
        print(f"MRKT auth failed: {exc}")

    try:
        portals = await auth_portals_token(client)
        print(f"PORTALS_AUTH={portals[:80]}...")
    except Exception as exc:
        print(f"Portals auth failed: {exc}")

    try:
        tonnel = await auth_tonnel_data(client)
        print(f"TONNEL_AUTH={tonnel[:80]}...")
    except Exception as exc:
        print(f"Tonnel auth failed: {exc}")

    await client.disconnect()
    print(f"Сессия сохранена: {settings.session_path}.session")


if __name__ == "__main__":
    asyncio.run(main())
