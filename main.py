from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import get_settings
from bot.database import init_db
from bot.handlers import setup_routers
from bot.parsers import build_parsers
from bot.services import MonitorService, PriceConverter, RateService
from bot.utils import setup_logging

logger = logging.getLogger("main")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    await init_db()

    rates = RateService()
    await rates.load_cached()
    try:
        await rates.refresh()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial rates refresh failed: %s", exc)

    parsers = await build_parsers(settings)
    converter = PriceConverter(rates)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    monitor = MonitorService(bot=bot, parsers=parsers, rates=rates, converter=converter)

    dp["monitor"] = monitor
    dp["rates"] = rates
    dp.include_router(setup_routers())

    logger.info(
        "Bot started. Markets: %s",
        ", ".join(p.title for p in parsers),
    )
    try:
        await dp.start_polling(bot)
    finally:
        if monitor.is_running:
            await monitor.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
