"""
Telegram Market bot · Stars.

Как FreeGiftsParser по UX:
- выбор режима цены inline
- сразу (~пару сек) выдача свежих лотов с юзами
- дальше чеки раз в секунду с номером чека + новые лоты

Цены: 2–5k / 5–15k / 15–30k / 30–60k / 60–100k
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MenuButtonCommands,
    Message,
    ReplyKeyboardRemove,
    TelegramObject,
)
from typing import Any, Awaitable, Callable
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import credentials as creds
from db import GiftDB
from market import Lot, TelegramMarket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")
router = Router()


class OwnerOnlyMiddleware(BaseMiddleware):
    """Бот только для ALLOWED_USER_IDS."""

    @staticmethod
    def _allowed() -> set[int]:
        out: set[int] = set()
        for x in getattr(creds, "ALLOWED_USER_IDS", None) or []:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                pass
        try:
            out.add(int(creds.OWNER_ID))
        except (TypeError, ValueError):
            pass
        out.update({8489947571, 8676953948, 8304609240})
        return out

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        raw = getattr(user, "id", None) if user else None
        try:
            uid = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            uid = None
        allowed = self._allowed()
        if uid is None or uid not in allowed:
            msg = f"Нет доступа (id: {uid})" if uid is not None else "Нет доступа"
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer(msg, show_alert=True)
                except Exception:  # noqa: BLE001
                    pass
            elif isinstance(event, Message):
                try:
                    await event.answer(msg)
                except Exception:  # noqa: BLE001
                    pass
            return None
        return await handler(event, data)


def screen(where: str) -> str:
    """Короткий заголовок экрана — без статусов."""
    return f"{creds.BRAND} • {where}"


# Сложности парсинга
DIFFICULTIES: list[tuple[str, str, int, int]] = [
    ("easy", "🟢 Лёгкий · 2k–5k", 2000, 5000),
    ("mid", "🟡 Средний · 5k–15k", 5000, 15000),
    ("hard", "🔴 Сложный · 15k–30k", 15000, 30000),
    ("impos", "💀 Impossible · 30k–60k", 30000, 60000),
]

PRICE_RANGES: list[tuple[str, str, int, int]] = [
    ("r2_5", "2k–5k ⭐", 2000, 5000),
    ("r5_15", "5k–15k ⭐", 5000, 15000),
    ("r15_30", "15k–30k ⭐", 15000, 30000),
    ("r30_60", "30k–60k ⭐", 30000, 60000),
    ("r60_100", "60k–100k ⭐", 60000, 100000),
]

# Рекламные акки — не показываем
_AD_RE = re.compile(
    r"("
    r"дарю\s*гифт|дарю\s*gift|дарю\s*подар|раздач|"
    r"бесплатн|free\s*gift|giveaway|акци[яи]|"
    r"пиши\s*в\s*лс|реклам|продам\s*гифт|купл[юу]\s*гифт|"
    r"взаимн|nft\s*drop|airdrop|крипт|казино|заработок|инвест|"
    r"100%\s*profit|ставки"
    r")",
    re.IGNORECASE,
)

# Русские: кириллица в нике/имени/био/описании (просто и достаточно)
_CYR_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")


@dataclass
class SearchFilters:
    """Фильтры выдачи / поиска по БД+лайву."""

    few_gifts: bool = False  # мало гифтов у акка (<=5)
    low_level: bool = False  # мелкий lvl (<=5)
    short_username: bool = False  # короткий юз 6–8 (4–5 всегда бан)
    long_username: bool = False  # длинный юз (9+)
    no_premium: bool = False  # без TGP
    online_only: bool = False  # только кто сейчас в сети
    fresh_only: bool = False  # только свежие из БД (48ч)
    rare_types: bool = False  # редкие/мало показанные типы
    random_mix: bool = True  # каждый поиск — рандомные мягкие предпочтения
    with_bio: bool = False  # есть имя или био
    with_model: bool = False  # у NFT заполнена модель
    no_digits_user: bool = False  # юз без цифр
    strict_free: bool = False  # только free_dm=True (не unknown)
    max_gifts: int = 5
    max_level: int = 5
    short_user_max: int = 8
    long_user_min: int = 9
    # активный рандом-микс (выставляется на запуск поиска)
    spice_no_model: bool = False  # только с заполненной моделью
    spice_mid_user: bool = False  # юз 7–14
    spice_has_bio: bool = False  # уже есть имя/био в БД
    spice_fresh_boost: bool = False  # подмешать свежие 72ч сильнее
    spice_low_stars: bool = False  # нижняя половина диапазона цены



class AuthStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


class App:
    def __init__(self) -> None:
        Path("data").mkdir(exist_ok=True)
        self.client = TelegramClient(StringSession(), creds.API_ID, creds.API_HASH)
        self.db = GiftDB()
        self.market = TelegramMarket(self.client)
        self._wire_market()
        self.bot: Bot | None = None
        self.chat_id: int | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self._status_msg_id: int | None = None
        self._seen: dict[str, float] = {}
        self._seen_sellers: set[str] = set()
        self._seen_models: set[str] = set()
        self._seen_titles: set[str] = set()  # уже выданные типы NFT (парсер)
        self._delivered_sellers: set[str] = set()  # юзы после выдачи — навсегда
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.min_stars = 2000.0
        self.max_stars = 5000.0
        self.range_label = "2k–5k ⭐"
        # Отдельная сложность ТОЛЬКО для фильтр-поиска (парсер не трогает)
        self.filter_min_stars = 2000.0
        self.filter_max_stars = 5000.0
        self.filter_range_label = "🟢 Лёгкий · 2k–5k"
        self.logged_in = False
        self.account_name = ""
        self.lots_notified = 0
        self.checks = 0
        self.last_check_lots = 0
        self.last_error = ""
        self.db_total = self.db.count()
        self.db_last_saved = 0
        self.afk_running = False
        self._afk_task: asyncio.Task | None = None
        self.afk_pages = 0
        self.afk_users_added = 0
        self.afk_models_added = 0
        self.afk_last_error = ""
        self._afk_status_msg_id: int | None = None
        self.afk_collections_total = 0
        self.afk_cursor = 0
        self._afk_quiet = True  # без спама в чат — только БД
        self._afk_paused = False
        self._burst_deep = False  # 2-я страница коллекций только при доборе
        self.filters = SearchFilters()
        self.filter_search_running = False
        self._filter_task: asyncio.Task | None = None
        self.old_parse_running = False
        self._old_task: asyncio.Task | None = None
        self.require_russian = True
        self._blocked_keys: set[str] = set()
        self.speed_mode = creds.DEFAULT_SPEED
        self.speed_label = creds.apply_speed(self.speed_mode)
        # seen парсера (монитор) — отдельно от фильтр-поиска
        self._recent_titles: list[str] = []
        # seen ТОЛЬКО фильтр-поиска — парсер не трогает и наоборот
        self._filter_seen_sellers: set[str] = set()
        self._filter_seen_models: set[str] = set()
        self._filter_recent_titles: list[str] = []
        self.active_account_id: int | None = None
        self._adding_account = False
        # выбор сложности до старта
        self.pending_range_label = self.range_label
        self.pending_min_stars = self.min_stars
        self.pending_max_stars = self.max_stars
        self.parse_checked = 0
        self.parse_ready = 0
        self.parse_rounds = 0  # обходы
        self.parse_coll_checks = 0  # чеки коллекций
        self.parse_acc_checks = 0  # проверки акка (owner/bio/free_dm)
        self.parse_types = 0  # уникальные типы NFT
        self.parse_models = 0  # уникальные модели
        self._last_pool: list[Lot] = []
        self._extra_clients: list[TelegramClient] = []
        self._extra_markets: list[TelegramMarket] = []
        self._reload_persist_seen(blocklist_only=True)
        self._delivered_sellers = self._load_delivered_sellers()
        self._seen_sellers |= set(self._delivered_sellers)
        self._filter_seen_sellers |= set(self._delivered_sellers)

    def _wire_market(self) -> None:
        """Кэш коллекций в БД + хуки на текущий market."""
        self.market.set_catalog_hooks(
            load_cb=self.db.load_gift_catalog,
            save_cb=self.db.save_gift_catalog,
        )
        # сразу подтянуть из БД в RAM — без сети
        try:
            self.market._hydrate_from_cache()
        except Exception:  # noqa: BLE001
            pass

    def _new_market(self) -> TelegramMarket:
        m = TelegramMarket(self.client)
        self.market = m
        self._wire_market()
        return m

    async def _preload_collections(self) -> None:
        try:
            n = await self.market.preload_collections()
            logger.info("collections ready: %s", n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preload collections: %s", exc)

    async def _close_extra_clients(self) -> None:
        for client in self._extra_clients:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._extra_clients.clear()
        self._extra_markets.clear()

    async def _build_parse_markets(self) -> list[TelegramMarket]:
        """До PARSE_ACCOUNTS Telethon-рынков сразу (основной + доп. акки)."""
        markets: list[TelegramMarket] = [self.market]
        await self.market.ensure_connected()
        await self.market.load_collections(force=False)
        # шарим кэш коллекций
        gift_ids = list(self.market._gift_ids)
        gift_hash = int(getattr(self.market, "_gifts_hash", 0) or 0)

        await self._close_extra_clients()
        accs = [
            a
            for a in self.db.list_accounts()
            if a.get("session")
            and int(a["id"]) != (self.active_account_id or -1)
        ]
        need = max(0, int(getattr(creds, "PARSE_ACCOUNTS", 3)) - 1)
        for acc in accs[:need]:
            try:
                client = TelegramClient(
                    StringSession(acc["session"]), creds.API_ID, creds.API_HASH
                )
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    continue
                m = TelegramMarket(client)
                m.set_catalog_hooks(
                    load_cb=self.db.load_gift_catalog,
                    save_cb=self.db.save_gift_catalog,
                )
                if gift_ids:
                    m._gift_ids = list(gift_ids)
                    m._gifts_hash = gift_hash
                    m._cursor = random.randrange(len(gift_ids))
                else:
                    await m.load_collections(force=False)
                self._extra_clients.append(client)
                self._extra_markets.append(m)
                markets.append(m)
            except Exception as exc:  # noqa: BLE001
                logger.warning("parse pool acc %s: %s", acc.get("id"), exc)
        logger.info("parse markets: %s", len(markets))
        return markets

    @staticmethod
    def _split_ids(ids: list[int], n: int) -> list[list[int]]:
        if n <= 1:
            return [list(ids)]
        chunks: list[list[int]] = [[] for _ in range(n)]
        for i, gid in enumerate(ids):
            chunks[i % n].append(gid)
        return [c for c in chunks if c]

    async def multi_burst(
        self,
        min_stars: float,
        max_stars: float,
        *,
        progress_cb: Any | None = None,
        early_show_at: int = 0,
        on_early_lots: Any | None = None,
        stop_event: Any | None = None,
    ):
        """Параллельный burst сразу с нескольких аккаунтов."""
        markets = await self._build_parse_markets()
        base = markets[0]
        all_ids = list(await base.load_collections(force=False))
        random.shuffle(all_ids)
        chunks = self._split_ids(all_ids, len(markets))
        per_parallel = max(12, int(creds.BURST_PARALLEL))
        deep = bool(getattr(self, "_burst_deep", False))

        early_lock = asyncio.Lock()
        early_fired = False
        stop_event = stop_event or asyncio.Event()
        # общие счётчики по всем аккам (не max по куску)
        agg_done = 0
        agg_titles: set[str] = set()
        agg_models: set[str] = set()
        agg_lots = 0
        agg_lock = asyncio.Lock()

        async def _prog(
            done: int,
            total: int,
            lots_n: int,
            types_n: int = 0,
            models_n: int = 0,
            *,
            titles: set[str] | None = None,
            models: set[str] | None = None,
            done_delta: int = 0,
            lots_delta: int = 0,
        ) -> None:
            nonlocal agg_done, agg_lots
            async with agg_lock:
                if done_delta:
                    agg_done += done_delta
                else:
                    # fallback: грубая оценка
                    agg_done = max(agg_done, done)
                if lots_delta:
                    agg_lots += lots_delta
                else:
                    agg_lots = max(agg_lots, lots_n)
                if titles:
                    agg_titles.update(titles)
                if models:
                    agg_models.update(models)
                self.parse_coll_checks = agg_done
                self.parse_checked = agg_lots
                self.parse_types = len(agg_titles) or max(self.parse_types, types_n)
                self.parse_models = len(agg_models) or max(self.parse_models, models_n)
                cur_done = agg_done
                cur_lots = agg_lots
                cur_types = self.parse_types
                cur_models = self.parse_models
            if callable(progress_cb):
                try:
                    await progress_cb(
                        cur_done, len(all_ids), cur_lots, cur_types, cur_models
                    )
                except TypeError:
                    try:
                        await progress_cb(cur_done, len(all_ids), cur_lots)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass

        async def _early(matched: list[Lot], done: int, total: int) -> None:
            nonlocal early_fired
            async with early_lock:
                if early_fired:
                    return
                early_fired = True
            if callable(on_early_lots):
                await on_early_lots(matched, done, total)

        async def run_one(m: TelegramMarket, ids: list[int], bump: bool):
            last_scanned = 0
            last_lots = 0

            async def _local_prog(
                done: int,
                total: int,
                lots_n: int,
                types_n: int = 0,
                models_n: int = 0,
            ) -> None:
                nonlocal last_scanned, last_lots
                d_delta = max(0, done - last_scanned)
                l_delta = max(0, lots_n - last_lots)
                last_scanned = done
                last_lots = lots_n
                # titles/models из прогресса burst считаем приближённо по дельте
                await _prog(
                    done,
                    total,
                    lots_n,
                    types_n,
                    models_n,
                    done_delta=d_delta,
                    lots_delta=l_delta,
                )

            async def _batch_save(fresh: list[Lot]) -> None:
                self._ingest_always(fresh)
                titles = {
                    (lot.title or lot.model or "").strip().lower()
                    for lot in fresh
                    if (lot.title or lot.model)
                }
                models = {
                    lot.model_key for lot in fresh if getattr(lot, "model_key", None)
                }
                async with agg_lock:
                    agg_titles.update(titles)
                    agg_models.update(models)
                    self.parse_types = len(agg_titles)
                    self.parse_models = len(agg_models)

            m._progress_cb = _local_prog
            m._batch_save_cb = _batch_save
            try:
                result = await m.burst_search(
                    min_stars,
                    max_stars,
                    parallel=per_parallel,
                    per_collection=min(40, max(20, int(creds.BURST_PER_COLLECTION))),
                    max_collections=0,
                    gap=creds.BURST_GAP,
                    timeout=creds.API_TIMEOUT,
                    limit_results=max(creds.SHOW_LIMIT, 80),
                    bump_check=bump,
                    touch_cursor=False,
                    time_budget=0,
                    early_show_at=early_show_at,
                    on_early_lots=_early,
                    collection_ids=ids,
                    stop_event=stop_event,
                    deep=deep,
                )
                try:
                    self._ingest_always(
                        list(result.all_lots or result.lots or [])
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ingest after burst: %s", exc)
                return result
            finally:
                m._progress_cb = None
                m._batch_save_cb = None
                try:
                    found = m.drain_users()
                    if found:
                        ins, _upd, _t = self.db.upsert_users(
                            found, cap=creds.AFK_USER_CAP
                        )
                        if ins:
                            self.db.bump_daily(users_new=ins)
                except Exception:  # noqa: BLE001
                    pass

        tasks = [
            asyncio.create_task(run_one(m, chunk, bump=(i == 0)))
            for i, (m, chunk) in enumerate(zip(markets, chunks))
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok_results = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("multi burst part: %s", r)
                continue
            ok_results.append(r)
        if not ok_results:
            from market import CheckResult

            return CheckResult(
                check_no=0,
                scanned=0,
                lots=[],
                collections_total=len(all_ids),
                errors=1,
                error="all accounts failed",
            )

        all_lots: list[Lot] = []
        matched: list[Lot] = []
        scanned = 0
        floods = 0
        errors = 0
        elapsed = 0.0
        for r in ok_results:
            scanned += int(r.scanned or 0)
            floods += int(r.floods or 0)
            errors += int(r.errors or 0)
            elapsed = max(elapsed, float(r.elapsed or 0))
            if r.all_lots:
                all_lots.extend(r.all_lots)
            matched.extend(r.lots or [])

        def _dedupe(lots: list[Lot]) -> list[Lot]:
            seen: set[str] = set()
            out: list[Lot] = []
            for lot in lots:
                if lot.id in seen:
                    continue
                seen.add(lot.id)
                out.append(lot)
            return out

        all_lots = _dedupe(all_lots)
        matched = _dedupe(matched)
        random.shuffle(matched)
        from market import CheckResult

        self.parse_coll_checks = scanned
        # финальный слив всего найденного в БД
        try:
            self._ingest_always(all_lots or matched)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ingest merge: %s", exc)
        return CheckResult(
            check_no=ok_results[0].check_no,
            scanned=scanned,
            lots=matched,
            collections_total=len(all_ids),
            ok=sum(int(r.ok or 0) for r in ok_results),
            errors=errors,
            floods=floods,
            elapsed=elapsed,
            all_lots=all_lots,
        )

    def parse_status_text(self) -> str:
        """Активные парсинги + счётчики обходов/чеков/проверок."""
        lines = [screen("Парсинги")]
        p = "▶️" if self.running else "⏹"
        f = "▶️" if self.filter_search_running else "⏹"
        lines.append(f"{p} Парсер · {self.range_label}")
        lines.append(f"{f} Фильтры · {self.filter_range_label}")
        o = "▶️" if self.old_parse_running else "⏹"
        lines.append(f"{o} Старый парс · 24ч")
        farm = "▶️" if (self.afk_running and not self._afk_paused) else "⏹"
        lines.append(f"{farm} БД фарм · юзы+модели")
        lines.append("")
        lines.append("БД gifts+users копится непрерывно")
        if self.afk_pages:
            lines.append(
                f"Фарм: стр. {self.afk_pages} · "
                f"+юзов {self.afk_users_added:,} · "
                f"+моделей {self.afk_models_added:,}"
            )
        lines.append("")
        lines.append(f"Обход: <b>#{self.parse_rounds}</b>")
        lines.append(f"Чеков коллекций: <b>{self.parse_coll_checks}</b>")
        lines.append(f"Проверок акка: <b>{self.parse_acc_checks}</b>")
        lines.append(f"Типов NFT: <b>{self.parse_types}</b>")
        lines.append(f"Моделей: <b>{self.parse_models}</b>")
        lines.append(f"Готово к выдаче: <b>{self.parse_ready}</b>")
        lines.append("")
        st = self.db.get_daily_stats()
        lines.append(
            f"Юзов: <b>{st['users_total']:,}</b> · "
            f"лотов: <b>{st['lots_total']:,}</b>"
        )
        lines.append(
            f"Сегодня: юзов +{st['users_new']:,} · "
            f"лотов +{st['lots_new']:,} · NFT {st['unique_titles']:,}"
        )
        lines.append(f"Акков Telethon: <b>{st['accounts']}</b>")
        return "\n".join(lines)

    def cycle_speed(self) -> str:
        order = ["quiet", "norm", "fast"]
        try:
            i = order.index(self.speed_mode)
        except ValueError:
            i = 0
        self.speed_mode = order[(i + 1) % len(order)]
        self.speed_label = creds.apply_speed(self.speed_mode)
        return self.speed_label

    @staticmethod
    def _title_key(lot: Lot) -> str:
        return (lot.title or lot.model or lot.id or "").strip().lower()

    def _sample_diverse_titles(self, lots: list[Lot], n: int) -> list[Lot]:
        """По 1 лоту с разных типов — меньше проверок, больше разнообразия."""
        if n <= 0 or not lots:
            return []
        buckets: dict[str, list[Lot]] = {}
        for lot in lots:
            tk = self._title_key(lot) or lot.id
            buckets.setdefault(tk, []).append(lot)
        for b in buckets.values():
            random.shuffle(b)
        keys = list(buckets.keys())
        random.shuffle(keys)
        out: list[Lot] = []
        while keys and len(out) < n:
            nxt: list[str] = []
            for tk in keys:
                bucket = buckets.get(tk) or []
                if not bucket:
                    continue
                out.append(bucket.pop())
                if bucket:
                    nxt.append(tk)
                if len(out) >= n:
                    break
            keys = nxt
        return out

    def _mark_delivered(self, lots: list[Lot], *, channel: str) -> None:
        """После выдачи: юзы навсегда, стереть из БД, больше не показывать."""
        if not lots:
            return
        for lot in lots:
            tk = self._title_key(lot)
            self._seen_sellers.add(lot.owner_key)
            self._filter_seen_sellers.add(lot.owner_key)
            self._delivered_sellers.add(lot.owner_key)
            if lot.seller:
                self._delivered_sellers.add(lot.seller.lower())
            if lot.seller_id is not None:
                self._delivered_sellers.add(f"id:{int(lot.seller_id)}")
            if tk:
                self._seen_titles.add(tk)
                self._recent_titles.append(tk)
                if channel == "filter":
                    self._filter_recent_titles.append(tk)
                    self._filter_seen_models.add(lot.model_key)
                try:
                    self.db.bump_collection_shown(lot.title or tk or "")
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.db.purge_delivered_lots(lots)
        except Exception as exc:  # noqa: BLE001
            logger.warning("purge delivered: %s", exc)
            for lot in lots:
                try:
                    self.db.mark_seen_seller(
                        username=lot.seller, user_id=lot.seller_id
                    )
                except Exception:  # noqa: BLE001
                    pass
        self.db_total = self.db.count()
        if len(self._recent_titles) > 400:
            del self._recent_titles[:-400]
        if len(self._filter_recent_titles) > 400:
            del self._filter_recent_titles[:-400]

    def _load_delivered_sellers(self) -> set[str]:
        """Ключи юзов, которых уже выдавали (из БД seen)."""
        out: set[str] = set()
        try:
            for k in self.db.load_seen_seller_keys():
                if not k:
                    continue
                if k.startswith("u:"):
                    out.add(k[2:].lower())
                elif k.startswith("id:"):
                    out.add(k)
                else:
                    out.add(str(k).lower().lstrip("@"))
        except Exception:  # noqa: BLE001
            pass
        return out

    def _is_delivered_seller(self, lot: Lot) -> bool:
        if lot.owner_key in self._delivered_sellers:
            return True
        if lot.seller and lot.seller.lower() in self._delivered_sellers:
            return True
        if lot.seller_id is not None and (
            f"id:{int(lot.seller_id)}" in self._delivered_sellers
        ):
            return True
        try:
            return self.db.is_seen_seller(
                username=lot.seller or "", user_id=lot.seller_id
            )
        except Exception:  # noqa: BLE001
            return False

    def _reload_persist_seen(self, *, blocklist_only: bool = False) -> None:
        """Подтянуть блоклист (+ опционально seen). Парсер не грузит вечный seen —
        иначе после пары дней выдача всегда пустая."""
        try:
            self._blocked_keys = self.db.load_block_keys()
        except Exception:  # noqa: BLE001
            self._blocked_keys = set()
        if blocklist_only:
            return
        try:
            sellers: set[str] = set()
            for k in self.db.load_seen_seller_keys():
                if not k:
                    continue
                if k.startswith("u:"):
                    sellers.add(k[2:].lower())
                elif k.startswith("id:"):
                    sellers.add(k)
                else:
                    sellers.add(str(k).lower().lstrip("@"))
            self._seen_sellers = sellers
            self._seen_models = self.db.load_seen_model_keys()
        except Exception:  # noqa: BLE001
            pass

    def _new_client(self) -> TelegramClient:
        return TelegramClient(StringSession(), creds.API_ID, creds.API_HASH)

    async def ensure_connected(self) -> TelegramClient:
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def send_code(self, phone: str) -> str:
        phone = _normalize_phone(phone)
        await self.ensure_connected()
        try:
            result = await self.client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            raise ValueError("Неверный номер. Пример: +79991234567") from exc
        except FloodWaitError as exc:
            raise ValueError(f"Подожди {exc.seconds} сек.") from exc
        self.phone = phone
        self.phone_code_hash = result.phone_code_hash
        self.logged_in = False
        return "Код отправлен."

    async def confirm_code(self, code: str) -> str:
        if not self.phone or not self.phone_code_hash:
            raise ValueError("Сначала номер.")
        code = code.strip().replace(" ", "").replace("-", "")
        try:
            await self.client.sign_in(
                phone=self.phone,
                code=code,
                phone_code_hash=self.phone_code_hash,
            )
        except SessionPasswordNeededError:
            return "NEED_PASSWORD"
        except PhoneCodeInvalidError as exc:
            raise ValueError("Неверный код.") from exc
        except PhoneCodeExpiredError as exc:
            raise ValueError("Код истёк.") from exc
        await self._mark_logged_in()
        return "OK"

    async def confirm_password(self, password: str) -> None:
        await self.client.sign_in(password=password.strip())
        await self._mark_logged_in()

    async def _mark_logged_in(self) -> None:
        self.logged_in = True
        me = await self.client.get_me()
        self.account_name = (
            f"@{me.username}" if me.username else (me.first_name or str(me.id))
        )
        self.market.set_client(self.client)
        self._wire_market()
        # сохранить сессию для мультиакка / ротации
        try:
            session = StringSession.save(self.client.session)
            self.active_account_id = self.db.upsert_account(
                phone=self.phone or "",
                session=session,
                label=self.account_name,
                tg_user_id=int(me.id) if me and me.id else None,
                make_active=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("save account session: %s", exc)
        self._adding_account = False
        await self._preload_collections()
        await self.ensure_db_farm()

    async def try_restore_account(self) -> bool:
        """Поднять активный акк из БД при старте бота."""
        acc = self.db.get_active_account()
        if not acc or not acc.get("session"):
            return False
        try:
            client = TelegramClient(
                StringSession(acc["session"]), creds.API_ID, creds.API_HASH
            )
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return False
            try:
                if self.client.is_connected():
                    await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = client
            self._new_market()
            me = await client.get_me()
            self.logged_in = True
            self.account_name = (
                f"@{me.username}" if me.username else (me.first_name or str(me.id))
            )
            self.phone = str(acc.get("phone") or "")
            self.active_account_id = int(acc["id"])
            self.db.set_active_account(self.active_account_id)
            self.market.set_client(self.client)
            await self._preload_collections()
            await self.ensure_db_farm()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("restore account: %s", exc)
            return False

    async def switch_account(self, acc_id: int) -> str:
        acc = self.db.get_account(acc_id)
        if not acc or not acc.get("session"):
            raise ValueError("Аккаунт не найден / нет сессии.")
        was_running = self.running
        chat = self.chat_id
        await self.stop_monitor()
        await self.stop_afk()
        try:
            if self.client.is_connected():
                await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        client = TelegramClient(
            StringSession(acc["session"]), creds.API_ID, creds.API_HASH
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ValueError("Сессия мертва — залогинь акк заново.")
        self.client = client
        self._new_market()
        me = await client.get_me()
        self.logged_in = True
        self.account_name = (
            f"@{me.username}" if me.username else (me.first_name or str(me.id))
        )
        self.phone = str(acc.get("phone") or "")
        self.active_account_id = int(acc["id"])
        self.db.set_active_account(self.active_account_id)
        # обновим session string
        try:
            self.db.upsert_account(
                phone=self.phone,
                session=StringSession.save(client.session),
                label=self.account_name,
                tg_user_id=int(me.id) if me and me.id else None,
                make_active=True,
            )
        except Exception:  # noqa: BLE001
            pass
        await self._preload_collections()
        if was_running and chat:
            await self.start_monitor(chat)
        else:
            await self.ensure_db_farm()
        return f"🔀 Акк: <b>{self.account_name}</b>"

    async def rotate_account(self) -> str:
        nxt = self.db.next_account_id(self.active_account_id)
        if nxt is None or nxt == self.active_account_id:
            raise ValueError("Нет другого аккаунта для ротации. Добавь через Аккаунты.")
        return await self.switch_account(nxt)

    async def start_add_account(self) -> None:
        """Начать логин доп. акка — текущий не трогаем в БД."""
        self._adding_account = True
        await self.stop_monitor()
        await self.stop_afk()
        try:
            if self.client.is_connected():
                await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.client = self._new_client()
        self._new_market()
        self.phone = None
        self.phone_code_hash = None
        self.logged_in = False
        self.account_name = ""

    async def reset_auth(self) -> None:
        await self.stop_monitor()
        await self.stop_afk()
        # удалить только активный акк из БД, остальные оставить
        if self.active_account_id is not None:
            try:
                self.db.delete_account(self.active_account_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            if self.client.is_connected():
                await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.phone = None
        self.phone_code_hash = None
        self.logged_in = False
        self.account_name = ""
        self.active_account_id = None
        self.client = self._new_client()
        self._new_market()
        wipe_disk_junk()
        # если есть другой акк — поднять его
        other = self.db.get_active_account() or (
            self.db.list_accounts()[0] if self.db.list_accounts() else None
        )
        if other and other.get("session"):
            try:
                await self.switch_account(int(other["id"]))
            except Exception:  # noqa: BLE001
                pass

    def set_range(self, label: str, mn: int, mx: int) -> None:
        """Сложность/цена парсера (монитор)."""
        self.range_label = label
        self.min_stars = float(mn)
        self.max_stars = float(mx)

    def set_filter_range(self, label: str, mn: int, mx: int) -> None:
        """Сложность ТОЛЬКО для фильтр-поиска — парсер не меняется."""
        self.filter_range_label = label
        self.filter_min_stars = float(mn)
        self.filter_max_stars = float(mx)

    async def start_monitor(self, chat_id: int) -> None:
        if not self.logged_in:
            raise RuntimeError("Сначала вход.")
        if self.running:
            await self.stop_monitor()
        await self.pause_db_farm()
        self.chat_id = chat_id
        self.running = True
        # новый обход: лоты-seen сбрасываем; типы/юзы сессии — НЕ трогаем
        # (вечный seen из БД убивает выдачу до 1–2 типов)
        self._seen.clear()
        self._seen_models.clear()
        self._reload_persist_seen(blocklist_only=True)
        self.lots_notified = 0
        self.checks = 0
        self.last_check_lots = 0
        self.last_error = ""
        self._status_msg_id = None
        self.parse_rounds += 1
        self.parse_coll_checks = 0
        self.parse_acc_checks = 0
        self.parse_checked = 0
        self.parse_ready = 0
        self.parse_types = 0
        self.parse_models = 0
        self.market.reshuffle_collections()
        self._task = asyncio.create_task(self._loop(), name="monitor")

    def _is_blocked_lot(self, lot: Lot) -> bool:
        u = (lot.seller or "").lower()
        if u and (u in self._blocked_keys or f"u:{u}" in self._blocked_keys):
            return True
        if lot.seller_id is not None and f"id:{lot.seller_id}" in self._blocked_keys:
            return True
        return self.db.is_blocked(username=lot.seller, user_id=lot.seller_id)

    @staticmethod
    def _is_russian(lot: Lot) -> bool:
        """RU: кириллица в имени/био, флаг, или lang_code ru/uk/be."""
        lc = (getattr(lot, "lang_code", "") or "").lower()
        if lc.startswith(("ru", "uk", "be")):
            return True
        parts = [lot.first_name or "", lot.last_name or "", lot.about or ""]
        blob = " ".join(p for p in parts if p).strip()
        if not blob:
            return False
        if "🇷🇺" in blob or "🇺🇦" in blob or "🇧🇾" in blob:
            return True
        return bool(_CYR_RE.search(blob))

    def block_seller(self, *, username: str = "", user_id: int | None = None) -> bool:
        ok = self.db.block_user(username=username, user_id=user_id, reason="manual")
        if ok:
            u = (username or "").lstrip("@").strip().lower()
            if u:
                self._blocked_keys.add(u)
                self._blocked_keys.add(f"u:{u}")
                self._seen_sellers.add(u)
            if user_id is not None:
                self._blocked_keys.add(f"id:{int(user_id)}")
            self.db.mark_seen_seller(username=username, user_id=user_id)
        return ok

    async def stop_monitor(self) -> str:
        if not self.running and self._task is None:
            return f"{screen('Парсинг')}\nУже стоп"
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_extra_clients()
        await self.ensure_db_farm()
        return f"{screen('Парсинг')}\nСтоп · выдано {self.lots_notified}"

    async def stop_all_jobs(self) -> str:
        """Стоп парсера + фильтров + старого парса."""
        stopped: list[str] = []
        if self.running or self._task is not None:
            self.running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
                self._task = None
            stopped.append("парсер")
        if self.filter_search_running or self._filter_task is not None:
            self.filter_search_running = False
            tsk = self._filter_task
            self._filter_task = None
            if tsk is not None and not tsk.done():
                tsk.cancel()
                try:
                    await tsk
                except (asyncio.CancelledError, Exception):
                    pass
            stopped.append("фильтры")
        if self.old_parse_running or getattr(self, "_old_task", None) is not None:
            self.old_parse_running = False
            tsk = self._old_task
            self._old_task = None
            if tsk is not None and not tsk.done():
                tsk.cancel()
                try:
                    await tsk
                except (asyncio.CancelledError, Exception):
                    pass
            stopped.append("старый парс")
        await self._close_extra_clients()
        await self.ensure_db_farm()
        if not stopped:
            return f"{screen('Стоп')}\nУже стоп"
        return f"{screen('Стоп')}\nостановлено: {', '.join(stopped)}"

    def _in_price(self, lot: Lot) -> bool:
        return self.min_stars <= lot.stars <= self.max_stars

    def _is_ad(self, lot: Lot) -> bool:
        blob = " ".join(x for x in (lot.about, lot.first_name, lot.last_name) if x)
        return bool(blob and _AD_RE.search(blob))

    @staticmethod
    def _bad_username_len(seller: str) -> bool:
        """4 и 5 значные юзы не выдаём."""
        n = len(seller or "")
        return n in (4, 5)

    def _filters_active(self) -> bool:
        f = self.filters
        return bool(
            f.few_gifts
            or f.low_level
            or f.short_username
            or f.long_username
            or f.no_premium
            or f.online_only
            or f.fresh_only
            or f.rare_types
            or f.with_bio
            or f.with_model
            or f.no_digits_user
            or f.strict_free
        )

    def _roll_random_spice(self) -> str:
        """Рандомные мягкие предпочтения (только приоритет, не режем в ноль)."""
        f = self.filters
        f.spice_no_model = False
        f.spice_mid_user = False
        f.spice_has_bio = False
        f.spice_fresh_boost = False
        f.spice_low_stars = False
        if not f.random_mix:
            return ""
        opts = [
            ("model", "spice_no_model"),
            ("mid-юз", "spice_mid_user"),
            ("bio", "spice_has_bio"),
            ("свежие", "spice_fresh_boost"),
            ("дешевле", "spice_low_stars"),
        ]
        random.shuffle(opts)
        picked = opts[: random.randint(1, 2)]
        labels: list[str] = []
        for label, attr in picked:
            setattr(f, attr, True)
            labels.append(label)
        return " · 🎲 " + "+".join(labels) if labels else ""

    def _passes_extra_filters(self, lot: Lot) -> bool:
        """Только явные тумблеры. Рандом-spice — мягкий приоритет, не бан."""
        f = self.filters
        if f.few_gifts:
            if lot.gifts_count is not None and lot.gifts_count > f.max_gifts:
                return False
        if f.low_level:
            # unknown lvl не режем — иначе из 12k БД выдача пустая
            if lot.account_level is not None and lot.account_level > f.max_level:
                return False
        if f.short_username:
            n = len(lot.seller or "")
            if n < 6 or n > f.short_user_max:
                return False
        if f.long_username:
            n = len(lot.seller or "")
            if n < f.long_user_min:
                return False
        if f.no_premium:
            if lot.is_premium is True:
                return False
        if f.online_only:
            if lot.is_online is False:
                return False
        if f.with_bio:
            if not (lot.first_name or lot.last_name or lot.about):
                return False
        if f.with_model:
            if not (lot.model or "").strip():
                return False
        if f.no_digits_user:
            u = lot.seller or ""
            if any(ch.isdigit() for ch in u):
                return False
        if f.strict_free:
            if lot.free_dm is not True:
                return False
        return True

    def _spice_score(self, lot: Lot) -> float:
        """Выше = лучше для рандом-микса (не отсев)."""
        f = self.filters
        score = random.random()
        if f.spice_no_model and (lot.model or "").strip():
            score += 2.0
        if f.spice_mid_user:
            n = len(lot.seller or "")
            if 7 <= n <= 14:
                score += 1.5
        if f.spice_has_bio and (lot.first_name or lot.last_name or lot.about):
            score += 1.2
        if f.spice_low_stars:
            mid = (self.filter_min_stars + self.filter_max_stars) / 2.0
            if lot.stars <= mid:
                score += 1.0
        if f.rare_types:
            score += random.random() * 0.5
        return score

    def _pick_clean(
        self,
        lots: list[Lot],
        *,
        limit: int | None = None,
        apply_extra: bool = False,
        track_seen: bool = True,
        channel: str = "parser",
        require_russian: bool | None = None,
        require_free_dm: bool = True,
        ignore_seen: bool = False,
        strict_free_dm: bool = False,
    ) -> list[Lot]:
        """Разнообразие NFT. channel=parser|filter|old — разные режимы."""
        is_filter = channel == "filter"
        is_old = channel == "old"
        want_ru = (
            self.require_russian if require_russian is None else bool(require_russian)
        )
        # типы: каждый канал — свой seen (фильтры ≠ парсер)
        seen_titles: set[str] = set()
        if channel == "parser":
            seen_titles = set(self._seen_titles) | set(self._recent_titles[-200:])
        elif is_filter:
            seen_titles = set(self._filter_recent_titles[-300:])
        if is_old or ignore_seen:
            seen_sellers: set[str] = set()
            seen_models: set[str] = set()
            recent_list: list[str] = []
        elif is_filter:
            seen_sellers = set(self._filter_seen_sellers)
            seen_models = set(self._filter_seen_models)
            recent_list = self._filter_recent_titles
        else:
            seen_sellers = set(self._seen_sellers)
            seen_models = self._seen_models
            recent_list = self._recent_titles
        always_block_sellers = (
            set(self._filter_seen_sellers) if is_filter else set(self._seen_sellers)
        )
        # выданные юзы — никогда снова (парсер и фильтры)
        always_block_sellers |= set(self._delivered_sellers)

        lots = list(lots)
        random.shuffle(lots)
        buckets: dict[str, list[Lot]] = {}
        keys: list[str] = []
        local_sellers: set[str] = set()
        local_models: set[str] = set()
        local_titles: set[str] = set()
        show_counts = (
            {}
            if (is_old or ignore_seen)
            else self.db.get_collection_show_counts()
        )
        recent = set(recent_list[-100:]) | seen_titles

        for lot in lots:
            if not lot.seller:
                continue
            if self._bad_username_len(lot.seller):
                continue
            if self._is_blocked_lot(lot):
                continue
            if self._is_ad(lot):
                continue
            if want_ru and not self._is_russian(lot):
                continue
            # платные Stars — мимо; unknown ок
            if require_free_dm and lot.free_dm is False:
                continue
            if is_filter and self._filters_active() and not self._passes_extra_filters(lot):
                continue
            # владельцы без повторов
            if lot.owner_key in always_block_sellers:
                continue
            if (not ignore_seen) and lot.owner_key in seen_sellers:
                continue
            if lot.owner_key in local_sellers:
                continue
            tk = self._title_key(lot)
            if not tk:
                continue
            # типы без повторов (парсер + фильтры)
            if channel in ("parser", "filter"):
                if tk in seen_titles or tk in local_titles:
                    continue
            # модели: только внутри выдачи (не режем историю БД — иначе 1–2 типа)
            if lot.model_key in local_models:
                continue
            if tk not in buckets:
                buckets[tk] = []
                keys.append(tk)
            buckets[tk].append(lot)
            local_sellers.add(lot.owner_key)
            local_models.add(lot.model_key)
            local_titles.add(tk)

        for tk in keys:
            random.shuffle(buckets[tk])

        def _rank(tk: str) -> tuple:
            return (
                show_counts.get(tk, 0),
                1 if tk in recent else 0,
                random.random(),
            )

        def _take(tk: str) -> Lot | None:
            bucket = buckets.get(tk) or []
            while bucket:
                lot = bucket.pop(0)
                if lot.owner_key in always_block_sellers:
                    continue
                if lot.owner_key in marked_sellers:
                    continue
                if (not ignore_seen) and lot.owner_key in seen_sellers:
                    continue
                if lot.model_key in marked_models:
                    continue
                if channel in ("parser", "filter"):
                    if self._title_key(lot) in marked_titles:
                        continue
                if require_free_dm and lot.free_dm is False:
                    continue
                return lot
            return None

        marked_sellers: set[str] = set()
        marked_models: set[str] = set()
        marked_titles: set[str] = set()

        def _mark(lot: Lot) -> None:
            marked_sellers.add(lot.owner_key)
            marked_models.add(lot.model_key)
            tk = self._title_key(lot)
            if tk:
                marked_titles.add(tk)

        primary: list[Lot] = []
        # парсер/фильтры: рандомный разброс типов (+ spice приоритет)
        if channel in ("parser", "filter"):
            ordered = list(keys)
            if channel == "filter" and (
                self.filters.random_mix
                or self.filters.rare_types
                or self.filters.spice_no_model
                or self.filters.spice_mid_user
                or self.filters.spice_has_bio
                or self.filters.spice_low_stars
            ):
                ordered.sort(
                    key=lambda tk: -max(
                        (self._spice_score(x) for x in (buckets.get(tk) or [])),
                        default=0.0,
                    )
                )
            else:
                random.shuffle(ordered)
        else:
            ordered = sorted(keys, key=_rank)
        per_type = max(1, int(getattr(creds, "PER_TYPE", 1)))
        max_types = int(getattr(creds, "MAX_TYPES", 0) or 0)
        # фильтры и парсер — по 1 с типа, цель ~30
        by_types = channel in ("parser", "filter") and (
            limit is None or limit <= 0 or channel == "filter"
        )
        take_all = channel == "old"

        if by_types:
            target_types = (
                max(30, int(creds.SHOW_LIMIT))
                if channel == "filter"
                else (max_types if max_types > 0 else 10_000)
            )
            if channel == "filter" and limit is not None and limit > 0:
                target_types = int(limit)
            types_taken = 0
            for tk in ordered:
                if types_taken >= target_types:
                    break
                if tk in marked_titles:
                    continue
                lot = _take(tk)
                if lot is None:
                    continue
                primary.append(lot)
                _mark(lot)
                types_taken += 1
            result = list(primary)
            random.shuffle(result)
        elif take_all:
            for tk in ordered:
                while True:
                    lot = _take(tk)
                    if lot is None:
                        break
                    primary.append(lot)
                    _mark(lot)
                    if limit is not None and limit > 0 and len(primary) >= limit:
                        break
                if limit is not None and limit > 0 and len(primary) >= limit:
                    break
            result = list(primary)
            random.shuffle(result)
        else:
            target = limit if limit is not None else creds.SHOW_LIMIT
            for tk in ordered:
                if len(primary) >= target:
                    break
                lot = _take(tk)
                if lot is None:
                    continue
                primary.append(lot)
                _mark(lot)

            extra: list[Lot] = []
            if len(primary) < target:
                again = list(keys)
                random.shuffle(again)
                for tk in again:
                    if len(primary) + len(extra) >= target:
                        break
                    lot = _take(tk)
                    if lot is None:
                        continue
                    extra.append(lot)
                    _mark(lot)

            result = list(primary) + list(extra)
            result = result[:target]
            random.shuffle(result)

        # разброс: соседние в списке — разные типы
        spread: list[Lot] = []
        rest = list(result)
        while rest:
            pick_i = 0
            if spread:
                last = self._title_key(spread[-1])
                cands = [
                    i
                    for i, lot in enumerate(rest)
                    if self._title_key(lot) != last
                ]
                if cands:
                    pick_i = random.choice(cands)
                else:
                    pick_i = random.randrange(len(rest))
            else:
                pick_i = random.randrange(len(rest))
            spread.append(rest.pop(pick_i))
        result = spread

        if not ignore_seen and track_seen and result:
            self._mark_delivered(result, channel=channel)
        return result

    def _pick_with_fallback(
        self,
        lots: list[Lot],
        *,
        limit: int | None,
        apply_extra: bool,
        track_seen: bool,
        channel: str,
        strict_russian: bool | None = None,
    ) -> list[Lot]:
        """Сначала строго; фильтры — основа: ~30 уникальных типов."""
        target = max(30, int(creds.SHOW_LIMIT))
        if channel not in ("parser", "filter"):
            target = limit or target
        elif limit is not None and limit > 0:
            target = int(limit)
        keep_ru = (
            bool(self.require_russian)
            if strict_russian is None
            else bool(strict_russian)
        )
        if channel == "parser":
            attempts = [
                (True, True, False, False),
                (True, True, True, False),
            ]
        elif channel == "old":
            attempts = [
                (True, True, True, False),
                (True, False, True, False),
            ]
        else:
            # фильтры: free DM всегда; RU сначала; если мало — без RU
            keep_extra = bool(apply_extra or self._filters_active())
            attempts = [
                (True, True, False, keep_extra),
                (True, True, True, keep_extra),
                (False, True, True, keep_extra),
            ]
        best: list[Lot] = []
        for want_ru, want_free, ign_seen, extra in attempts:
            out = self._pick_clean(
                lots,
                limit=target if channel == "filter" else (None if channel == "parser" else limit),
                apply_extra=extra,
                track_seen=False,
                channel=channel,
                require_russian=want_ru,
                require_free_dm=want_free,
                ignore_seen=ign_seen,
                strict_free_dm=False,
            )
            if channel == "parser" and out:
                out = out[:target]
            if len(out) > len(best):
                best = out
            stop_at = target if channel == "filter" else min(20, target)
            if len(best) >= stop_at:
                break
        if best:
            best = best[:target]
        if best and track_seen and channel in ("parser", "filter"):
            self._mark_delivered(best, channel=channel)
        return best

    async def _prepare_show(
        self,
        lots: list[Lot],
        *,
        limit: int | None = None,
        apply_extra: bool = False,
        track_seen: bool = True,
        need_full: bool | None = None,
        channel: str = "parser",
        strict_russian: bool | None = None,
    ) -> list[Lot]:
        by_types = channel == "parser" and (limit is None or limit <= 0)
        take_all = channel == "old"
        if by_types:
            lim = 10_000
        elif take_all:
            lim = int(limit) if (limit is not None and limit > 0) else 30
        else:
            lim = limit or max(30, creds.SHOW_LIMIT)
        self._ingest_always(list(lots))
        pre = list(lots)
        random.shuffle(pre)
        with_seller = [lot for lot in pre if lot.seller]
        without = [lot for lot in pre if not lot.seller]
        # меньше resolve — больше упор на уже известных продавцов
        resolve_n = min(
            len(without),
            50
            if by_types
            else (
                120
                if take_all
                else (max(lim * 8, 200) if channel == "filter" else max(lim * 2, 40))
            ),
        )
        if resolve_n:
            await self.market.resolve_owners(
                without[:resolve_n],
                timeout=creds.OWNER_TIMEOUT,
                parallel=getattr(creds, "ENRICH_PARALLEL", 8),
            )
            self.parse_acc_checks += resolve_n
            self._ingest_always(without[:resolve_n])
        pool = with_seller + without[:resolve_n]
        self.db.upsert_users_from_lots(
            [lot for lot in pool if lot.seller_id is not None or lot.seller],
            cap=creds.AFK_USER_CAP,
        )
        self._save_models([lot for lot in pool if lot.seller or lot.seller_id])
        self._flush_market_users()

        candidates = [
            lot
            for lot in pool
            if lot.seller and not self._bad_username_len(lot.seller)
            and not self._is_delivered_seller(lot)
        ]
        # без уже выданных юзов/типов — каналы раздельно
        if channel == "parser":
            blocked_t = set(self._seen_titles) | set(self._recent_titles[-200:])
            candidates = [
                lot
                for lot in candidates
                if lot.owner_key not in self._seen_sellers
                and lot.owner_key not in self._delivered_sellers
                and self._title_key(lot) not in blocked_t
            ]
        elif channel == "filter":
            blocked_t = set(self._filter_recent_titles[-300:])
            candidates = [
                lot
                for lot in candidates
                if lot.owner_key not in self._filter_seen_sellers
                and lot.owner_key not in self._delivered_sellers
                and self._title_key(lot) not in blocked_t
            ]

        want_ru = (
            bool(self.require_russian)
            if strict_russian is None
            else bool(strict_russian)
        )
        if channel in ("parser", "filter"):
            want_ru = True

        if channel == "filter" and candidates:
            # === ОСНОВА: из большой БД выжать ~30+ разных типов ===
            mn_f, mx_f = self.filter_min_stars, self.filter_max_stars
            candidates = [
                lot for lot in candidates if mn_f <= lot.stars <= mx_f
            ]
            if self._filters_active():
                pre: list[Lot] = []
                for lot in candidates:
                    if self.filters.short_username:
                        n = len(lot.seller or "")
                        if n < 6 or n > self.filters.short_user_max:
                            continue
                    if self.filters.long_username:
                        if len(lot.seller or "") < self.filters.long_user_min:
                            continue
                    if self.filters.no_premium and lot.is_premium is True:
                        continue
                    if self.filters.with_model and not (lot.model or "").strip():
                        continue
                    if self.filters.no_digits_user and any(
                        ch.isdigit() for ch in (lot.seller or "")
                    ):
                        continue
                    pre.append(lot)
                candidates = pre or candidates
            # сортируем по spice — сначала «вкусные», но всех оставляем
            if self.filters.random_mix or self.filters.rare_types:
                candidates = sorted(
                    candidates, key=self._spice_score, reverse=True
                )
            already_ru = [lot for lot in candidates if self._is_russian(lot)]
            need_bio = [lot for lot in candidates if not self._is_russian(lot)]
            # из 12k БД берём МНОГО разных типов на проверку
            sample = self._sample_diverse_titles(already_ru, 250)
            if len({self._title_key(x) for x in sample}) < 80:
                sample = _dedupe_lots(
                    sample + self._sample_diverse_titles(need_bio, 350)
                )
            if len({self._title_key(x) for x in sample}) < 60:
                sample = _dedupe_lots(
                    sample + self._sample_diverse_titles(candidates, 400)
                )
            need_lvl = bool(self.filters.low_level or self.filters.few_gifts)
            # enrich всем без био / без lvl — иначе RU/фильтры режут в ноль
            need_enr = [
                lot
                for lot in sample
                if not (lot.first_name or lot.last_name or lot.about)
                or (
                    need_lvl
                    and (
                        lot.account_level is None
                        or (
                            self.filters.few_gifts
                            and lot.gifts_count is None
                        )
                    )
                )
                or (self.filters.no_premium and lot.is_premium is None)
            ]
            if need_enr:
                await self.market.enrich_profiles(
                    need_enr,
                    timeout=min(max(creds.OWNER_TIMEOUT, 0.7), 1.0),
                    parallel=max(8, int(getattr(creds, "ENRICH_PARALLEL", 8))),
                )
                self.parse_acc_checks += len(need_enr)
            await self.market.check_free_dm(
                sample,
                timeout=max(creds.OWNER_TIMEOUT, 1.0),
            )
            self.parse_acc_checks += len(sample)
            if self.filters.online_only:
                await self.market.refresh_online(
                    sample,
                    timeout=max(creds.OWNER_TIMEOUT, 1.0),
                )
                self.parse_acc_checks += len(sample)
            self._ingest_always(sample)
            candidates = [lot for lot in sample if lot.free_dm is not False]
            if self._filters_active():
                filtered = [
                    lot
                    for lot in candidates
                    if self._passes_extra_filters(lot)
                ]
                # не обнуляем пул тумблерами, если совсем пусто
                if filtered:
                    candidates = filtered
            ru_titles = {
                self._title_key(x)
                for x in candidates
                if self._is_russian(x) and self._title_key(x)
            }
            if len(ru_titles) < 35:
                used = {lot.id for lot in sample}
                used_t = {self._title_key(x) for x in sample}
                left = [
                    lot
                    for lot in need_bio
                    if lot.id not in used and self._title_key(lot) not in used_t
                ]
                wave2 = self._sample_diverse_titles(left, 300)
                if wave2:
                    await self.market.enrich_profiles(
                        wave2,
                        timeout=min(max(creds.OWNER_TIMEOUT, 0.7), 1.0),
                        parallel=max(
                            8, int(getattr(creds, "ENRICH_PARALLEL", 8))
                        ),
                    )
                    await self.market.check_free_dm(
                        wave2,
                        timeout=max(creds.OWNER_TIMEOUT, 1.0),
                    )
                    self.parse_acc_checks += len(wave2) * 2
                    if self.filters.online_only:
                        await self.market.refresh_online(
                            wave2,
                            timeout=max(creds.OWNER_TIMEOUT, 1.0),
                        )
                        self.parse_acc_checks += len(wave2)
                    self._ingest_always(wave2)
                    add = [
                        lot
                        for lot in wave2
                        if lot.free_dm is not False
                        and (
                            not self._filters_active()
                            or self._passes_extra_filters(lot)
                        )
                    ]
                    candidates = _dedupe_lots(candidates + add)
            # ещё волна если всё ещё мало типов
            if len({self._title_key(x) for x in candidates}) < 30:
                used = {lot.id for lot in candidates}
                used_t = {self._title_key(x) for x in candidates}
                left = [
                    lot
                    for lot in (already_ru + need_bio)
                    if lot.id not in used and self._title_key(lot) not in used_t
                ]
                wave3 = self._sample_diverse_titles(left, 250)
                if wave3:
                    need_e = [
                        lot
                        for lot in wave3
                        if not (lot.first_name or lot.last_name or lot.about)
                    ]
                    if need_e:
                        await self.market.enrich_profiles(
                            need_e,
                            timeout=min(max(creds.OWNER_TIMEOUT, 0.7), 1.0),
                            parallel=max(
                                8, int(getattr(creds, "ENRICH_PARALLEL", 8))
                            ),
                        )
                    await self.market.check_free_dm(
                        wave3,
                        timeout=max(creds.OWNER_TIMEOUT, 1.0),
                    )
                    self.parse_acc_checks += len(wave3)
                    self._ingest_always(wave3)
                    candidates = _dedupe_lots(
                        candidates
                        + [
                            lot
                            for lot in wave3
                            if lot.free_dm is not False
                            and (
                                not self._filters_active()
                                or self._passes_extra_filters(lot)
                            )
                        ]
                    )
        elif channel == "parser" and candidates:
            already_ru = [lot for lot in candidates if self._is_russian(lot)]
            need_bio = [lot for lot in candidates if not self._is_russian(lot)]
            # ~100 разных типов уже-RU + добор
            check_ru = self._sample_diverse_titles(already_ru, 100)
            if check_ru:
                await self.market.check_free_dm(
                    check_ru,
                    timeout=max(creds.OWNER_TIMEOUT, 1.0),
                )
                self.parse_acc_checks += len(check_ru)
            ready = [
                lot
                for lot in check_ru
                if lot.free_dm is not False and self._is_russian(lot)
            ]
            titles_ready = {
                self._title_key(lot) for lot in ready if self._title_key(lot)
            }
            if len(titles_ready) < 35:
                wave = self._sample_diverse_titles(
                    [
                        lot
                        for lot in need_bio
                        if self._title_key(lot) not in titles_ready
                    ],
                    120,
                )
                if wave:
                    await self.market.enrich_profiles(
                        wave,
                        timeout=min(max(creds.OWNER_TIMEOUT, 0.7), 1.0),
                        parallel=getattr(creds, "ENRICH_PARALLEL", 8),
                    )
                    await self.market.check_free_dm(
                        wave,
                        timeout=max(creds.OWNER_TIMEOUT, 1.0),
                    )
                    self.parse_acc_checks += len(wave)
                    self._ingest_always(wave)
                    for lot in wave:
                        if lot.free_dm is False:
                            continue
                        if self._is_russian(lot):
                            ready.append(lot)
            candidates = _dedupe_lots(
                [lot for lot in ready if lot.free_dm is not False]
            )
            self._ingest_always(candidates)
        elif candidates:
            sample_n = min(
                len(candidates),
                250 if take_all else max(lim * 4, 80),
            )
            sample = self._sample_diverse_titles(candidates, sample_n)
            await self.market.enrich_profiles(
                sample,
                timeout=min(max(creds.OWNER_TIMEOUT, 0.8), 1.1),
                parallel=getattr(creds, "ENRICH_PARALLEL", 8),
            )
            await self.market.check_free_dm(
                sample,
                timeout=max(creds.OWNER_TIMEOUT, 1.1),
            )
            self.parse_acc_checks += len(sample) * 2
            self._ingest_always(sample)
            candidates = [lot for lot in sample if lot.free_dm is not False]

        return self._pick_with_fallback(
            candidates,
            limit=None if by_types else lim,
            apply_extra=bool(apply_extra and channel == "filter"),
            track_seen=bool(track_seen and channel in ("parser", "filter")),
            channel=channel,
            strict_russian=True if channel in ("parser", "filter") else want_ru,
        )

    async def run_filter_search(self, chat_id: int) -> None:
        """Фильтр-поиск: большая БД → ~30 уникальных, free DM."""
        if self.filter_search_running:
            await self._say_to(chat_id, f"{screen('Фильтры')}\nуже идёт")
            return
        self.filter_search_running = True
        await self.pause_db_farm()
        f = self.filters
        mn, mx = self.filter_min_stars, self.filter_max_stars
        label = self.filter_range_label
        spice_note = self._roll_random_spice()
        target_n = max(30, int(creds.SHOW_LIMIT))
        # каждый поиск — новые типы; выданные юзы навсегда (из БД)
        self._filter_recent_titles.clear()
        self._filter_seen_models.clear()
        self._delivered_sellers |= self._load_delivered_sellers()
        self._filter_seen_sellers = set(self._delivered_sellers)
        self._seen_sellers |= set(self._delivered_sellers)
        try:
            db_n = 0
            try:
                db_n = self.db.count_in_range(min_stars=mn, max_stars=mx)
            except Exception:  # noqa: BLE001
                db_n = self.db.count()
            notes = []
            if f.online_only:
                notes.append("🟢 в сети")
            if f.fresh_only:
                notes.append("🆕 48ч")
            if f.rare_types:
                notes.append("💎 редкие")
            if f.with_bio:
                notes.append("био")
            if f.with_model:
                notes.append("модель")
            if f.no_digits_user:
                notes.append("без цифр")
            if f.strict_free:
                notes.append("free✓")
            extra = (" · " + " · ".join(notes)) if notes else ""
            total_db = self.db.count()
            await self._say_to(
                chat_id,
                f"{screen('Фильтры')}\n{label}{extra}{spice_note}\n"
                f"БД всего: <b>{total_db}</b> · в диапазоне: <b>{db_n}</b>",
            )

            def _db_pool() -> list[Lot]:
                hours = 48.0 if f.fresh_only else (
                    96.0 if f.spice_fresh_boost else 0.0
                )
                # почти вся БД в диапазоне — не 2–3 тысячи
                lim = min(max(db_n, 500), 12000)
                lim = max(lim, int(getattr(creds, "FILTER_DB_LIMIT", 800)) * 10)
                lim = min(lim, 12000)
                try:
                    return self.db.fetch_for_filters(
                        min_stars=mn,
                        max_stars=mx,
                        limit=lim,
                        hours=hours if hours > 0 else 168.0,
                        require_seller=True,
                        prefer_rare=bool(f.rare_types or f.random_mix),
                        exclude_sellers=set(self._delivered_sellers),
                        exclude_titles=set(),
                    )
                except Exception:  # noqa: BLE001
                    return self.db.fetch_random_lots(
                        min_stars=mn,
                        max_stars=mx,
                        limit=lim,
                        require_seller=True,
                    )

            async def _live_pool() -> list[Lot]:
                try:
                    burst = await self.multi_burst(
                        mn,
                        mx,
                        progress_cb=None,
                        early_show_at=0,
                    )
                    if not burst:
                        return []
                    raw = list(burst.all_lots or burst.lots or [])
                    if raw:
                        self._save_models(raw)
                    self._flush_market_users()
                    return list(burst.lots or raw)
                except Exception as exc:  # noqa: BLE001
                    await self._say_to(
                        chat_id,
                        f"{screen('Фильтры')}\n⚠️ live {_esc(str(exc)[:100])}",
                    )
                    return []

            def _merge_unique(
                base: list[Lot], more: list[Lot], *, cap: int
            ) -> list[Lot]:
                have_t = {self._title_key(x) for x in base}
                have_o = {x.owner_key for x in base}
                out = list(base)
                for lot in more:
                    tk = self._title_key(lot)
                    if tk in have_t or lot.owner_key in have_o:
                        continue
                    if lot.free_dm is False:
                        continue
                    out.append(lot)
                    have_t.add(tk)
                    have_o.add(lot.owner_key)
                    if len(out) >= cap:
                        break
                return out

            # 1) сначала ТОЛЬКО БД (быстро и много при 12k)
            old = _db_pool()
            random.shuffle(old)
            self._ingest_always(old)
            await self._say_to(
                chat_id,
                f"{screen('Фильтры')}\nпул БД: <b>{len(old)}</b> лотов",
            )
            shown = await self._prepare_show(
                old,
                limit=target_n,
                apply_extra=True,
                track_seen=True,
                need_full=True,
                channel="filter",
            )

            # 2) мало — live + ещё рандом из БД
            if len(shown) < target_n:
                live = await _live_pool()
                old2 = _db_pool()
                merged = _dedupe_lots(old + old2 + live)
                random.shuffle(merged)
                self._ingest_always(merged)
                await self._say_to(
                    chat_id,
                    f"{screen('Фильтры')}\nдобор · БД {len(old2)} + live {len(live)}",
                )
                more = await self._prepare_show(
                    merged,
                    limit=target_n,
                    apply_extra=True,
                    track_seen=True,
                    need_full=True,
                    channel="filter",
                )
                shown = _merge_unique(shown, more, cap=target_n)

            # 3) всё ещё мало — без spice, ещё раз по всей БД
            if len(shown) < 20:
                f.spice_no_model = False
                f.spice_mid_user = False
                f.spice_has_bio = False
                f.spice_low_stars = False
                self._filter_seen_sellers = set(self._delivered_sellers)
                self._filter_seen_models.clear()
                old3 = _db_pool()
                more = await self._prepare_show(
                    old3,
                    limit=target_n,
                    apply_extra=True,
                    track_seen=True,
                    need_full=True,
                    channel="filter",
                )
                shown = _merge_unique(shown, more, cap=target_n)

            if not shown:
                await self._say_to(
                    chat_id,
                    f"{screen('Фильтры')}\nпусто · БД {db_n} · "
                    f"сними онлайн / смени сложность",
                    reply_markup=filters_inline(),
                )
                return
            shown = shown[:target_n]
            await self._say_lot_list_to(chat_id, shown, channel="filter")
            await self._say_to(
                chat_id,
                f"{screen('Фильтры')}\nготово · <b>{len(shown)}</b> / {target_n} · "
                f"из БД {db_n}",
                reply_markup=filters_inline(),
            )
        finally:
            self.filter_search_running = False
            self._filter_task = None
            f.spice_no_model = False
            f.spice_mid_user = False
            f.spice_has_bio = False
            f.spice_fresh_boost = False
            f.spice_low_stars = False
            await self._close_extra_clients()
            await self.ensure_db_farm()

    async def run_old_parse(self, chat_id: int, *, hours: float = 24.0) -> None:
        """Все лоты из БД за 24ч в выбранном ценовом диапазоне."""
        if self.old_parse_running:
            await self._say_to(chat_id, f"{screen('Старый парс')}\nуже идёт")
            return
        self.old_parse_running = True
        await self.pause_db_farm()
        mn, mx = self.min_stars, self.max_stars
        label = self.range_label
        try:
            await self._say_to(
                chat_id,
                f"{screen('Старый парс')}\n{label} · за {int(hours)}ч",
            )
            lots = self.db.fetch_lots_last_hours(
                min_stars=mn,
                max_stars=mx,
                hours=hours,
                require_seller=True,
                limit=0,
            )
            self.parse_checked = len(lots)
            self._ingest_always(lots)
            await self._say_to(
                chat_id,
                f"{screen('Старый парс')}\n"
                f"В БД за {int(hours)}ч: <b>{len(lots)}</b>",
            )
            if not lots:
                await self._say_to(
                    chat_id,
                    f"{screen('Старый парс')}\nпусто · нет лотов за 24ч",
                    reply_markup=main_inline(),
                )
                return
            shown = await self._prepare_show(
                lots,
                limit=30,
                apply_extra=False,
                track_seen=False,
                need_full=False,
                channel="old",
            )
            self.parse_ready = len(shown)
            if not shown:
                await self._say_to(
                    chat_id,
                    f"{screen('Старый парс')}\n"
                    f"нашёл {len(lots)}, после фильтров 0",
                    reply_markup=main_inline(),
                )
                return
            await self._say_lot_list_to(chat_id, shown, channel="old")
            await self._say_to(
                chat_id,
                f"{screen('Старый парс')}\n"
                f"выдал <b>{len(shown)}</b> / {len(lots)}\n"
                f"проверок акка: {self.parse_acc_checks}",
                reply_markup=main_inline(),
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            await self._say_to(
                chat_id,
                f"{screen('Старый парс')}\n⚠️ {_esc(str(exc)[:180])}",
                reply_markup=main_inline(),
            )
        finally:
            self.old_parse_running = False
            self._old_task = None
            await self.ensure_db_farm()

    @staticmethod
    def _format_lot_line(lot: Lot) -> str:
        # Название NFT/коллекции (Plush Pepe), не модель (Glow Verde)
        nft_name = (lot.title or lot.model or "Gift").strip()
        meta = []
        if lot.gifts_count is not None:
            meta.append(f"gifts {lot.gifts_count}")
        if lot.account_level is not None:
            meta.append(f"lvl {lot.account_level}")
        if lot.is_premium is False:
            meta.append("no TGP")
        elif lot.is_premium is True:
            meta.append("TGP")
        extra = f" · {', '.join(meta)}" if meta else ""
        return (
            f'🎁 <a href="{lot.nft_url}">{_esc(nft_name)}</a> | '
            f"@{lot.seller} | {_fmt(lot.stars)}⭐{extra}"
        )

    async def _say_lot_list(
        self, lots: list[Lot], *, channel: str = "parser"
    ) -> None:
        if self.chat_id:
            await self._say_lot_list_to(self.chat_id, lots, channel=channel)

    async def _say_lot_list_to(
        self, chat_id: int, lots: list[Lot], *, channel: str = "parser"
    ) -> None:
        """Выдача СПИСКОМ. channel=parser|filter — раздельный учёт."""
        batch = (
            lots
            if channel in ("parser", "old")
            else lots[: creds.SHOW_LIMIT]
        )
        if not batch:
            return
        # стереть юзов из БД после выдачи (если ещё не)
        todo = [
            lot for lot in batch if lot.owner_key not in self._delivered_sellers
        ]
        if todo:
            try:
                self._mark_delivered(todo, channel=channel)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mark on say: %s", exc)
        lines = [self._format_lot_line(lot) for lot in batch]
        for i in range(0, len(lines), 10):
            await self._say_to(chat_id, "\n".join(lines[i : i + 10]))
        if channel == "parser":
            try:
                self.db.bump_daily(lots_shown=len(batch))
            except Exception:  # noqa: BLE001
                pass

    async def _say(self, text: str, reply_markup=None) -> Message | None:
        if not self.chat_id:
            return None
        return await self._say_to(self.chat_id, text, reply_markup=reply_markup)

    async def _say_to(
        self, chat_id: int, text: str, reply_markup=None
    ) -> Message | None:
        if not self.bot or not chat_id:
            return None
        try:
            return await self.bot.send_message(
                chat_id, text, reply_markup=reply_markup
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("say: %s", exc)
            return None

    async def _edit_status(self, text: str) -> None:
        if not self.bot or not self.chat_id:
            return
        try:
            if self._status_msg_id:
                await self.bot.edit_message_text(
                    text,
                    chat_id=self.chat_id,
                    message_id=self._status_msg_id,
                    reply_markup=main_inline(),
                )
                return
        except Exception:  # noqa: BLE001
            self._status_msg_id = None
        msg = await self._say(text, reply_markup=main_inline())
        if msg:
            self._status_msg_id = msg.message_id

    def _save_models(self, lots: list[Lot]) -> tuple[int, int]:
        """Сохраняет модели в БД (всегда, без условий выдачи)."""
        if not lots:
            return 0, 0
        inserted, updated = self.db.upsert_models(lots)
        self.db_last_saved = inserted + updated
        self.db_total = self.db.count()
        if inserted:
            try:
                self.db.bump_daily(lots_new=inserted)
            except Exception:  # noqa: BLE001
                pass
        return inserted, updated

    def _flush_market_users(self) -> tuple[int, int, int]:
        """Слить юзов со ВСЕХ Telethon-рынков в БД."""
        found: list[dict] = []
        try:
            found.extend(self.market.drain_users() or [])
        except Exception:  # noqa: BLE001
            pass
        for m in list(self._extra_markets):
            try:
                found.extend(m.drain_users() or [])
            except Exception:  # noqa: BLE001
                pass
        if not found:
            return 0, 0, self.db.count_users()
        # не возвращаем в БД уже выданных
        clean: list[dict] = []
        for u in found:
            uid = u.get("user_id")
            uname = str(u.get("username") or "").lstrip("@").strip().lower()
            if uname and uname in self._delivered_sellers:
                continue
            if uid is not None and f"id:{int(uid)}" in self._delivered_sellers:
                continue
            if self.db.is_seen_seller(username=uname, user_id=uid):
                if uname:
                    self._delivered_sellers.add(uname)
                if uid is not None:
                    self._delivered_sellers.add(f"id:{int(uid)}")
                continue
            clean.append(u)
        if not clean:
            return 0, 0, self.db.count_users()
        ins, upd, total = self.db.upsert_users(clean, cap=creds.AFK_USER_CAP)
        if ins:
            try:
                self.db.bump_daily(users_new=ins)
            except Exception:  # noqa: BLE001
                pass
        return ins, upd, total

    def _ingest_always(self, lots: list[Lot] | None = None) -> None:
        """Копить gifts+users в БД — кроме уже выданных юзов."""
        batch = [
            lot
            for lot in (lots or [])
            if not self._is_delivered_seller(lot)
        ]
        if batch:
            try:
                self._save_models(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ingest gifts: %s", exc)
            try:
                self.db.upsert_users_from_lots(
                    [
                        lot
                        for lot in batch
                        if lot.seller_id is not None or lot.seller
                    ],
                    cap=creds.AFK_USER_CAP,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ingest users from lots: %s", exc)
        try:
            self._flush_market_users()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ingest drain users: %s", exc)
        try:
            self.db.checkpoint()
        except Exception:  # noqa: BLE001
            pass

    async def pause_db_farm(self) -> None:
        """Мягкая пауза — задачу НЕ убиваем, БД продолжает копиться после."""
        self._afk_paused = True

    async def ensure_db_farm(self) -> None:
        """Тихий непрерывный скан юзов+моделей в БД (без кнопки AFK)."""
        if not self.logged_in:
            return
        if self.running or self.filter_search_running or self.old_parse_running:
            return
        if self.afk_running and self._afk_task and not self._afk_task.done():
            self._afk_paused = False
            return
        self._afk_quiet = True
        self._afk_paused = False
        self.afk_running = True
        self.afk_last_error = ""
        self._afk_task = asyncio.create_task(self._afk_loop(), name="db-farm")
        logger.info(
            "DB farm start · users=%s models=%s",
            self.db.count_users(),
            self.db.count(),
        )

    async def start_afk(self, chat_id: int) -> str:
        if not self.logged_in:
            raise RuntimeError("Сначала вход.")
        self.chat_id = chat_id
        self._afk_quiet = True
        await self.ensure_db_farm()
        return (
            f"💾 БД фарм · юзов <b>{self.db.count_users():,}</b> · "
            f"моделей <b>{self.db.count():,}</b>"
        )

    async def stop_afk(self) -> str:
        self._afk_paused = True
        if not self.afk_running and self._afk_task is None:
            return "💾 БД фарм уже стоп."
        self.afk_running = False
        if self._afk_task:
            self._afk_task.cancel()
            try:
                await self._afk_task
            except asyncio.CancelledError:
                pass
            self._afk_task = None
        return (
            f"💾 БД фарм стоп.\n"
            f"Юзов: <b>{self.db.count_users():,}</b>\n"
            f"Моделей: <b>{self.db.count():,}</b>\n"
            f"Страниц: <b>{self.afk_pages}</b> · +юзов: "
            f"<b>{self.afk_users_added}</b> · +моделей: "
            f"<b>{self.afk_models_added}</b>"
        )

    async def _edit_afk(self, text: str) -> None:
        if self._afk_quiet or not self.bot or not self.chat_id:
            return
        try:
            if self._afk_status_msg_id:
                await self.bot.edit_message_text(
                    text,
                    chat_id=self.chat_id,
                    message_id=self._afk_status_msg_id,
                    reply_markup=main_inline(),
                )
                return
        except Exception:  # noqa: BLE001
            self._afk_status_msg_id = None
        msg = await self._say(text, reply_markup=main_inline())
        if msg:
            self._afk_status_msg_id = msg.message_id

    async def _afk_loop(self) -> None:
        """Непрерывный фарм коллекций → юзы+модели в БД без остановки."""
        try:
            gift_ids = await self.market.load_collections()
        except Exception as exc:  # noqa: BLE001
            self.afk_last_error = str(exc)
            logger.warning("DB farm collections: %s", exc)
            if not self._afk_quiet:
                await self._say(f"💾 БД фарм ошибка: {_esc(str(exc)[:180])}")
            self.afk_running = False
            return

        self.market.reshuffle_collections()
        gift_ids = list(self.market._gift_ids or gift_ids)
        self.afk_collections_total = len(gift_ids)
        self.afk_cursor = random.randrange(len(gift_ids)) if gift_ids else 0
        for gid in gift_ids:
            self.db.touch_collection(
                gid, title="", last_offset=self.db.get_collection_offset(gid)
            )

        if not self._afk_quiet:
            await self._say(
                f"💾 БД фарм: коллекций <b>{len(gift_ids)}</b> · "
                f"сейчас юзов <b>{self.db.count_users():,}</b>"
            )
        else:
            logger.info("DB farm quiet · collections=%s", len(gift_ids))

        last_status = 0.0
        n = len(gift_ids)
        if n == 0:
            self.afk_running = False
            return

        while self.afk_running and not self._afk_paused:
            # во время активного парсинга — ждём
            if self.running or self.filter_search_running or self.old_parse_running:
                await asyncio.sleep(0.4)
                continue

            parallel = max(2, int(getattr(creds, "AFK_PARALLEL", 4)))
            batch_gids: list[int] = []
            for _ in range(parallel):
                batch_gids.append(gift_ids[self.afk_cursor % n])
                self.afk_cursor = (self.afk_cursor + 1) % n

            async def _farm_one(gid: int) -> tuple[int, list, list, str, str, int]:
                offset = self.db.get_collection_offset(gid)
                lots, users, next_offset, total = await self.market.afk_fetch_page(
                    gid,
                    offset=offset,
                    limit=creds.AFK_PAGE_LIMIT,
                    gap=creds.AFK_GAP,
                    timeout=creds.API_TIMEOUT,
                )
                # сразу ещё страница — БД растёт быстрее, меньше одних и тех же
                if lots and next_offset:
                    try:
                        lots2, users2, next2, _t2 = await self.market.afk_fetch_page(
                            gid,
                            offset=next_offset,
                            limit=creds.AFK_PAGE_LIMIT,
                            gap=creds.AFK_GAP,
                            timeout=creds.API_TIMEOUT,
                        )
                        if lots2:
                            lots = list(lots) + list(lots2)
                            users = list(users) + list(users2)
                            next_offset = next2 or ""
                    except Exception:  # noqa: BLE001
                        pass
                title = lots[0].title if lots else ""
                return gid, lots, users, next_offset or "", title, int(total or 0)

            try:
                parts = await asyncio.gather(
                    *[_farm_one(g) for g in batch_gids],
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.afk_last_error = str(exc)
                await asyncio.sleep(0.4)
                continue

            total_u = self.db.count_users()
            for part in parts:
                if isinstance(part, Exception):
                    self.afk_last_error = str(part)
                    continue
                gid, lots, users, next_offset, title, _total = part
                if lots:
                    ins_m, _upd_m = self._save_models(lots)
                    self.afk_models_added += ins_m
                    self._ingest_always(lots)
                batch_users = list(users)
                for lot in lots:
                    if lot.seller_id is not None or lot.seller:
                        batch_users.append(
                            {
                                "user_id": lot.seller_id,
                                "username": lot.seller,
                                "first_name": lot.first_name,
                                "last_name": lot.last_name,
                            }
                        )
                try:
                    drain = self.market.drain_users()
                    if drain:
                        batch_users.extend(drain)
                except Exception:  # noqa: BLE001
                    pass
                ins_u, _upd_u, total_u = self.db.upsert_users(
                    batch_users, cap=creds.AFK_USER_CAP
                )
                self.afk_users_added += ins_u
                if ins_u:
                    try:
                        self.db.bump_daily(users_new=ins_u)
                    except Exception:  # noqa: BLE001
                        pass
                self.afk_pages += 1
                # пустая/конец — сброс offset, чтобы снова пройти глубже с начала
                new_offset = next_offset if (lots and next_offset) else ""
                self.db.touch_collection(
                    gid,
                    title=title,
                    last_offset=new_offset,
                    pages_inc=1 + (1 if next_offset else 0),
                    lots_inc=len(lots),
                )

            if self.afk_pages % 8 == 0:
                self.db.checkpoint()
                # периодически мешаем порядок коллекций
                if self.afk_pages % 40 == 0:
                    random.shuffle(gift_ids)
                    self.afk_cursor = random.randrange(n)

            now = time.monotonic()
            if (not self._afk_quiet) and now - last_status >= creds.AFK_STATUS_EVERY:
                last_status = now
                await self._edit_afk(
                    f"💾 <b>БД фарм</b>\n"
                    f"Коллекций: <b>{self.db.count_collections()}</b>/"
                    f"<b>{self.afk_collections_total}</b>\n"
                    f"Юзов: <b>{total_u:,}</b> / <b>{creds.AFK_USER_CAP:,}</b>\n"
                    f"+юзов: <b>{self.afk_users_added:,}</b> · "
                    f"+моделей: <b>{self.afk_models_added:,}</b>\n"
                    f"Моделей: <b>{self.db.count():,}</b>\n"
                    f"Страниц: <b>{self.afk_pages}</b>"
                )
            elif self._afk_quiet and self.afk_pages % 40 == 0:
                logger.info(
                    "DB farm · pages=%s users=%s models=%s +u=%s +m=%s",
                    self.afk_pages,
                    total_u,
                    self.db.count(),
                    self.afk_users_added,
                    self.afk_models_added,
                )

            await asyncio.sleep(0.01)

        self._afk_task = None
        # если не на паузе осознанно — вышли из-за ошибки/стопа
        if self._afk_paused and self.logged_in and not self.running:
            # внешний pause сам рестартнет через ensure
            pass

    async def _loop(self) -> None:
        """Один обход: скан → прогресс (чеки/проверки) → выдача по типам."""
        self.parse_checked = 0
        self.parse_ready = 0
        self._last_pool = []
        n_acc = max(1, len(self.db.list_accounts()[: creds.PARSE_ACCOUNTS]))
        stop_event = asyncio.Event()
        delivered = False

        async def _status() -> None:
            await self._edit_status(
                f"{screen('Парсинг')}\n"
                f"Обход <b>#{self.parse_rounds}</b> · акков {n_acc}\n"
                f"Чеков коллекций: <b>{self.parse_coll_checks}</b>\n"
                f"Проверок акка: <b>{self.parse_acc_checks}</b>\n"
                f"Типов: <b>{self.parse_types}</b> · "
                f"моделей: <b>{self.parse_models}</b>\n"
                f"Готово: <b>{self.parse_ready}</b> (по типам)"
            )

        await _status()
        last_prog = 0.0

        async def _burst_progress(
            done: int,
            total: int,
            lots_n: int,
            types_n: int = 0,
            models_n: int = 0,
        ) -> None:
            nonlocal last_prog
            nowp = time.monotonic()
            if nowp - last_prog < 0.7 and done < total:
                return
            last_prog = nowp
            self.parse_coll_checks = done
            self.parse_checked = lots_n
            self.parse_types = types_n
            self.parse_models = models_n
            await _status()

        async def _on_early(matched: list[Lot], done: int, total: int) -> None:
            nonlocal delivered
            if delivered or not self.running:
                return
            pool = [lot for lot in matched if self._in_price(lot)]
            self._ingest_always(matched)
            shown = await self._prepare_show(
                pool, limit=None, apply_extra=False, channel="parser"
            )
            # достаточно типов → сразу выдача и стоп скана
            min_types = 8
            if len(shown) >= min_types:
                delivered = True
                self.parse_ready = len(shown)
                stop_event.set()
                now_e = time.monotonic()
                for lot in shown:
                    self._seen[lot.id] = now_e
                await self._say_lot_list(shown, channel="parser")
                self.lots_notified += len(shown)
                self._last_pool = list(pool)
                await self._say(
                    f"{screen('Парсинг')}\n"
                    f"Обход #{self.parse_rounds} · выдал "
                    f"<b>{len(shown)}</b> типов · рандом\n"
                    f"Чеков: {self.parse_coll_checks} · "
                    f"проверок акка: {self.parse_acc_checks} · "
                    f"акков {n_acc}",
                    reply_markup=parse_done_inline(),
                )

        try:
            burst = await self.multi_burst(
                self.min_stars,
                self.max_stars,
                progress_cb=_burst_progress,
                early_show_at=max(10, creds.BURST_EARLY_SHOW_AT),
                on_early_lots=_on_early,
                stop_event=stop_event,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.running = False
            self._task = None
            await self._say(
                f"{screen('Парсинг')}\n⚠️ {_esc(str(exc)[:200])}",
                reply_markup=parse_done_inline(),
            )
            await self.ensure_db_farm()
            return

        if not self.running:
            await self.ensure_db_farm()
            return

        now = time.monotonic()
        to_save = (burst.all_lots or burst.lots) if burst else []
        self._ingest_always(to_save)
        if to_save:
            for lot in to_save:
                self._seen.setdefault(lot.id, now)

        if not delivered:
            price_pool = [lot for lot in to_save if self._in_price(lot)]
            random.shuffle(price_pool)
            self._last_pool = list(price_pool)
            self.parse_checked = len(
                {lot.owner_key for lot in price_pool if lot.seller or lot.seller_id}
            ) or len(price_pool)
            await _status()
            shown = await self._prepare_show(
                price_pool,
                limit=None,
                apply_extra=False,
                channel="parser",
            )
            # пусто/мало — ещё 1 добор (не тормозить 3 проходами)
            for retry in range(1):
                if len(shown) >= 8:
                    break
                if not self.running:
                    break
                await self._edit_status(
                    f"{screen('Парсинг')}\n"
                    f"Мало типов ({len(shown)}) · добор…"
                )
                self.market.reshuffle_collections()
                self._burst_deep = True
                try:
                    burst2 = await self.multi_burst(
                        self.min_stars,
                        self.max_stars,
                        progress_cb=_burst_progress,
                        early_show_at=0,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("retry burst: %s", exc)
                    break
                finally:
                    self._burst_deep = False
                extra = [
                    lot
                    for lot in (burst2.all_lots or burst2.lots or [])
                    if self._in_price(lot)
                ]
                self._last_pool = _dedupe_lots(self._last_pool + extra)
                self._ingest_always(extra)
                random.shuffle(self._last_pool)
                shown = await self._prepare_show(
                    self._last_pool,
                    limit=None,
                    apply_extra=False,
                    channel="parser",
                )
            self.parse_ready = len(shown)
            if burst:
                self.checks = burst.check_no
            if shown:
                for lot in shown:
                    self._seen[lot.id] = now
                await self._say_lot_list(shown, channel="parser")
                self.lots_notified += len(shown)
                await self._say(
                    f"{screen('Парсинг')}\n"
                    f"Обход #{self.parse_rounds} · выдал "
                    f"<b>{len(shown)}</b> типов · рандом\n"
                    f"Чеков: {self.parse_coll_checks} · "
                    f"проверок акка: {self.parse_acc_checks} · "
                    f"акков {n_acc}",
                    reply_markup=parse_done_inline(),
                )
            else:
                await self._say(
                    f"{screen('Парсинг')}\n"
                    f"Обход #{self.parse_rounds} · пока пусто\n"
                    f"Мало новых RU / free DM · жми Заново\n"
                    f"Чеков: {self.parse_coll_checks} · акков {n_acc}",
                    reply_markup=parse_done_inline(),
                )
        elif burst:
            self.checks = burst.check_no

        self.running = False
        self._task = None
        await self._close_extra_clients()
        await self.ensure_db_farm()

    async def deliver_again(self) -> None:
        """Ещё одна выдача других лотов из пула / быстрый добор."""
        if self.running:
            raise RuntimeError("Уже парсит — сначала стоп.")
        if not self.logged_in:
            raise RuntimeError("Сначала вход.")
        await self.pause_db_farm()
        self.running = True
        self.parse_rounds += 1
        self._task = asyncio.create_task(self._again_loop(), name="again")

    async def _again_loop(self) -> None:
        try:
            await self._edit_status(f"{screen('Парсинг')}\nЗаново · рандом…")
            shown: list[Lot] = []
            for attempt in range(2):
                if not self.running:
                    break
                await self._edit_status(
                    f"{screen('Парсинг')}\n"
                    f"Заново · скан {attempt + 1}/2 · акков "
                    f"{max(1, len(self.db.list_accounts()[: creds.PARSE_ACCOUNTS]))}"
                )
                self.market.reshuffle_collections()
                self._burst_deep = attempt > 0  # 2-й проход глубже
                try:
                    burst = await self.multi_burst(
                        self.min_stars,
                        self.max_stars,
                        progress_cb=None,
                        early_show_at=0,
                    )
                finally:
                    self._burst_deep = False
                extra = [
                    lot
                    for lot in (burst.all_lots or burst.lots or [])
                    if self._in_price(lot)
                ]
                pool = [lot for lot in self._last_pool if self._in_price(lot)]
                self._last_pool = _dedupe_lots(pool + extra)
                self._ingest_always(extra)
                random.shuffle(self._last_pool)
                shown = await self._prepare_show(
                    self._last_pool,
                    limit=None,
                    apply_extra=False,
                    channel="parser",
                    strict_russian=True,
                )
                self.parse_ready = len(shown)
                if len(shown) >= 8:
                    break
                # не чистим типы — иначе одни и те же коллекции снова
            if shown:
                now = time.monotonic()
                for lot in shown:
                    self._seen[lot.id] = now
                await self._say_lot_list(shown, channel="parser")
                self.lots_notified += len(shown)
                await self._say(
                    f"{screen('Парсинг')}\n"
                    f"Заново · <b>{len(shown)}</b> типов · рандом · RU\n"
                    f"Чеков: {self.parse_coll_checks} · "
                    f"проверок акка: {self.parse_acc_checks}",
                    reply_markup=parse_done_inline(),
                )
            else:
                await self._say(
                    f"{screen('Парсинг')}\n"
                    f"Пока пусто · мало новых RU/free DM\n"
                    f"Жми Заново или смени сложность",
                    reply_markup=parse_done_inline(),
                )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            await self._say(
                f"{screen('Парсинг')}\n⚠️ {_esc(str(exc)[:180])}",
                reply_markup=parse_done_inline(),
            )
        finally:
            self.running = False
            self._task = None
            self._burst_deep = False
            await self._close_extra_clients()
            await self.ensure_db_farm()

    async def _notify_lot(self, lot: Lot, count_as_new: bool) -> None:
        if not self.chat_id:
            return
        await self._notify_lot_to(self.chat_id, lot, count_as_new=count_as_new)

    async def _notify_lot_to(
        self, chat_id: int, lot: Lot, count_as_new: bool
    ) -> None:
        if not self.bot or not chat_id or not lot.seller:
            return
        if self._bad_username_len(lot.seller):
            return
        if self._is_blocked_lot(lot):
            return
        text = self._format_lot_line(lot)
        if count_as_new:
            text = "🆕 " + text
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🖼 NFT / LINK", url=lot.nft_url)]
        ]
        action_row: list[InlineKeyboardButton] = []
        if re.fullmatch(r"[A-Za-z0-9_]{6,64}", lot.seller):
            action_row.append(
                InlineKeyboardButton(
                    text="✍️ Написать", url=f"https://t.me/{lot.seller}"
                )
            )
            action_row.append(
                InlineKeyboardButton(
                    text="🚫 Блок", callback_data=f"block:{lot.seller}"
                )
            )
        elif lot.seller_id is not None:
            action_row.append(
                InlineKeyboardButton(
                    text="🚫 Блок", callback_data=f"blockid:{lot.seller_id}"
                )
            )
        if action_row:
            rows.append(action_row)
        try:
            await self.bot.send_message(
                chat_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=False),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("notify: %s", exc)


app = App()


def _dedupe_lots(lots: list[Lot]) -> list[Lot]:
    seen: set[str] = set()
    out: list[Lot] = []
    for lot in lots:
        if lot.id in seen:
            continue
        seen.add(lot.id)
        out.append(lot)
    return out


def _filters_label(f: SearchFilters) -> str:
    parts = ["бан юз 4–5"]
    parts.append(f"мало gifts≤{f.max_gifts}" if f.few_gifts else "gifts:any")
    parts.append(f"lvl≤{f.max_level}" if f.low_level else "lvl:any")
    if f.short_username:
        parts.append(f"юз 6–{f.short_user_max}")
    elif f.long_username:
        parts.append(f"юз ≥{f.long_user_min}")
    else:
        parts.append("юз:any (кроме 4–5)")
    parts.append("no TGP" if f.no_premium else "TGP:any")
    parts.append("🟢 в сети" if f.online_only else "сеть:any")
    parts.append("🆕 48ч" if f.fresh_only else "возраст:any")
    parts.append("💎 редкие" if f.rare_types else "типы:any")
    parts.append("био" if f.with_bio else "био:any")
    parts.append("модель" if f.with_model else "модель:any")
    parts.append("юз без цифр" if f.no_digits_user else "цифры:ok")
    parts.append("free✓" if f.strict_free else "free±")
    parts.append("🎲 микс" if f.random_mix else "микс:off")
    return " · ".join(parts)


def main_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1️⃣ Парсинг", callback_data="menu:parse")],
            [
                InlineKeyboardButton(
                    text="2️⃣ Парсер по фильтрам", callback_data="menu:filters"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗄 Старый парс", callback_data="menu:old"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📡 Парсинги", callback_data="menu:jobs"
                )
            ],
            [InlineKeyboardButton(text="3️⃣ Настройки", callback_data="menu:settings")],
        ]
    )


def difficulty_inline(prefix: str = "diff") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{rid}")]
        for rid, label, _, _ in DIFFICULTIES
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_ready_inline() -> InlineKeyboardMarkup:
    """После выбора сложности — старт / стоп."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="▶️ Начать парсить", callback_data="parse:go")],
    ]
    if app.running:
        rows.append(
            [InlineKeyboardButton(text="⏹ Стоп", callback_data="menu:stop")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:parse")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_done_inline() -> InlineKeyboardMarkup:
    """После листинга — заново / стоп / меню."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔄 Заново", callback_data="parse:again")],
    ]
    if app.running:
        rows.append(
            [InlineKeyboardButton(text="⏹ Стоп", callback_data="menu:stop")]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="🎯 Сложность", callback_data="menu:parse")],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prices_inline(prefix: str = "price") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"✔️ {label}", callback_data=f"{prefix}:{rid}")]
        for rid, label, _, _ in PRICE_RANGES
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def filters_inline() -> InlineKeyboardMarkup:
    f = app.filters
    diff_row = [
        InlineKeyboardButton(
            text=("•" if rid == _current_filter_diff_id() else "") + short,
            callback_data=f"fdiff:{rid}",
        )
        for rid, short in (
            ("easy", "🟢"),
            ("mid", "🟡"),
            ("hard", "🔴"),
            ("impos", "💀"),
        )
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            diff_row,
            [
                InlineKeyboardButton(
                    text=("✅" if f.few_gifts else "⬜️") + " Мало гифтов",
                    callback_data="flt:few",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.low_level else "⬜️") + " Мелкий lvl",
                    callback_data="flt:lvl",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.short_username else "⬜️") + " Короткий юз",
                    callback_data="flt:short",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.long_username else "⬜️") + " Длинный юз",
                    callback_data="flt:long",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.no_premium else "⬜️") + " Без TGP",
                    callback_data="flt:tgp",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.online_only else "⬜️") + " В сети (онлайн)",
                    callback_data="flt:online",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.fresh_only else "⬜️") + " Свежие 48ч",
                    callback_data="flt:fresh",
                ),
                InlineKeyboardButton(
                    text=("✅" if f.rare_types else "⬜️") + " Редкие",
                    callback_data="flt:rare",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.with_bio else "⬜️") + " С био",
                    callback_data="flt:bio",
                ),
                InlineKeyboardButton(
                    text=("✅" if f.with_model else "⬜️") + " С моделью",
                    callback_data="flt:model",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.no_digits_user else "⬜️") + " Юз без цифр",
                    callback_data="flt:nodigit",
                ),
                InlineKeyboardButton(
                    text=("✅" if f.strict_free else "⬜️") + " Строго free",
                    callback_data="flt:frestrict",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=("✅" if f.random_mix else "⬜️") + " Рандом-микс",
                    callback_data="flt:mix",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Искать", callback_data="flt:run"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
        ]
    )


def _current_filter_diff_id() -> str:
    for rid, _label, mn, mx in DIFFICULTIES:
        if (
            abs(app.filter_min_stars - mn) < 1
            and abs(app.filter_max_stars - mx) < 1
        ):
            return rid
    return ""


def _filters_menu_text() -> str:
    return (
        f"{screen('Фильтры')}\n"
        f"{app.filter_range_label}\n"
        f"{_filters_label(app.filters)}"
    )


def settings_inline() -> InlineKeyboardMarkup:
    stop = "⏹ Стоп" if app.running else "▶️ Парсинг выкл"
    speed = f"Скорость · {app.speed_label}"
    nacc = len(app.db.list_accounts())
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=speed, callback_data="menu:speed")],
            [
                InlineKeyboardButton(
                    text=f"👤 Аккаунты ({nacc})", callback_data="menu:accounts"
                )
            ],
            [InlineKeyboardButton(text="📡 Парсинги", callback_data="menu:jobs")],
            [InlineKeyboardButton(text="📅 /daily", callback_data="menu:daily")],
            [InlineKeyboardButton(text=stop, callback_data="menu:stop")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
        ]
    )


def jobs_inline() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if app.running:
        rows.append(
            [InlineKeyboardButton(text="⏹ Стоп парсер", callback_data="menu:stop")]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:jobs")]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def accounts_inline() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for acc in app.db.list_accounts():
        mark = "✅ " if acc.get("is_active") else ""
        label = acc.get("label") or acc.get("phone") or f"#{acc['id']}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{label}",
                    callback_data=f"acc:use:{acc['id']}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"acc:del:{acc['id']}",
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить акк", callback_data="acc:add")]
    )
    if len(app.db.list_accounts()) > 1:
        rows.append(
            [InlineKeyboardButton(text="🔀 Ротация", callback_data="acc:rotate")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _normalize_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("8") and len(phone) == 11:
        phone = "+7" + phone[1:]
    if not phone.startswith("+"):
        phone = "+" + phone
    if not re.fullmatch(r"\+\d{10,15}", phone):
        raise ValueError("Формат: +79991234567")
    return phone


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value)):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def wipe_disk_junk() -> None:
    """Чистит session-мусор. gifts.db / WAL / blocklist — НИКОГДА не трогаем."""
    root = Path(__file__).resolve().parent
    data = root / "data"
    data.mkdir(exist_ok=True)

    def _is_gifts_db(path: Path) -> bool:
        name = path.name.lower()
        return name.startswith("gifts.db") or "gifts.db" in name

    for folder in (data, root):
        for pattern in ("*session*", "*.session*", "*.session-journal"):
            for path in folder.glob(pattern):
                if path.is_file() and not _is_gifts_db(path):
                    try:
                        path.unlink()
                    except OSError:
                        pass
        for pattern in ("*.db", "*.db-*", "*.sqlite*"):
            for path in folder.glob(pattern):
                if _is_gifts_db(path):
                    continue
                # в data/ кроме gifts.* ничего критичного не удаляем
                if folder == data:
                    continue
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass


def _range_by_id(rid: str) -> tuple[str, int, int] | None:
    for i, label, mn, mx in PRICE_RANGES:
        if i == rid:
            return label, mn, mx
    return None


def _diff_by_id(rid: str) -> tuple[str, int, int] | None:
    for i, label, mn, mx in DIFFICULTIES:
        if i == rid:
            return label, mn, mx
    return None


async def _send_menu(target: Message | CallbackQuery, prefix: str = "") -> None:
    text = screen("Меню")
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=main_inline())
        await target.answer()
    else:
        await target.answer(text, reply_markup=main_inline())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if app.logged_in:
        await _send_menu(message)
        return
    if await app.try_restore_account():
        await _send_menu(message)
        return
    wipe_disk_junk()
    await state.set_state(AuthStates.phone)
    await message.answer(
        f"{screen('Вход')}\n📱 <code>+79991234567</code>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    text = await app.stop_all_jobs()
    await message.answer(text, reply_markup=main_inline() if app.logged_in else None)


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await app.reset_auth()
    await state.set_state(AuthStates.phone)
    await message.answer("Сброшено. Номер:")


@router.message(StateFilter(AuthStates.phone))
async def got_phone(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    try:
        reply = await app.send_code(text)
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    await state.set_state(AuthStates.code)
    await message.answer(f"{reply} Пришли код:")


@router.message(StateFilter(AuthStates.code))
async def got_code(message: Message, state: FSMContext) -> None:
    try:
        result = await app.confirm_code(message.text or "")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    if result == "NEED_PASSWORD":
        await state.set_state(AuthStates.password)
        await message.answer("🔒 2FA:")
        return
    await state.clear()
    await _send_menu(message)


@router.message(StateFilter(AuthStates.password))
async def got_password(message: Message, state: FSMContext) -> None:
    try:
        await app.confirm_password(message.text or "")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await _send_menu(message)


@router.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала /start", show_alert=True)
        return
    await _send_menu(callback)


@router.callback_query(F.data == "menu:parse")
async def cb_parse(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    await callback.message.edit_text(
        screen("Парсинг"),
        reply_markup=difficulty_inline("diff"),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:old")
async def cb_old_menu(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    await callback.message.edit_text(
        f"{screen('Старый парс')}\nлоты за 24ч · выбери цену",
        reply_markup=difficulty_inline("old"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("old:"))
async def cb_old_start(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    if app.old_parse_running:
        await callback.answer("Уже идёт", show_alert=True)
        return
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _diff_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    app.set_range(label, mn, mx)
    await callback.message.edit_text(
        f"{screen('Старый парс')}\n{label}\nищу за 24ч…",
        reply_markup=main_inline(),
    )
    await callback.answer("Старый парс")
    app._old_task = asyncio.create_task(
        app.run_old_parse(callback.from_user.id, hours=24.0),
        name="old-parse",
    )


@router.callback_query(F.data == "menu:filters")
async def cb_filters(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    await callback.message.edit_text(
        _filters_menu_text(),
        reply_markup=filters_inline(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fdiff:"))
async def cb_filter_diff(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    rid = (callback.data or "").split(":", 1)[-1]
    if rid == "noop":
        await callback.answer("Сначала выбери 🟢🟡🔴💀", show_alert=False)
        return
    chosen = _diff_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    app.set_filter_range(label, mn, mx)
    await callback.message.edit_text(
        _filters_menu_text(),
        reply_markup=filters_inline(),
    )
    await callback.answer(f"Сложность поиска: {label}")


@router.callback_query(F.data.startswith("flt:"))
async def cb_filter_toggle(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    key = (callback.data or "").split(":", 1)[-1]
    if key == "few":
        app.filters.few_gifts = not app.filters.few_gifts
    elif key == "lvl":
        app.filters.low_level = not app.filters.low_level
    elif key == "short":
        app.filters.short_username = not app.filters.short_username
        if app.filters.short_username:
            app.filters.long_username = False
    elif key == "long":
        app.filters.long_username = not app.filters.long_username
        if app.filters.long_username:
            app.filters.short_username = False
    elif key == "tgp":
        app.filters.no_premium = not app.filters.no_premium
    elif key == "online":
        app.filters.online_only = not app.filters.online_only
    elif key == "fresh":
        app.filters.fresh_only = not app.filters.fresh_only
    elif key == "rare":
        app.filters.rare_types = not app.filters.rare_types
    elif key == "mix":
        app.filters.random_mix = not app.filters.random_mix
    elif key == "bio":
        app.filters.with_bio = not app.filters.with_bio
    elif key == "model":
        app.filters.with_model = not app.filters.with_model
    elif key == "nodigit":
        app.filters.no_digits_user = not app.filters.no_digits_user
    elif key == "frestrict":
        app.filters.strict_free = not app.filters.strict_free
    elif key == "run":
        if app.filter_search_running:
            await callback.answer("Уже идёт", show_alert=True)
            return
        await callback.answer("Ищу…")
        await callback.message.edit_text(
            screen("Фильтры"),
            reply_markup=main_inline(),
        )
        app._filter_task = asyncio.create_task(
            app.run_filter_search(callback.from_user.id),
            name="filter-search",
        )
        return
    else:
        await callback.answer("?")
        return
    await callback.message.edit_text(
        _filters_menu_text(),
        reply_markup=filters_inline(),
    )
    await callback.answer("Ок")


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        screen("Настройки"),
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:jobs")
async def cb_jobs(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        app.parse_status_text(),
        reply_markup=jobs_inline(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:speed")
async def cb_speed(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    label = app.cycle_speed()
    await callback.message.edit_text(
        screen("Настройки"),
        reply_markup=settings_inline(),
    )
    await callback.answer(label)


@router.callback_query(F.data == "menu:accounts")
async def cb_accounts(callback: CallbackQuery) -> None:
    if not app.logged_in and not app.db.list_accounts():
        await callback.answer("Сначала вход", show_alert=True)
        return
    lines = [screen("Аккаунты")]
    for acc in app.db.list_accounts():
        mark = "✅" if acc.get("is_active") else "·"
        lines.append(
            f"{mark} {_esc(str(acc.get('label') or acc.get('phone') or acc['id']))}"
        )
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=accounts_inline(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("acc:"))
async def cb_acc_action(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "add":
        await app.start_add_account()
        await state.set_state(AuthStates.phone)
        await callback.message.edit_text(
            "➕ Новый акк для ротации.\n"
            "Старый останется в списке.\n"
            "📱 Пришли номер: <code>+79991234567</code>"
        )
        await callback.answer("Жду номер")
        return
    if action == "rotate":
        try:
            text = await app.rotate_account()
        except Exception as exc:  # noqa: BLE001
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        await callback.message.edit_text(text, reply_markup=accounts_inline())
        await callback.answer("Ротация")
        return
    if action in ("use", "del") and len(parts) > 2:
        try:
            acc_id = int(parts[2])
        except ValueError:
            await callback.answer("?", show_alert=True)
            return
        if action == "del":
            if app.active_account_id == acc_id and len(app.db.list_accounts()) <= 1:
                await callback.answer("Нельзя удалить единственный акк", show_alert=True)
                return
            app.db.delete_account(acc_id)
            if app.active_account_id == acc_id:
                nxt = app.db.next_account_id(None)
                if nxt:
                    try:
                        await app.switch_account(nxt)
                    except Exception:  # noqa: BLE001
                        pass
            await callback.message.edit_text(
                "🗑 Удалил.\n"
                f"Сейчас: <b>{app.account_name or '—'}</b>",
                reply_markup=accounts_inline(),
            )
            await callback.answer("Удалено")
            return
        try:
            text = await app.switch_account(acc_id)
        except Exception as exc:  # noqa: BLE001
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        await callback.message.edit_text(text, reply_markup=accounts_inline())
        await callback.answer("Переключил")
        return
    await callback.answer("?")


@router.callback_query(F.data == "menu:daily")
async def cb_daily(callback: CallbackQuery) -> None:
    st = app.db.get_daily_stats()
    text = (
        f"{screen('Daily')}\n"
        f"Юзов всего: <b>{st['users_total']:,}</b> · "
        f"сегодня +{st['users_new']:,}\n"
        f"Лотов всего: <b>{st['lots_total']:,}</b> · "
        f"сегодня +{st['lots_new']:,}\n"
        f"Выдано: {st['lots_shown']:,} · NFT {st['unique_titles']:,}\n"
        f"Обход #{app.parse_rounds} · чеков {app.parse_coll_checks} · "
        f"проверок акка {app.parse_acc_checks}"
    )
    await callback.message.edit_text(text, reply_markup=settings_inline())
    await callback.answer()


@router.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    st = app.db.get_daily_stats()
    await message.answer(
        f"{screen('Daily')}\n"
        f"Юзов: <b>{st['users_total']:,}</b> (+{st['users_new']:,})\n"
        f"Лотов: <b>{st['lots_total']:,}</b> (+{st['lots_new']:,})\n"
        f"Выдано {st['lots_shown']:,} · NFT {st['unique_titles']:,}\n"
        f"Обход #{app.parse_rounds} · чеков {app.parse_coll_checks} · "
        f"проверок акка {app.parse_acc_checks}"
    )


@router.callback_query(F.data == "menu:stop")
async def cb_stop(callback: CallbackQuery) -> None:
    text = await app.stop_all_jobs()
    await callback.message.edit_text(text, reply_markup=parse_done_inline())
    await callback.answer("Стоп")


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        screen("Настройки"),
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("block:"))
async def cb_block_user(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    username = (callback.data or "").split(":", 1)[-1].lstrip("@").strip()
    if not username:
        await callback.answer("?", show_alert=True)
        return
    app.block_seller(username=username)
    await callback.answer(f"🚫 @{username} в блоклисте", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("blockid:"))
async def cb_block_id(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    raw = (callback.data or "").split(":", 1)[-1]
    try:
        uid = int(raw)
    except ValueError:
        await callback.answer("?", show_alert=True)
        return
    app.block_seller(user_id=uid)
    await callback.answer(f"🚫 id:{uid} в блоклисте", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.message(Command("block"))
async def cmd_block(message: Message) -> None:
    if not app.logged_in:
        await message.answer("Сначала /start")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пример: <code>/block username</code>")
        return
    username = parts[1].lstrip("@").strip()
    app.block_seller(username=username)
    await message.answer(f"🚫 @{username} заблокирован · больше не покажу")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message) -> None:
    if not app.logged_in:
        await message.answer("Сначала /start")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пример: <code>/unblock username</code>")
        return
    username = parts[1].lstrip("@").strip()
    app.db.unblock_user(username=username)
    app._blocked_keys.discard(username.lower())
    app._blocked_keys.discard(f"u:{username.lower()}")
    app._seen_sellers.discard(username.lower())
    await message.answer(f"✅ @{username} разблокирован")


@router.message(Command("blocked"))
async def cmd_blocked(message: Message) -> None:
    rows = app.db.list_blocked(40)
    if not rows:
        await message.answer("Блоклист пуст.")
        return
    lines = []
    for r in rows:
        u = r.get("username") or ""
        uid = r.get("user_id")
        label = f"@{u}" if u else f"id:{uid}"
        lines.append(f"• {label}")
    await message.answer(
        f"🚫 Блоклист ({app.db.count_blocked()}):\n" + "\n".join(lines)
    )


@router.callback_query(F.data.startswith("diff:"))
async def cb_diff_pick(callback: CallbackQuery) -> None:
    """Сложность → экран с кнопкой Начать парсить (не старт сразу)."""
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _diff_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    app.pending_range_label = label
    app.pending_min_stars = float(mn)
    app.pending_max_stars = float(mx)
    state = "▶️ идёт" if app.running else "⏹ стоп"
    await callback.message.edit_text(
        f"{screen('Парсинг')}\n{label}\n{state}",
        reply_markup=parse_ready_inline(),
    )
    await callback.answer(label)


@router.callback_query(F.data == "parse:go")
async def cb_parse_go(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    if app.running:
        await callback.answer("Уже парсит", show_alert=True)
        return
    app.set_range(
        app.pending_range_label,
        int(app.pending_min_stars),
        int(app.pending_max_stars),
    )
    await callback.message.edit_text(
        f"{screen('Парсинг')}\n{app.range_label}\n▶️ старт…",
        reply_markup=parse_done_inline(),
    )
    await callback.answer("Старт")
    try:
        await app.start_monitor(callback.from_user.id)
    except RuntimeError as exc:
        await callback.message.answer(f"⚠️ {exc}")


@router.callback_query(F.data == "parse:again")
async def cb_parse_again(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    if app.running:
        await callback.answer("Уже парсит", show_alert=True)
        return
    await callback.answer("Заново")
    await callback.message.edit_text(
        f"{screen('Парсинг')}\n🔄 …",
        reply_markup=parse_done_inline(),
    )
    try:
        await app.deliver_again()
    except RuntimeError as exc:
        await callback.message.answer(f"⚠️ {exc}")


@router.callback_query(F.data.startswith("price:"))
async def cb_price_start(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _range_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    app.pending_range_label = label
    app.pending_min_stars = float(mn)
    app.pending_max_stars = float(mx)
    await callback.message.edit_text(
        f"{screen('Парсинг')}\n{label}",
        reply_markup=parse_ready_inline(),
    )
    await callback.answer()


async def main() -> None:
    wipe_disk_junk()
    bot = Bot(
        token=creds.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    app.bot = bot
    try:
        await app.try_restore_account()
        if app.logged_in:
            await app.ensure_db_farm()
    except Exception:  # noqa: BLE001
        pass
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Меню"),
            BotCommand(command="stop", description="Стоп"),
            BotCommand(command="daily", description="Стата за день"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    dp = Dispatcher(storage=MemoryStorage())
    router.message.middleware(OwnerOnlyMiddleware())
    router.callback_query.middleware(OwnerOnlyMiddleware())
    dp.include_router(router)
    logger.info("Ready | Neptun Parser · owner-only · multi-acc")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        await app.stop_afk()
        await app._close_extra_clients()
        if app.client.is_connected():
            await app.client.disconnect()
        app.db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
