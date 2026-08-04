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
    max_gifts: int = 5
    max_level: int = 5
    short_user_max: int = 8
    long_user_min: int = 9


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
        # новый обход: свежие seen юзов/типов (блоклист оставляем)
        self._seen.clear()
        self._seen_titles.clear()
        self._recent_titles.clear()
        self._seen_sellers.clear()
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
        )

    def _passes_extra_filters(self, lot: Lot) -> bool:
        """TGP/lvl/длинный юз — жёстко, без проскока в fallback."""
        f = self.filters
        if f.few_gifts:
            if lot.gifts_count is not None and lot.gifts_count > f.max_gifts:
                return False
        if f.low_level:
            if lot.account_level is None or lot.account_level > f.max_level:
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
        return True

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
        # парсер: типы не повторяем в рамках обхода (Заново)
        seen_titles = set(self._seen_titles) if channel == "parser" else set()
        if is_old or ignore_seen:
            seen_sellers: set[str] = set()
            seen_models: set[str] = set()
            recent_list: list[str] = []
        else:
            seen_sellers = (
                self._filter_seen_sellers if is_filter else self._seen_sellers
            )
            seen_models = (
                self._filter_seen_models if is_filter else self._seen_models
            )
            recent_list = (
                self._filter_recent_titles if is_filter else self._recent_titles
            )
        # юзы, которых уже выдавали — ВСЕГДА режем в парсере (даже при fallback)
        always_block_sellers: set[str] = set()
        if channel == "parser":
            always_block_sellers = set(self._seen_sellers)

        lots = list(lots)
        random.shuffle(lots)
        buckets: dict[str, list[Lot]] = {}
        keys: list[str] = []
        local_sellers: set[str] = set()
        local_models: set[str] = set()
        show_counts = (
            {}
            if (is_filter or is_old or ignore_seen)
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
            # платные Stars / Premium DM — не выдаём; неизвестно (None) — ок
            if require_free_dm and lot.free_dm is False:
                continue
            # тумблеры (TGP/lvl/длинный юз) — всегда, даже в fallback
            if is_filter and self._filters_active() and not self._passes_extra_filters(lot):
                continue
            # уже выданные юзы в ЭТОМ обходе — не повторяем
            if channel == "parser":
                if lot.owner_key in always_block_sellers:
                    continue
            if not ignore_seen:
                if lot.owner_key in seen_sellers:
                    continue
                # модели: в парсере НЕ режем по истории БД — только юзы + типы
                if channel != "parser" and lot.model_key in seen_models:
                    continue
                if (
                    is_filter
                    and (not ignore_seen)
                    and lot.owner_key in self._filter_seen_sellers
                ):
                    continue
            if lot.owner_key in local_sellers:
                continue
            # в одной выдаче — разные модели; между обходами модели можно снова
            if lot.model_key in local_models:
                continue
            tk = self._title_key(lot)
            if not tk:
                continue
            # повторные типы NFT не выдаём в парсере (этот обход / Заново)
            if channel == "parser" and tk in seen_titles:
                continue
            if tk not in buckets:
                buckets[tk] = []
                keys.append(tk)
            buckets[tk].append(lot)
            local_sellers.add(lot.owner_key)
            local_models.add(lot.model_key)

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
                if channel == "parser" and lot.owner_key in always_block_sellers:
                    continue
                if (not ignore_seen) and lot.owner_key in seen_sellers:
                    continue
                if (
                    channel != "parser"
                    and (not ignore_seen)
                    and lot.model_key in seen_models
                ):
                    continue
                if lot.owner_key in marked_sellers:
                    continue
                if lot.model_key in marked_models:
                    continue
                return lot
            return None

        marked_sellers: set[str] = set()
        marked_models: set[str] = set()

        def _mark(lot: Lot) -> None:
            marked_sellers.add(lot.owner_key)
            marked_models.add(lot.model_key)

        primary: list[Lot] = []
        # парсер: всегда рандомный порядок типов (разброс)
        if channel == "parser":
            ordered = list(keys)
            random.shuffle(ordered)
        else:
            ordered = sorted(keys, key=_rank)
        per_type = max(1, int(getattr(creds, "PER_TYPE", 1)))
        max_types = int(getattr(creds, "MAX_TYPES", 0) or 0)
        by_types = (not is_filter) and channel == "parser" and (
            limit is None or limit <= 0
        )
        take_all = channel == "old"

        if by_types:
            types_taken = 0
            for tk in ordered:
                if max_types > 0 and types_taken >= max_types:
                    break
                got = 0
                while got < per_type:
                    lot = _take(tk)
                    if lot is None:
                        break
                    primary.append(lot)
                    _mark(lot)
                    got += 1
                if got:
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

        if not ignore_seen and track_seen:
            for lot in result:
                tk = self._title_key(lot)
                if tk:
                    recent_list.append(tk)
                    if channel == "parser":
                        self._seen_titles.add(tk)
            if len(recent_list) > 250:
                del recent_list[:-250]
            if channel == "parser":
                for lot in result:
                    self._seen_sellers.add(lot.owner_key)
                    self._seen_models.add(lot.model_key)
                    self.db.mark_seen_seller(
                        username=lot.seller, user_id=lot.seller_id
                    )
                    self.db.mark_seen_model(
                        lot.model_key, title=lot.model or lot.title
                    )
                    try:
                        self.db.bump_collection_shown(lot.title or self._title_key(lot) or "")
                    except Exception:  # noqa: BLE001
                        pass
            elif channel == "filter":
                for lot in result:
                    self._filter_seen_sellers.add(lot.owner_key)
                    self._filter_seen_models.add(lot.model_key)
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
        """Сначала строго; парсер/фильтры — стабильная выдача без пустоты."""
        target = (
            max(30, int(creds.SHOW_LIMIT))
            if channel in ("parser", "filter") and (limit is None or limit <= 0)
            else (limit or max(30, creds.SHOW_LIMIT))
        )
        keep_ru = (
            bool(self.require_russian)
            if strict_russian is None
            else bool(strict_russian)
        )
        if channel == "parser":
            # RU+free → RU → RU ignore session-models; sellers сессии всегда блок
            attempts = [
                (True, True, False, False),
                (True, False, False, False),
                (True, False, True, False),
            ]
        elif channel == "old" and keep_ru:
            attempts = [
                (True, True, True, False),
                (True, False, True, False),
            ]
        else:
            # фильтры: RU; тумблеры не снимаем
            keep_extra = bool(apply_extra or self._filters_active())
            attempts = [
                (True, True, False, keep_extra),
                (True, True, True, keep_extra),
                (True, False, True, keep_extra),
            ]
        best: list[Lot] = []
        for want_ru, want_free, ign_seen, extra in attempts:
            out = self._pick_clean(
                lots,
                limit=None if channel == "parser" else limit,
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
            if len(best) >= min(15, target):
                break
        if channel in ("parser", "filter") and best:
            best = best[: max(30, int(creds.SHOW_LIMIT))]
        if best and track_seen and channel == "parser":
            for lot in best:
                self._seen_sellers.add(lot.owner_key)
                self._seen_models.add(lot.model_key)
                tk = self._title_key(lot)
                if tk:
                    self._seen_titles.add(tk)
                    self._recent_titles.append(tk)
                try:
                    self.db.mark_seen_seller(
                        username=lot.seller, user_id=lot.seller_id
                    )
                    self.db.mark_seen_model(
                        lot.model_key, title=lot.model or lot.title
                    )
                    if lot.title:
                        self.db.bump_collection_shown(lot.title)
                except Exception:  # noqa: BLE001
                    pass
            if len(self._recent_titles) > 250:
                del self._recent_titles[:-250]
        elif best and channel == "filter":
            for lot in best:
                self._filter_seen_sellers.add(lot.owner_key)
                self._filter_seen_models.add(lot.model_key)
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
        # быстрее: меньше resolve на старте
        resolve_n = min(
            len(without),
            120 if by_types else (220 if take_all else max(lim * 3, 60)),
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
        ]
        # парсер: только юзы уже выданные в ЭТОМ обходе (не вечная БД seen)
        if channel == "parser":
            candidates = [
                lot
                for lot in candidates
                if lot.owner_key not in self._seen_sellers
            ]

        want_ru = (
            bool(self.require_russian)
            if strict_russian is None
            else bool(strict_russian)
        )
        if channel == "parser":
            want_ru = True

        if channel == "parser" and candidates:
            already_ru = [lot for lot in candidates if self._is_russian(lot)]
            need_bio = [lot for lot in candidates if not self._is_russian(lot)]
            random.shuffle(already_ru)
            random.shuffle(need_bio)
            check_ru = already_ru[:300]
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
            # всегда добираем био если мало типов (иначе то пусто то 1)
            if len(titles_ready) < 25:
                wave = need_bio[:250]
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
            # если всё ещё мало — берём RU без требования free_dm True
            if len({self._title_key(x) for x in ready}) < 8:
                ready = [
                    lot
                    for lot in (check_ru + ready)
                    if self._is_russian(lot) and lot.free_dm is not False
                ]
            candidates = _dedupe_lots(ready)
            self._ingest_always(candidates)
        elif channel == "filter" and candidates:
            sample_n = min(len(candidates), max(250, lim * 10, 200))
            random.shuffle(candidates)
            # сначала с кириллицей
            candidates.sort(
                key=lambda lot: (0 if self._is_russian(lot) else 1, random.random())
            )
            sample = candidates[:sample_n]
            await self.market.enrich_profiles(
                sample,
                timeout=min(max(creds.OWNER_TIMEOUT, 0.7), 1.1),
                parallel=getattr(creds, "ENRICH_PARALLEL", 8),
            )
            await self.market.check_free_dm(
                sample,
                timeout=max(creds.OWNER_TIMEOUT, 1.0),
            )
            self.parse_acc_checks += len(sample) * 2
            if self.filters.online_only:
                await self.market.refresh_online(
                    sample,
                    timeout=max(creds.OWNER_TIMEOUT, 1.2),
                )
                self.parse_acc_checks += len(sample)
            self._ingest_always(sample)
            candidates = sample
        elif candidates:
            sample_n = min(
                len(candidates),
                400 if take_all else max(lim * 6, 120),
            )
            if need_full or apply_extra:
                sample_n = min(len(candidates), max(sample_n, lim * 8, 150))
            need_bio = list(candidates[:sample_n])
            if need_bio:
                await self.market.enrich_profiles(
                    need_bio,
                    timeout=min(max(creds.OWNER_TIMEOUT, 0.8), 1.2),
                    parallel=getattr(creds, "ENRICH_PARALLEL", 8),
                )
                self.parse_acc_checks += len(need_bio)
            await self.market.check_free_dm(
                candidates[:sample_n],
                timeout=max(creds.OWNER_TIMEOUT, 1.2),
            )
            self.parse_acc_checks += len(candidates[:sample_n])
            self._ingest_always(candidates[:sample_n])
            candidates = candidates[:sample_n]

        return self._pick_with_fallback(
            candidates,
            limit=None if by_types else lim,
            apply_extra=bool(apply_extra and channel == "filter"),
            track_seen=bool(track_seen and channel == "parser"),
            channel=channel,
            strict_russian=want_ru if channel == "parser" else False,
        )

    async def run_filter_search(self, chat_id: int) -> None:
        """Отдельный поиск по фильтрам — НЕ связан с парсером/монитором."""
        if self.filter_search_running:
            await self._say_to(chat_id, f"{screen('Фильтры')}\nуже идёт")
            return
        self.filter_search_running = True
        # каждый запуск фильтров — свежий seen, иначе быстро 0
        self._filter_seen_sellers.clear()
        self._filter_seen_models.clear()
        self._filter_recent_titles.clear()
        await self.pause_db_farm()
        f = self.filters
        mn, mx = self.filter_min_stars, self.filter_max_stars
        label = self.filter_range_label
        try:
            online_note = " · 🟢 в сети" if f.online_only else ""
            await self._say_to(chat_id, f"{screen('Фильтры')}\n{label}{online_note}")
            db_lim = max(400, int(getattr(creds, "FILTER_DB_LIMIT", 120)) * 4)
            old = self.db.fetch_random_lots(
                min_stars=mn,
                max_stars=mx,
                limit=db_lim,
                require_seller=False,
            )
            live: list[Lot] = []
            try:
                burst = await self.multi_burst(
                    mn,
                    mx,
                    progress_cb=None,
                    early_show_at=0,
                )
                if burst:
                    raw = list(burst.all_lots or burst.lots or [])
                    if raw:
                        self._save_models(raw)
                    self._flush_market_users()
                    live = list(burst.lots or raw)
            except Exception as exc:  # noqa: BLE001
                await self._say_to(
                    chat_id, f"{screen('Фильтры')}\n⚠️ {_esc(str(exc)[:120])}"
                )

            merged = _dedupe_lots(old + live)
            random.shuffle(merged)
            self._ingest_always(merged)
            shown = await self._prepare_show(
                merged,
                limit=max(30, int(creds.SHOW_LIMIT)),
                apply_extra=True,
                track_seen=False,
                need_full=True,
                channel="filter",
            )
            # пусто — ещё один live без лишних фильтров-extra
            if not shown and merged:
                shown = await self._prepare_show(
                    merged,
                    limit=max(30, int(creds.SHOW_LIMIT)),
                    apply_extra=False,
                    track_seen=False,
                    need_full=True,
                    channel="filter",
                )
            if not shown:
                await self._say_to(
                    chat_id,
                    f"{screen('Фильтры')}\nпусто",
                    reply_markup=filters_inline(),
                )
                return
            await self._say_lot_list_to(chat_id, shown, channel="filter")
            await self._say_to(
                chat_id,
                f"{screen('Фильтры')}\nготово · {len(shown)}",
                reply_markup=filters_inline(),
            )
        finally:
            self.filter_search_running = False
            self._filter_task = None
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
        lines = [self._format_lot_line(lot) for lot in batch]
        for i in range(0, len(lines), 10):
            await self._say_to(chat_id, "\n".join(lines[i : i + 10]))
        # счётчик коллекций / дневная стата — только парсер-монитор
        if channel == "parser":
            for lot in batch:
                self.db.bump_collection_shown(lot.title or lot.model or "")
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
        ins, upd, total = self.db.upsert_users(found, cap=creds.AFK_USER_CAP)
        if ins:
            try:
                self.db.bump_daily(users_new=ins)
            except Exception:  # noqa: BLE001
                pass
        return ins, upd, total

    def _ingest_always(self, lots: list[Lot] | None = None) -> None:
        """Всегда копить gifts+users в БД — даже если в выдачу не попали."""
        batch = list(lots or [])
        if batch:
            try:
                self._save_models(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ingest gifts: %s", exc)
            try:
                self.db.upsert_users_from_lots(
                    [lot for lot in batch if lot.seller_id is not None or lot.seller],
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

    async def ensure_db_farm(self) -> None:
        """Тихий непрерывный скан юзов+моделей в БД (без кнопки AFK)."""
        if not self.logged_in:
            return
        if self.running:
            # парсер занят — фарм после
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

    async def pause_db_farm(self) -> None:
        """Пауза фарма на время парсинга (клиент занят)."""
        self._afk_paused = True
        if not self.afk_running and self._afk_task is None:
            return
        self.afk_running = False
        if self._afk_task:
            self._afk_task.cancel()
            try:
                await self._afk_task
            except asyncio.CancelledError:
                pass
            self._afk_task = None

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
                if len(shown) < 3:
                    self._seen_titles.clear()
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
