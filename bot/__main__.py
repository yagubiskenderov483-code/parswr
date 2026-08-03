from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from bot.auth import auth_mrkt_token, auth_portals_token, auth_tonnel_data, build_telethon_client
from bot.categories import CATEGORY_BY_KEY
from bot.config import load_settings
from bot.handlers import router
from bot.markets import MarketClient, MrktClient, PortalsClient, TonnelClient
from bot.models import Lot
from bot.monitor import LotMonitor
from bot.storage import SeenLotsStore, SubscriptionStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("bot")

MARKET_TITLE = {
    "tonnel": "Tonnel",
    "mrkt": "MRKT",
    "portals": "Portals",
}


def format_lot_message(lot: Lot, category_key: str) -> str:
    cat = CATEGORY_BY_KEY[category_key]
    title = lot.title.replace("<", "&lt;").replace(">", "&gt;")
    market = MARKET_TITLE.get(lot.market, lot.market)
    return (
        f"{cat.emoji} <b>Новый лот · {cat.title}</b>\n"
        f"🏪 {market}\n"
        f"💰 <b>{lot.price_label}</b>\n"
        f"🎁 {title}\n"
        f'🔗 <a href="{lot.url}">Открыть маркет</a>'
    )


async def resolve_market_clients(settings) -> list[MarketClient]:
    mrkt_token = settings.mrkt_token
    portals_auth = settings.portals_auth
    tonnel_auth = settings.tonnel_auth

    need_telethon = ("mrkt" in settings.markets and not mrkt_token) or (
        "portals" in settings.markets and not portals_auth
    )

    session_file = settings.session_path.with_suffix(".session")
    if need_telethon and settings.api_id and settings.api_hash and session_file.exists():
        client = build_telethon_client(
            settings.api_id, settings.api_hash, settings.session_path
        )
        await client.connect()
        if await client.is_user_authorized():
            try:
                if "mrkt" in settings.markets and not mrkt_token:
                    mrkt_token = await auth_mrkt_token(client)
                    logger.info("MRKT token obtained via Telethon")
                if "portals" in settings.markets and not portals_auth:
                    portals_auth = await auth_portals_token(client)
                    logger.info("Portals auth obtained via Telethon")
                if "tonnel" in settings.markets and not tonnel_auth:
                    tonnel_auth = await auth_tonnel_data(client)
                    logger.info("Tonnel auth obtained via Telethon")
            except Exception:
                logger.exception("Failed to auth some markets via Telethon")
        else:
            logger.warning(
                "Telethon session exists but not authorized. "
                "Run: python -m bot.login"
            )
        await client.disconnect()
    elif need_telethon:
        logger.warning(
            "MRKT/Portals нужен логин. Пока работает Tonnel. "
            "Запусти: python -m bot.login  или пропиши MRKT_TOKEN/PORTALS_AUTH в .env"
        )

    clients: list[MarketClient] = []
    for name in settings.markets:
        if name == "tonnel":
            clients.append(TonnelClient(settings.stars_per_ton, tonnel_auth))
        elif name == "mrkt":
            if mrkt_token:
                clients.append(MrktClient(settings.stars_per_ton, mrkt_token))
            else:
                logger.warning("MRKT skipped: no token")
        elif name == "portals":
            if portals_auth:
                clients.append(PortalsClient(settings.stars_per_ton, portals_auth))
            else:
                logger.warning("Portals skipped: no auth")
        else:
            logger.warning("Unknown market: %s", name)
    return clients


async def main() -> None:
    settings = load_settings()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    subs = SubscriptionStore(settings.subs_path)
    seen = SeenLotsStore(settings.seen_path)
    clients = await resolve_market_clients(settings)
    if not clients:
        raise RuntimeError("Нет доступных маркетов для опроса")

    monitor_status = {
        "poll_interval": settings.poll_interval,
        "markets": [c.name for c in clients],
        "stars_per_ton": settings.stars_per_ton,
        "last_fetch_count": 0,
        "new_lots_total": 0,
        "last_error": None,
        "per_market": {},
    }

    async def notify(lot: Lot, category_key: str) -> None:
        text = format_lot_message(lot, category_key)
        for user_id in subs.subscribers_for(category_key):
            try:
                await bot.send_message(
                    user_id,
                    text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
                await bot.send_message(
                    user_id,
                    text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramForbiddenError:
                subs.set(user_id, set())
                logger.warning("User %s blocked bot", user_id)
            except Exception:
                logger.exception("Send failed to %s", user_id)

    monitor = LotMonitor(
        clients=clients,
        seen_store=seen,
        on_new_lot=notify,
        poll_interval=settings.poll_interval,
    )

    async def status_updater() -> None:
        while True:
            monitor_status["last_fetch_count"] = monitor.last_fetch_count
            monitor_status["new_lots_total"] = monitor.new_lots_total
            monitor_status["last_error"] = monitor.last_error
            monitor_status["per_market"] = dict(monitor.per_market)
            await asyncio.sleep(1)

    dp["subs"] = subs
    dp["monitor_status"] = monitor_status
    dp.include_router(router)

    monitor_task = asyncio.create_task(monitor.run(), name="lot-monitor")
    status_task = asyncio.create_task(status_updater(), name="status-updater")
    logger.info(
        "Started markets=%s interval=%ss",
        [c.name for c in clients],
        settings.poll_interval,
    )
    try:
        await dp.start_polling(bot)
    finally:
        monitor.stop()
        monitor_task.cancel()
        status_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
            await status_task


if __name__ == "__main__":
    asyncio.run(main())
