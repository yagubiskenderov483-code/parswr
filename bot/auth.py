from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote

from telethon import TelegramClient
from telethon.tl.functions.messages import RequestAppWebViewRequest
from telethon.tl.types import InputBotAppShortName, InputUser

logger = logging.getLogger(__name__)


async def get_webapp_init_data(
    client: TelegramClient,
    bot_username: str,
    short_name: str,
) -> str:
    bot_entity = await client.get_entity(bot_username)
    peer = await client.get_input_entity(bot_username)
    bot = InputUser(user_id=bot_entity.id, access_hash=bot_entity.access_hash)
    web_view = await client(
        RequestAppWebViewRequest(
            peer=peer,
            app=InputBotAppShortName(bot_id=bot, short_name=short_name),
            platform="android",
        )
    )
    url = web_view.url
    if "tgWebAppData=" not in url:
        raise RuntimeError(f"No tgWebAppData in webview url for @{bot_username}")
    return unquote(url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])


async def auth_mrkt_token(client: TelegramClient) -> str:
    from curl_cffi import requests

    init_data = await get_webapp_init_data(client, "mrkt", "app")
    r = requests.post(
        "https://api.tgmrkt.io/api/v1/auth",
        json={"data": init_data},
        impersonate="chrome",
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        raise RuntimeError(f"MRKT auth failed: {r.text[:300]}")
    return token


async def auth_portals_token(client: TelegramClient) -> str:
    init_data = await get_webapp_init_data(client, "portals", "market")
    return f"tma {init_data}"


async def auth_tonnel_data(client: TelegramClient) -> str:
    # Tonnel часто принимает сырой initData как user_auth
    return await get_webapp_init_data(client, "tonnel_network_bot", "gifts")


def build_telethon_client(api_id: int, api_hash: str, session_path: Path) -> TelegramClient:
    if not api_id or not api_hash:
        raise RuntimeError("API_ID / API_HASH не заданы")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(session_path), api_id, api_hash)
