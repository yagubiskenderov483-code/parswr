from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from bot import credentials as creds
from bot.database import session_scope
from bot.database.models import AppSettings
from bot.database.repositories import (
    get_or_create_settings,
    mark_seen,
    seed_seen,
    start_parse_run,
    stop_parse_run,
)
from bot.models import MarketName, UnifiedLot
from bot.parsers.base import BaseMarketParser
from bot.services.converter import PriceConverter
from bot.services.notifier import format_lot_message, lot_keyboard
from bot.services.owner import OwnerResolver
from bot.services.rates import RateService
from bot.utils.links import write_url

logger = logging.getLogger(__name__)


@dataclass
class MonitorState:
    running: bool = False
    run_id: int | None = None
    owner_id: int | None = None
    lots_found: int = 0
    last_tick_at: datetime | None = None
    primed: bool = False
    errors: dict[str, str] = field(default_factory=dict)
    ticks: int = 0


class MonitorService:
    def __init__(
        self,
        bot: Bot,
        parsers: list[BaseMarketParser],
        rates: RateService,
        converter: PriceConverter,
        owner_resolver: OwnerResolver | None = None,
    ) -> None:
        self.bot = bot
        self.parsers = parsers
        self.rates = rates
        self.converter = converter
        self.owner_resolver = owner_resolver or OwnerResolver()
        self.state = MonitorState()
        self._task: asyncio.Task | None = None
        self._seen_memory: set[str] = set()
        self._cfg_cache: _Snapshot | None = None
        self._cfg_user: int | None = None

    @property
    def is_running(self) -> bool:
        return self.state.running and self._task is not None and not self._task.done()

    async def start(self, user_id: int) -> str:
        if self.is_running:
            return "♻️ Парсинг уже запущен."

        async with session_scope() as session:
            run = await start_parse_run(session, user_id)
            self.state.run_id = run.id
            settings = await get_or_create_settings(session, user_id)
            # Force fast interval
            if settings.poll_interval > 0.5:
                settings.poll_interval = creds.DEFAULT_POLL_INTERVAL
            self._cfg_cache = _Snapshot.from_row(settings)
            self._cfg_user = user_id

        self.state.running = True
        self.state.owner_id = user_id
        self.state.lots_found = 0
        self.state.primed = False
        self.state.ticks = 0
        self._seen_memory.clear()
        self._task = asyncio.create_task(self._loop(user_id), name="monitor-loop")
        logger.info("Parser STARTED user=%s parsers=%s", user_id, [p.title for p in self.parsers])
        return "▶️ Парсинг запущен."

    async def stop(self) -> str:
        if not self.state.running:
            return "Парсинг уже остановлен."
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
        logger.info("Parser STOPPED lots=%s", self.state.lots_found)
        return "⏹ Парсинг остановлен."

    async def _loop(self, user_id: int) -> None:
        while self.state.running:
            started = datetime.now(timezone.utc)
            poll_interval = creds.DEFAULT_POLL_INTERVAL
            try:
                # Refresh settings every ~20 ticks only
                if self._cfg_cache is None or self.state.ticks % 20 == 0:
                    async with session_scope() as session:
                        settings = await get_or_create_settings(session, user_id)
                        self._cfg_cache = _Snapshot.from_row(settings)
                cfg = self._cfg_cache
                poll_interval = max(0.15, min(cfg.poll_interval, 0.5))
                await self._tick(cfg)
                self.state.last_tick_at = datetime.now(timezone.utc)
                self.state.ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Monitor tick failed: %s", exc)

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            await asyncio.sleep(max(0.05, poll_interval - elapsed))

    async def _tick(self, cfg: "_Snapshot") -> None:
        active = [p for p in self.parsers if _market_enabled(p.name, cfg)]
        if not active:
            logger.warning("No markets enabled")
            return

        results = await asyncio.gather(
            *[p.safe_fetch(limit=25) for p in active],
            return_exceptions=False,
        )

        unified: list[UnifiedLot] = []
        seed_payload: list[tuple[str, MarketName, str]] = []
        matched_preview: list[UnifiedLot] = []

        for parser, raw_lots in zip(active, results):
            if parser.last_error:
                self.state.errors[parser.name.value] = parser.last_error
                logger.warning("Market %s error: %s", parser.title, parser.last_error)
            else:
                self.state.errors.pop(parser.name.value, None)

            for raw in raw_lots:
                lot = self.converter.unify(raw)
                seed_payload.append((lot.fingerprint, lot.market, lot.external_id))
                if cfg.min_stars <= lot.price_stars <= cfg.max_stars:
                    unified.append(lot)
                    matched_preview.append(lot)

        logger.info(
            "tick#%s markets=%s fetched=%s matched=%s",
            self.state.ticks,
            {p.title: len(batch) if not isinstance(batch, Exception) else 0 for p, batch in zip(active, results)},
            len(seed_payload),
            len(matched_preview),
        )

        if not self.state.primed:
            self._seen_memory |= {fp for fp, _, _ in seed_payload}
            async with session_scope() as session:
                await seed_seen(session, seed_payload)
            self.state.primed = True
            logger.info("Seeded %s lots", len(seed_payload))

            # Send up to 3 newest matched lots RIGHT NOW so user sees it works
            preview = matched_preview[:3]
            if preview and cfg.notifications_enabled and self.state.owner_id:
                await self.bot.send_message(
                    self.state.owner_id,
                    f"📡 Парсер живой. Свежие лоты в диапазоне "
                    f"{int(cfg.min_stars)}–{int(cfg.max_stars)} ⭐ "
                    f"(показал {len(preview)} шт., дальше только новые):",
                )
                for lot in preview:
                    await self._enrich_owner(lot)
                    async with session_scope() as session:
                        await mark_seen(session, lot)
                    await self._notify(
                        self.state.owner_id,
                        format_lot_message(lot),
                        reply_markup=lot_keyboard(lot),
                    )
                    self.state.lots_found += 1
            elif self.state.owner_id:
                await self.bot.send_message(
                    self.state.owner_id,
                    f"📡 Парсер живой, лотов в диапазоне пока нет "
                    f"({int(cfg.min_stars)}–{int(cfg.max_stars)} ⭐). Жду новые…\n"
                    f"Ошибки: {self.state.errors or 'нет'}",
                )
            return

        fresh = [lot for lot in unified if lot.fingerprint not in self._seen_memory]
        for lot in fresh:
            self._seen_memory.add(lot.fingerprint)

        if not fresh:
            return

        # Notify immediately, enrich owner best-effort without blocking too long
        for lot in fresh:
            await self._enrich_owner(lot)
            logger.info(
                "NEW [%s] %.0f⭐ %s @%s",
                lot.market.value,
                lot.price_stars,
                lot.display_title()[:40],
                lot.seller_username or "—",
            )
            if cfg.notifications_enabled and self.state.owner_id:
                await self._notify(
                    self.state.owner_id,
                    format_lot_message(lot),
                    reply_markup=lot_keyboard(lot),
                )
            self.state.lots_found += 1

        async with session_scope() as session:
            for lot in fresh:
                await mark_seen(session, lot)

    async def _enrich_owner(self, lot: UnifiedLot) -> None:
        if lot.seller_username:
            return
        try:
            username = await asyncio.wait_for(
                self.owner_resolver.resolve(
                    title=lot.title,
                    number=lot.number,
                    nft_url=lot.nft_url,
                    current_username=lot.seller_username,
                ),
                timeout=0.8,
            )
        except Exception:  # noqa: BLE001
            return
        if not username:
            return
        lot.seller_username = username.lstrip("@")
        lot.write_url = write_url(
            seller_username=lot.seller_username,
            seller_id=lot.seller_id,
            market_url=lot.url,
        )

    async def reload_parsers(self, parsers: list[BaseMarketParser]) -> None:
        self.parsers = parsers
        logger.info("Parsers reloaded: %s", [p.title for p in parsers])

    async def _notify(self, user_id: int, text: str, reply_markup=None) -> None:
        try:
            await self.bot.send_message(
                user_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=reply_markup,
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.1)
            await self.bot.send_message(
                user_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=reply_markup,
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
