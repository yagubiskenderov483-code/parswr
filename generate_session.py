"""Одноразовый вход: python3 generate_session.py → SESSION_STRING в .env."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

import config

ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_dotenv() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def _write_session(session: str) -> None:
    lines: list[str] = []
    replaced = False
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SESSION_STRING="):
                lines.append(f"SESSION_STRING={session}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"SESSION_STRING={session}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path = config.session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session, encoding="utf-8")


async def main() -> None:
    _load_dotenv()
    client = TelegramClient(StringSession(), config.api_id(), config.api_hash())
    await client.start()
    me = await client.get_me()
    session = StringSession.save(client.session)
    await client.disconnect()
    _write_session(session)
    print()
    print(f"Готово! Вошёл как: {me.first_name} (@{me.username or '—'})")
    print("Сессия записана. Запускай: python3 main.py")


if __name__ == "__main__":
    asyncio.run(main())
