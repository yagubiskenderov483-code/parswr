from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonCommands

from bot import credentials as creds
from bot.config import get_settings
from bot.database import init_db
from bot.handlers import setup_routers
from bot.keyboards import bot_commands
from bot.parsers import build_parsers
from bot.services import MonitorService, PriceConverter, RateService
from bot.services.auth import AuthService
from bot.utils import setup_logging

logger = logging.getLogger("main")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    await init_db()

    logger.info("API_ID=%s", creds.API_ID)

    rates = RateService()
    await rates.load_cached()
    try:
        await rates.refresh()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rates failed: %s", exc)

    auth = AuthService(settings)
    if await auth.is_authorized():
        logger.info("Telethon OK: %s", auth.authorized_as)
        try:
            await auth.refresh_market_tokens()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Token refresh failed: %s", exc)
        parsers = await auth.build_authorized_parsers()
    else:
        logger.warning("No login — Tonnel+Portal available")
        parsers = await build_parsers(settings)

    bot = Bot(
        token=creds.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.set_my_commands(bot_commands())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    dp = Dispatcher(storage=MemoryStorage())
    monitor = MonitorService(
        bot=bot,
        parsers=parsers,
        rates=rates,
        converter=PriceConverter(rates),
        owner_resolver=auth.owner_resolver,
    )
    dp["monitor"] = monitor
    dp["rates"] = rates
    dp["auth"] = auth
    dp.include_router(setup_routers())

    logger.info("Ready parsers=%s poll=%s", [p.title for p in parsers], creds.DEFAULT_POLL_INTERVAL)
    try:
        await dp.start_polling(bot)
    finally:
        if monitor.is_running:
            await monitor.stop()
        if auth.client and auth.client.is_connected():
            await auth.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
