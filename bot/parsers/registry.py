from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import unquote

from telethon import TelegramClient
from telethon.tl.functions.messages import RequestAppWebViewRequest
from telethon.tl.types import InputBotAppShortName, InputUser

from bot import credentials as creds
from bot.config import Settings
from bot.parsers.base import BaseMarketParser
from bot.parsers.mrkt import MrktParser
from bot.parsers.portal import PortalParser
from bot.parsers.telegram_market import TelegramMarketParser
from bot.parsers.tonnel import TonnelParser

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
        raise RuntimeError(f"No tgWebAppData for @{bot_username}")
    return unquote(url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])


async def fetch_mrkt_token(client: TelegramClient) -> str:
    from curl_cffi import requests

    init_data = await get_webapp_init_data(client, "mrkt", "app")
    response = requests.post(
        "https://api.tgmrkt.io/api/v1/auth",
        json={"data": init_data},
        impersonate="chrome",
        timeout=20,
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        raise RuntimeError(f"MRKT auth failed: {response.text[:200]}")
    return token


async def fetch_portals_auth(client: TelegramClient) -> str:
    init_data = await get_webapp_init_data(client, "portals", "market")
    return f"tma {init_data}"


def build_telethon(settings: Settings | None = None) -> TelegramClient:
    session = settings.session_path if settings else Path(creds.TELETHON_SESSION)
    if not session.is_absolute():
        from bot.config.settings import ROOT_DIR

        session = ROOT_DIR / session
    session.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(session), creds.API_ID, creds.API_HASH)


async def build_parsers(settings: Settings) -> list[BaseMarketParser]:
    """Construct marketplace parsers. Missing auth disables only that market."""
    mrkt_token = os.getenv("MRKT_TOKEN", "").strip()
    portals_auth = os.getenv("PORTALS_AUTH", "").strip()
    tonnel_auth = os.getenv("TONNEL_AUTH", "").strip()

    telethon_client: TelegramClient | None = None
    session_file = Path(str(settings.session_path) + ".session")
    if session_file.exists():
        try:
            telethon_client = build_telethon(settings)
            await telethon_client.connect()
            if not await telethon_client.is_user_authorized():
                logger.warning("Telethon session exists but unauthorized")
                await telethon_client.disconnect()
                telethon_client = None
            else:
                if not mrkt_token:
                    try:
                        mrkt_token = await fetch_mrkt_token(telethon_client)
                        logger.info("MRKT token acquired via Telethon")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("MRKT auth failed: %s", exc)
                if not portals_auth:
                    try:
                        portals_auth = await fetch_portals_auth(telethon_client)
                        logger.info("Portal auth ready")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Portal auth failed: %s", exc)
                if not tonnel_auth:
                    try:
                        tonnel_auth = await get_webapp_init_data(
                            telethon_client, "tonnel_network_bot", "gifts"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Tonnel auth failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telethon init failed: %s", exc)
            telethon_client = None

    return [
        TonnelParser(auth=tonnel_auth),
        MrktParser(token=mrkt_token),
        PortalParser(auth=portals_auth),
        TelegramMarketParser(client=telethon_client),
    ]
