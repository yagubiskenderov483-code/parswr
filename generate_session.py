"""
Одноразовый вход в Telegram: создаёт SESSION_STRING для tracker.py.

Запуск:  python3 generate_session.py
Спросит номер телефона, код из Telegram и пароль 2FA (если стоит).
Результат сам пропишется в .env (строка SESSION_STRING=...).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


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


def _write_session_to_env(session: str) -> None:
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


async def main() -> None:
    _load_dotenv()
    api_id = int(os.environ.get("API_ID", "0") or 0)
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit("Сначала заполни API_ID и API_HASH в .env")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # спросит телефон, код, пароль 2FA
    me = await client.get_me()
    session = StringSession.save(client.session)
    await client.disconnect()

    _write_session_to_env(session)
    print()
    print(f"Готово! Вошёл как: {me.first_name} (@{me.username or '—'})")
    print("SESSION_STRING записан в .env — теперь запускай: python3 tracker.py")


if __name__ == "__main__":
    asyncio.run(main())
