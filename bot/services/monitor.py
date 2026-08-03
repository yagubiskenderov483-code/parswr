from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from bot.database import session_scope
from bot.database.models import AppSettings
from bot.database.repositories import (
    get_or_create_settings,
    is_seen,
    mark_seen,
    seed_seen,
    start_parse_run,
    stop_parse_run,
)
from bot.models import MarketName, UnifiedLot
from bot.parsers.base import BaseMarketParser
from bot.services.converter import PriceConverter
from bot.services.notifier import format_lot_message
from bot.services.rates import RateService

logger = logging.getLogger(__name__)

NotifyFn = Callable[[int, str], Awaitable[None]]


@dataclass
class MonitorState:
    running: bool = False
    run_id: int | None = None
    owner_id: int | None = None
    lots_found: int = 0
    last_tick_at: datetime | None = None
    primed: bool = False
    errors: dict[str, str] = field(default_factory=dict)


class MonitorService:
    def __init__(
        self,
        bot: Bot,
        parsers: list[BaseMarketParser],
        rates: RateService,
        converter: PriceConverter,
    ) -> None:
        self.bot = bot
        self.parsers = parsers
        self.rates = rates
        self.converter = converter
        self.state = MonitorState()
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self.state.running and self._task is not None and not self._task.done()

    async def start(self, user_id: int) -> str:
        if self.is_running:
            return "Парсинг уже запущен."

        async with session_scope() as session:
            run = await start_parse_run(session, user_id)
            self.state.run_id = run.id

        self.state.running = True
        self.state.owner_id = user_id
        self.state.lots_found = 0
        self.state.primed = False
        self._task = asyncio.create_task(self._loop(user_id), name="monitor-loop")
        logger.info("Parser started by user %s", user_id)
        return "▶️ Парсинг запущен по всем включённым маркетам."

    async def stop(self) -> str:
        if not self.state.running:
            return "Парсинг и так остановлен."
        self.state.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.state.run_id is not None:
            async with session_scope() as session:
                await stop_parse_run(session, self.state.run_id, self.state.lots_found)
        logger.info("Parser stopped. lots_found=%s", self.state.lots_found)
        return "⏹ Парсинг остановлен."

    async def _loop(self, user_id: int) -> None:
        while self.state.running:
            started = datetime.now(timezone.utc)
            poll_interval = 2.0
            try:
                async with session_scope() as session:
                    settings = await get_or_create_settings(session, user_id)
                    cfg = _Snapshot.from_row(settings)
                    poll_interval = cfg.poll_interval

                await self._tick(cfg)
                self.state.last_tick_at = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Monitor tick failed: %s", exc)

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            await asyncio.sleep(max(0.5, poll_interval - elapsed))

    async def _tick(self, cfg: "_Snapshot") -> None:
        active = [p for p in self.parsers if _market_enabled(p.name, cfg)]
        if not active:
            logger.warning("No markets enabled")
            return

        results = await asyncio.gather(
            *[p.safe_fetch(limit=30) for p in active],
            return_exceptions=False,
        )

        unified: list[UnifiedLot] = []
        seed_payload: list[tuple[str, MarketName, str]] = []

        for parser, raw_lots in zip(active, results):
            if parser.last_error:
                self.state.errors[parser.name.value] = parser.last_error
            elif parser.name.value in self.state.errors:
                self.state.errors.pop(parser.name.value, None)

            for raw in raw_lots:
                lot = self.converter.unify(raw)
                seed_payload.append((lot.fingerprint, lot.market, lot.external_id))
                if cfg.min_stars <= lot.price_stars <= cfg.max_stars:
                    unified.append(lot)

        if not self.state.primed:
            async with session_scope() as session:
                added = await seed_seen(session, seed_payload)
            self.state.primed = True
            logger.info("Seeded %s existing lots (no notifications)", added)
            return

        # Deduplicate within tick
        seen_local: set[str] = set()
        fresh: list[UnifiedLot] = []
        async with session_scope() as session:
            for lot in unified:
                if lot.fingerprint in seen_local:
                    continue
                seen_local.add(lot.fingerprint)
                if await is_seen(session, lot.fingerprint):
                    continue
                await mark_seen(session, lot)
                fresh.append(lot)

        for lot in fresh:
            logger.info(
                "New lot [%s] %s | %.0f⭐ | %s",
                lot.market.value,
                lot.display_title()[:60],
                lot.price_stars,
                lot.difficulty.value,
            )
            if cfg.notifications_enabled and self.state.owner_id:
                await self._notify(self.state.owner_id, format_lot_message(lot))
            self.state.lots_found += 1

    async def _notify(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(
                user_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
            await self.bot.send_message(
                user_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramForbiddenError:
            logger.warning("User %s blocked the bot", user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Notify failed: %s", exc)


@dataclass(slots=True)
class _Snapshot:
    min_stars: float
    max_stars: float
    poll_interval: float
    notifications_enabled: bool
    market_telegram: bool
    market_portal: bool
    market_mrkt: bool
    market_tonnel: bool

    @classmethod
    def from_row(cls, row: AppSettings) -> "_Snapshot":
        return cls(
            min_stars=row.min_stars,
            max_stars=row.max_stars,
            poll_interval=row.poll_interval,
            notifications_enabled=row.notifications_enabled,
            market_telegram=row.market_telegram,
            market_portal=row.market_portal,
            market_mrkt=row.market_mrkt,
            market_tonnel=row.market_tonnel,
        )


def _market_enabled(name: MarketName, cfg: _Snapshot) -> bool:
    return {
        MarketName.TELEGRAM: cfg.market_telegram,
        MarketName.PORTAL: cfg.market_portal,
        MarketName.MRKT: cfg.market_mrkt,
        MarketName.TONNEL: cfg.market_tonnel,
    }.get(name, False)
