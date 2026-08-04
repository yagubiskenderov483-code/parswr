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

from aiogram import Bot, Dispatcher, F, Router
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
)
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
    no_premium: bool = False  # без TGP
    max_gifts: int = 5
    max_level: int = 5
    short_user_max: int = 8


class AuthStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


class App:
    def __init__(self) -> None:
        Path("data").mkdir(exist_ok=True)
        self.client = TelegramClient(StringSession(), creds.API_ID, creds.API_HASH)
        self.market = TelegramMarket(self.client)
        self.db = GiftDB()
        self.bot: Bot | None = None
        self.chat_id: int | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self._status_msg_id: int | None = None
        self._seen: dict[str, float] = {}
        self._seen_sellers: set[str] = set()
        self._seen_models: set[str] = set()
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
        self.afk_last_error = ""
        self._afk_status_msg_id: int | None = None
        self.afk_collections_total = 0
        self.afk_cursor = 0
        self.filters = SearchFilters()
        self.filter_search_running = False
        self._filter_task: asyncio.Task | None = None
        self.require_russian = True
        self._blocked_keys: set[str] = set()
        self._reload_persist_seen()

    def _reload_persist_seen(self) -> None:
        """Подтянуть блоклист + уже показанных — без повторов после рестарта."""
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
            self._blocked_keys = self.db.load_block_keys()
        except Exception:  # noqa: BLE001
            self._blocked_keys = set()

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

    async def reset_auth(self) -> None:
        await self.stop_monitor()
        await self.stop_afk()
        try:
            if self.client.is_connected():
                await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.phone = None
        self.phone_code_hash = None
        self.logged_in = False
        self.account_name = ""
        self.client = self._new_client()
        self.market = TelegramMarket(self.client)
        wipe_disk_junk()

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
        self.chat_id = chat_id
        self.running = True
        # лоты-seen сбрасываем, продавцов/модели — НЕТ (без повторов юзов)
        self._seen.clear()
        self._reload_persist_seen()
        self.lots_notified = 0
        self.checks = 0
        self.last_check_lots = 0
        self.last_error = ""
        self._status_msg_id = None
        self.market.reshuffle_collections()
        self._task = asyncio.create_task(self._loop(), name="monitor")

    def _flush_market_users(self) -> tuple[int, int, int]:
        """Слить юзеров из ответов маркета в БД."""
        found = self.market.drain_users()
        if not found:
            return 0, 0, self.db.count_users()
        return self.db.upsert_users(found, cap=creds.AFK_USER_CAP)

    def _is_blocked_lot(self, lot: Lot) -> bool:
        u = (lot.seller or "").lower()
        if u and (u in self._blocked_keys or f"u:{u}" in self._blocked_keys):
            return True
        if lot.seller_id is not None and f"id:{lot.seller_id}" in self._blocked_keys:
            return True
        return self.db.is_blocked(username=lot.seller, user_id=lot.seller_id)

    @staticmethod
    def _is_russian(lot: Lot) -> bool:
        """Простая RU-проверка: кириллица в имени / фамилии / био (опис).

        Ник (@username) почти всегда латиница — смотрим display name + about.
        Аву/историю подарков API так не отдаёт текстом.
        """
        parts = [lot.first_name or "", lot.last_name or "", lot.about or ""]
        blob = " ".join(p for p in parts if p).strip()
        if not blob:
            return False
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
            return "⏹ Уже стоп."
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        return f"⏹ Стоп. Чеков: {self.checks}. Новых: {self.lots_notified}"

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
        return bool(f.few_gifts or f.low_level or f.short_username or f.no_premium)

    def _passes_extra_filters(self, lot: Lot) -> bool:
        f = self.filters
        if f.few_gifts:
            if lot.gifts_count is None or lot.gifts_count > f.max_gifts:
                return False
        if f.low_level:
            if lot.account_level is None or lot.account_level > f.max_level:
                return False
        if f.short_username:
            n = len(lot.seller or "")
            if n < 6 or n > f.short_user_max:
                return False
        if f.no_premium:
            if lot.is_premium is not False:
                return False
        return True

    def _pick_clean(
        self,
        lots: list[Lot],
        *,
        limit: int | None = None,
        apply_extra: bool = False,
        track_seen: bool = True,
    ) -> list[Lot]:
        """Без рекламы/4–5 юзов/повторов; сильный рандом порядка."""
        lots = list(lots)
        random.shuffle(lots)
        buckets: dict[str, list[Lot]] = {}
        keys: list[str] = []
        local_sellers: set[str] = set()
        local_models: set[str] = set()

        for lot in lots:
            if not lot.seller:
                continue
            if self._bad_username_len(lot.seller):
                continue
            if self._is_blocked_lot(lot):
                continue
            if self._is_ad(lot):
                continue
            if self.require_russian and not self._is_russian(lot):
                continue
            if apply_extra and not self._passes_extra_filters(lot):
                continue
            # без повторов продавцов/моделей всегда
            if lot.owner_key in self._seen_sellers:
                continue
            if lot.model_key in self._seen_models:
                continue
            if track_seen and self.db.is_seen_seller(
                username=lot.seller, user_id=lot.seller_id
            ):
                continue
            if track_seen and self.db.is_seen_model(lot.model_key):
                continue
            if lot.owner_key in local_sellers:
                continue
            if lot.model_key in local_models:
                continue
            mk = lot.model_key
            if mk not in buckets:
                buckets[mk] = []
                keys.append(mk)
            buckets[mk].append(lot)
            local_sellers.add(lot.owner_key)
            local_models.add(lot.model_key)

        random.shuffle(keys)
        for mk in keys:
            random.shuffle(buckets[mk])

        out: list[Lot] = []
        last = ""
        while True:
            progressed = False
            ordered = list(keys)
            random.shuffle(ordered)
            # чуть приоритетнее не-last
            ordered.sort(key=lambda k: k == last)
            for mk in ordered:
                bucket = buckets.get(mk) or []
                while bucket:
                    lot = bucket.pop(0)
                    if track_seen and lot.owner_key in self._seen_sellers:
                        continue
                    if track_seen and lot.model_key in self._seen_models:
                        continue
                    if out and out[-1].model_key == mk:
                        continue
                    out.append(lot)
                    # сразу помечаем — повторов больше не будет
                    self._seen_sellers.add(lot.owner_key)
                    self._seen_models.add(lot.model_key)
                    self.db.mark_seen_seller(
                        username=lot.seller, user_id=lot.seller_id
                    )
                    self.db.mark_seen_model(
                        lot.model_key, title=lot.model or lot.title
                    )
                    last = mk
                    progressed = True
                    break
                if progressed:
                    break
            if not progressed:
                break
            if limit is not None and len(out) >= limit:
                break
        random.shuffle(out)
        # после shuffle снова разнесём одинаковые подряд если вдруг
        fixed: list[Lot] = []
        rest = list(out)
        while rest:
            pick_i = 0
            if fixed:
                for i, lot in enumerate(rest):
                    if lot.model_key != fixed[-1].model_key:
                        pick_i = i
                        break
            fixed.append(rest.pop(pick_i))
        return fixed[:limit] if limit is not None else fixed

    async def _prepare_show(
        self,
        lots: list[Lot],
        *,
        limit: int | None = None,
        apply_extra: bool = False,
        track_seen: bool = True,
        need_full: bool | None = None,
    ) -> list[Lot]:
        await self.market.resolve_owners(lots, timeout=creds.OWNER_TIMEOUT)
        self.db.upsert_users_from_lots(
            [lot for lot in lots if lot.seller_id is not None],
            cap=creds.AFK_USER_CAP,
        )
        # сохраним seller к моделям в БД
        self._save_models([lot for lot in lots if lot.seller or lot.seller_id])

        candidates = [
            lot
            for lot in lots
            if lot.seller and not self._bad_username_len(lot.seller)
        ]
        random.shuffle(candidates)
        # био/имя — RU/анти-реклама; меньше запросов = быстрее выдача
        if candidates:
            lim = limit or creds.SHOW_LIMIT
            sample_n = min(len(candidates), max(lim * 4, 50))
            if need_full or apply_extra:
                sample_n = min(len(candidates), max(sample_n, lim * 5))
            await self.market.enrich_profiles(
                candidates[:sample_n],
                timeout=min(creds.OWNER_TIMEOUT, 0.7),
                parallel=16,
            )
            self.db.upsert_users_from_lots(
                [lot for lot in candidates if lot.seller_id is not None],
                cap=creds.AFK_USER_CAP,
            )
            self._flush_market_users()
            # в пул на pick — только уже обогащённые (есть шанс пройти RU)
            candidates = candidates[:sample_n]
        return self._pick_clean(
            candidates,
            limit=limit or creds.SHOW_LIMIT,
            apply_extra=apply_extra,
            track_seen=track_seen,
        )

    async def run_filter_search(self, chat_id: int) -> None:
        """Отдельный поиск по фильтрам — НЕ связан с парсером/монитором."""
        if self.filter_search_running:
            await self._say_to(
                chat_id, "🔎 Фильтр-поиск уже идёт. Подожди…"
            )
            return
        self.filter_search_running = True
        f = self.filters
        mn, mx = self.filter_min_stars, self.filter_max_stars
        label = self.filter_range_label
        try:
            await self._say_to(
                chat_id,
                f"🔎 <b>Фильтр-поиск</b> (отдельно от парсера)\n"
                f"Сложность: <b>{label}</b>\n"
                f"{_filters_label(f)}\n"
                f"БД + лайв по рынку · парсер/чеки не трогаю",
            )
            old = self.db.fetch_random_lots(
                min_stars=mn,
                max_stars=mx,
                limit=creds.FILTER_DB_LIMIT,
                require_seller=False,
            )
            live: list[Lot] = []
            try:
                burst = await self.market.burst_search(
                    mn,
                    mx,
                    parallel=creds.FILTER_BURST_PARALLEL,
                    per_collection=creds.FILTER_BURST_PER_COLLECTION,
                    max_collections=creds.FILTER_BURST_MAX_COLLECTIONS,
                    gap=creds.FILTER_BURST_GAP,
                    timeout=creds.API_TIMEOUT,
                    limit_results=creds.FILTER_LIMIT,
                    bump_check=False,
                    touch_cursor=False,
                    time_budget=0,
                    early_show_at=creds.FILTER_EARLY_SHOW_AT,
                )
                if burst:
                    if burst.all_lots:
                        self._save_models(burst.all_lots)
                    self._flush_market_users()
                    live = list(burst.lots)
                    full = (
                        burst.scanned >= burst.collections_total
                        and burst.collections_total > 0
                    )
                    await self._say_to(
                        chat_id,
                        f"📡 Лайв: <b>{burst.scanned}</b>/"
                        f"<b>{burst.collections_total}</b>"
                        f"{' ✅' if full else ''} · "
                        f"в диапазоне {len(live)} · ~{burst.elapsed:.1f}с",
                    )
            except Exception as exc:  # noqa: BLE001
                await self._say_to(
                    chat_id, f"⚠️ Лайв-поиск: {_esc(str(exc)[:160])}"
                )

            merged = _dedupe_lots(old + live)
            random.shuffle(merged)
            await self._say_to(
                chat_id,
                f"📦 Кандидатов: <b>{len(merged)}</b> "
                f"(БД {len(old)} · лайв {len(live)})",
            )
            shown = await self._prepare_show(
                merged,
                limit=creds.SHOW_LIMIT,
                apply_extra=True,
                track_seen=True,
                need_full=True,
            )
            if not shown:
                await self._say_to(
                    chat_id,
                    "Пусто по фильтрам. Ослабь тумблеры или смени сложность.",
                    reply_markup=filters_inline(),
                )
                return
            await self._say_to(
                chat_id,
                f"✅ Найдено: <b>{len(shown)}</b>/{creds.SHOW_LIMIT} · "
                f"фильтр-поиск (не парсер) · <b>{label}</b>",
            )
            await self._say_lot_list_to(chat_id, shown)
            for lot in shown[: creds.NOTIFY_CARDS]:
                await self._notify_lot_to(chat_id, lot, count_as_new=False)
            await self._say_to(
                chat_id, "Готово · фильтр-поиск.", reply_markup=filters_inline()
            )
        finally:
            self.filter_search_running = False

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

    async def _say_lot_list(self, lots: list[Lot]) -> None:
        if self.chat_id:
            await self._say_lot_list_to(self.chat_id, lots)

    async def _say_lot_list_to(self, chat_id: int, lots: list[Lot]) -> None:
        lines = [self._format_lot_line(lot) for lot in lots[: creds.SHOW_LIMIT]]
        for i in range(0, len(lines), 10):
            await self._say_to(chat_id, "\n".join(lines[i : i + 10]))

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
        """Сохраняет модели в БД."""
        if not lots:
            return 0, 0
        inserted, updated = self.db.upsert_models(lots)
        self.db_last_saved = inserted + updated
        self.db_total = self.db.count()
        return inserted, updated

    async def start_afk(self, chat_id: int) -> str:
        if not self.logged_in:
            raise RuntimeError("Сначала вход.")
        if self.afk_running:
            return (
                f"🌙 AFK уже крутится.\n"
                f"Юзов: <b>{self.db.count_users():,}</b> / {creds.AFK_USER_CAP:,}\n"
                f"Коллекций: <b>{self.db.count_collections()}</b>/"
                f"{self.afk_collections_total or '?'}"
            )
        self.chat_id = chat_id
        self.afk_running = True
        self.afk_pages = 0
        self.afk_users_added = 0
        self.afk_last_error = ""
        self._afk_status_msg_id = None
        self._afk_task = asyncio.create_task(self._afk_loop(), name="afk")
        return (
            f"🌙 AFK старт · коплю юзов до <b>{creds.AFK_USER_CAP:,}</b>\n"
            f"Сейчас в БД: <b>{self.db.count_users():,}</b> юзов · "
            f"<b>{self.db.count()}</b> моделей"
        )

    async def stop_afk(self) -> str:
        if not self.afk_running and self._afk_task is None:
            return "🌙 AFK уже стоп."
        self.afk_running = False
        if self._afk_task:
            self._afk_task.cancel()
            try:
                await self._afk_task
            except asyncio.CancelledError:
                pass
            self._afk_task = None
        return (
            f"🌙 AFK стоп.\n"
            f"Юзов в БД: <b>{self.db.count_users():,}</b>\n"
            f"Коллекций: <b>{self.db.count_collections()}</b>\n"
            f"Страниц: <b>{self.afk_pages}</b> · +юзов за сессию: "
            f"<b>{self.afk_users_added}</b>"
        )

    async def _edit_afk(self, text: str) -> None:
        if not self.bot or not self.chat_id:
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
        """Фарм по всем коллекциям с пагинацией, пока юзов < 5M."""
        try:
            gift_ids = await self.market.load_collections()
        except Exception as exc:  # noqa: BLE001
            self.afk_last_error = str(exc)
            await self._say(f"🌙 AFK ошибка коллекций: {_esc(str(exc)[:180])}")
            self.afk_running = False
            return

        self.market.reshuffle_collections()
        gift_ids = list(self.market._gift_ids or gift_ids)
        self.afk_collections_total = len(gift_ids)
        self.afk_cursor = random.randrange(len(gift_ids)) if gift_ids else 0
        # зарегистрируем все ~149 коллекций
        for gid in gift_ids:
            self.db.touch_collection(gid, title="", last_offset=self.db.get_collection_offset(gid))

        await self._say(
            f"🌙 AFK: коллекций <b>{len(gift_ids)}</b> · "
            f"цель <b>{creds.AFK_USER_CAP:,}</b> юзов\n"
            f"Сейчас: <b>{self.db.count_users():,}</b> · старт random"
        )

        last_status = 0.0
        n = len(gift_ids)
        if n == 0:
            await self._say("🌙 Нет коллекций.")
            self.afk_running = False
            return

        while self.afk_running:
            users_now = self.db.count_users()
            if users_now >= creds.AFK_USER_CAP:
                await self._say(
                    f"🌙 AFK готов · набрано <b>{users_now:,}</b> юзов "
                    f"(лимит {creds.AFK_USER_CAP:,})"
                )
                self.afk_running = False
                break

            gid = gift_ids[self.afk_cursor % n]
            self.afk_cursor = (self.afk_cursor + 1) % n
            offset = self.db.get_collection_offset(gid)

            try:
                lots, users, next_offset, total = await self.market.afk_fetch_page(
                    gid,
                    offset=offset,
                    limit=creds.AFK_PAGE_LIMIT,
                    gap=creds.AFK_GAP,
                    timeout=creds.API_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.afk_last_error = str(exc)
                await asyncio.sleep(0.5)
                continue

            title = lots[0].title if lots else ""
            if lots:
                self._save_models(lots)
            # юзы из ответа API + продавцы лотов
            batch_users = list(users)
            for lot in lots:
                if lot.seller_id is not None:
                    batch_users.append(
                        {
                            "user_id": lot.seller_id,
                            "username": lot.seller,
                            "first_name": lot.first_name,
                            "last_name": lot.last_name,
                        }
                    )
            # юзы из страницы + всё что market накопил в drain
            drain = self.market.drain_users()
            if drain:
                batch_users.extend(drain)
            ins_u, _upd_u, total_u = self.db.upsert_users(
                batch_users, cap=creds.AFK_USER_CAP
            )
            self.afk_users_added += ins_u
            self.afk_pages += 1
            if self.afk_pages % 5 == 0:
                self.db.checkpoint()

            # если страница пустая / нет next — с начала коллекции
            new_offset = next_offset if (lots and next_offset) else ""
            self.db.touch_collection(
                gid,
                title=title,
                last_offset=new_offset,
                pages_inc=1,
                lots_inc=len(lots),
            )

            now = time.monotonic()
            if now - last_status >= creds.AFK_STATUS_EVERY:
                last_status = now
                await self._edit_afk(
                    f"🌙 <b>AFK фарм</b>\n"
                    f"Коллекций: <b>{self.db.count_collections()}</b>/"
                    f"<b>{self.afk_collections_total}</b>\n"
                    f"Юзов: <b>{total_u:,}</b> / <b>{creds.AFK_USER_CAP:,}</b>\n"
                    f"+ за сессию: <b>{self.afk_users_added:,}</b>\n"
                    f"Моделей в БД: <b>{self.db.count():,}</b>\n"
                    f"Страниц: <b>{self.afk_pages}</b>\n"
                    f"Сейчас gift_id=<code>{gid}</code> · listed≈{total}\n"
                    f"offset: <code>{_esc((offset or '∅')[:40])}</code>"
                    + (
                        f"\n⚠️ {_esc(self.afk_last_error[:100])}"
                        if self.afk_last_error
                        else ""
                    )
                )

            await asyncio.sleep(0.01)

        self._afk_task = None

    async def _loop(self) -> None:
        # 1) Скан 149/149, но выдача СРАЗУ как набрали пул (не ждать конца)
        await self._say(
            f"⚡ Ищу свежие лоты · <b>{self.range_label}</b>\n"
            f"Коллекции: <b>149/149</b> · первая выдача сразу, как наберётся"
        )
        last_prog = 0.0
        early_shown = False

        async def _burst_progress(done: int, total: int, lots_n: int) -> None:
            nonlocal last_prog
            nowp = time.monotonic()
            if nowp - last_prog < 1.8 and done < total:
                return
            last_prog = nowp
            await self._edit_status(
                f"⚡ Скан: <b>{done}/{total}</b>"
                f"{' · выдача уже ушла' if early_shown else ' · жду пул…'}\n"
                f"Лотов сырых: <b>{lots_n}</b> · <b>{self.range_label}</b>"
            )

        async def _on_early(matched: list[Lot], done: int, total: int) -> None:
            nonlocal early_shown
            if early_shown or not self.running:
                return
            early_shown = True
            await self._say(
                f"⚡ Быстрая выдача · скан <b>{done}/{total}</b> ещё идёт в фоне…"
            )
            # сохранить что уже есть
            self._save_models(matched)
            self._flush_market_users()
            pool = [lot for lot in matched if self._in_price(lot)]
            random.shuffle(pool)
            shown = await self._prepare_show(
                pool, limit=creds.SHOW_LIMIT, apply_extra=False
            )
            await self._say(
                f"🔍 К выдаче: <b>{len(shown)}</b>/{creds.SHOW_LIMIT} "
                f"(быстрый режим · RU · без повторов)"
            )
            if shown:
                now_e = time.monotonic()
                for lot in shown:
                    self._seen[lot.id] = now_e
                await self._say_lot_list(shown)
                for lot in shown[: creds.NOTIFY_CARDS]:
                    await self._notify_lot(lot, count_as_new=False)
            else:
                await self._say("Пока пусто по фильтрам — добьём круг 149…")

        self.market._progress_cb = _burst_progress
        try:
            burst = await self.market.burst_search(
                self.min_stars,
                self.max_stars,
                parallel=creds.BURST_PARALLEL,
                per_collection=creds.BURST_PER_COLLECTION,
                max_collections=creds.BURST_MAX_COLLECTIONS,
                gap=creds.BURST_GAP,
                timeout=creds.API_TIMEOUT,
                limit_results=creds.SHOW_LIMIT,
                bump_check=True,
                touch_cursor=True,
                time_budget=0,
                early_show_at=creds.BURST_EARLY_SHOW_AT,
                on_early_lots=_on_early,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            await self._say(f"⚠️ Ошибка поиска: {_esc(str(exc)[:200])}")
            burst = None
        finally:
            self.market._progress_cb = None

        now = time.monotonic()
        if burst and (burst.all_lots or burst.lots):
            to_save = burst.all_lots or burst.lots
            for lot in to_save:
                self._seen.setdefault(lot.id, now)
            ins, upd = self._save_models(to_save)
            ins_u, upd_u, total_u = self._flush_market_users()
            self.db.upsert_users_from_lots(
                [lot for lot in to_save if lot.seller_id is not None],
                cap=creds.AFK_USER_CAP,
            )
            uniq_models = self.db.count_models()
            full = (
                burst.scanned >= burst.collections_total
                and burst.collections_total > 0
            )
            await self._say(
                f"💾 Круг готов · моделей: <b>{len(to_save)}</b> (+{ins}/{upd})\n"
                f"👤 Юзов +{ins_u} / upd {upd_u} · всего <b>{total_u:,}</b>\n"
                f"🗄 В БД: <b>{self.db_total}</b> · уник. моделей: <b>{uniq_models}</b>\n"
                f"Коллекции: <b>{burst.scanned}/{burst.collections_total}</b>"
                f"{' ✅' if full else ''}\n"
                f"~{burst.elapsed:.1f}с"
            )

            # если ранняя выдача не сработала — покажем сейчас
            if not early_shown:
                price_pool = [
                    lot
                    for lot in (burst.all_lots or burst.lots)
                    if self._in_price(lot)
                ]
                random.shuffle(price_pool)
                shown = await self._prepare_show(
                    price_pool or list(burst.lots),
                    limit=creds.SHOW_LIMIT,
                    apply_extra=False,
                )
                await self._say(
                    f"🔍 К выдаче: <b>{len(shown)}</b>/{creds.SHOW_LIMIT} "
                    f"(кириллица в имени/био · без повторов)"
                )
                if shown:
                    for lot in shown:
                        self._seen[lot.id] = now
                    await self._say_lot_list(shown)
                    for lot in shown[: creds.NOTIFY_CARDS]:
                        await self._notify_lot(lot, count_as_new=False)
                else:
                    await self._say(
                        "После фильтров пусто — мониторю дальше…"
                    )
        else:
            err = (burst.error if burst else self.last_error) or "пусто"
            await self._say(
                f"Пока в диапазоне ничего не нашёл за {getattr(burst, 'elapsed', 0):.1f}с.\n"
                f"({_esc(err)})\nЖду новые…"
            )

        if burst:
            self.checks = burst.check_no

        await self._say(
            "📡 Мониторю · чек = <b>149/149</b> (ускоренный).",
            reply_markup=main_inline(),
        )

        # 2) Чеки — полный круг коллекций каждый раз
        while self.running:
            started = time.monotonic()
            try:
                result = await self.market.run_check(
                    parallel=creds.CHECK_PARALLEL,
                    per_collection=creds.CHECK_PER_COLLECTION,
                    batch_size=creds.CHECK_BATCH,
                    gap=creds.CHECK_GAP,
                    timeout=creds.API_TIMEOUT,
                )
                self.checks = result.check_no
                self.last_check_lots = len(result.lots)
                self.last_error = result.error

                if result.lots:
                    self._save_models(result.lots)
                ins_u, _upd_u, total_u = self._flush_market_users()
                if result.lots:
                    self.db.upsert_users_from_lots(
                        [lot for lot in result.lots if lot.seller_id is not None],
                        cap=creds.AFK_USER_CAP,
                    )
                    total_u = self.db.count_users()

                now = time.monotonic()
                candidates = []
                for lot in result.lots:
                    if lot.id in self._seen:
                        continue
                    self._seen[lot.id] = now
                    if self._in_price(lot):
                        candidates.append(lot)

                fresh: list[Lot] = []
                if candidates:
                    fresh = await self._prepare_show(
                        candidates,
                        limit=creds.SHOW_LIMIT,
                        apply_extra=False,
                    )
                    if fresh:
                        await self._say_lot_list(fresh[: creds.SHOW_LIMIT])
                    for lot in fresh[: creds.NOTIFY_CARDS]:
                        self.lots_notified += 1
                        await self._notify_lot(lot, count_as_new=True)
                    self.lots_notified += max(0, len(fresh) - creds.NOTIFY_CARDS)

                full = (
                    result.scanned >= result.collections_total
                    and result.collections_total > 0
                )
                if self.checks % 3 == 0:
                    self.db.checkpoint()
                await self._edit_status(
                    f"💓 <b>Чек #{self.checks}</b>\n"
                    f"Режим: <b>{self.range_label}</b>\n"
                    f"Обошёл коллекций: <b>{result.scanned}</b>/"
                    f"<b>{result.collections_total}</b>"
                    f"{' ✅ все' if full else ''}\n"
                    f"Лотов в ответе: <b>{len(result.lots)}</b>\n"
                    f"Новых за чек: <b>{len(fresh)}</b>\n"
                    f"Всего новых: <b>{self.lots_notified}</b>\n"
                    f"Seen: <b>{len(self._seen)}</b> · "
                    f"sellers: <b>{len(self._seen_sellers)}</b> · "
                    f"models: <b>{len(self._seen_models)}</b>\n"
                    f"👤 Юзов в БД: <b>{total_u:,}</b> (+{ins_u})\n"
                    f"🗄 Моделей в БД: <b>{self.db_total}</b> "
                    f"(уник. {self.db.count_models()}, last {self.db_last_saved})\n"
                    f"🚫 Блок: <b>{self.db.count_blocked()}</b>\n"
                    f"ok/err/flood: {result.ok}/{result.errors}/{result.floods}\n"
                    f"⏱ {result.elapsed:.2f}с"
                    + (f"\n⚠️ {_esc(result.error[:120])}" if result.error else "")
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.exception("check failed")
                await self._edit_status(
                    f"💓 Чек ошибка\n<code>{_esc(str(exc)[:180])}</code>\nПродолжаю…"
                )

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.05, creds.CHECK_INTERVAL - elapsed))

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
    parts.append(
        f"юз 6–{f.short_user_max}" if f.short_username else "юз:any (кроме 4–5)"
    )
    parts.append("no TGP" if f.no_premium else "TGP:any")
    return " · ".join(parts)


def main_inline() -> InlineKeyboardMarkup:
    afk = "🌙 AFK стоп" if app.afk_running else "🌙 AFK фарм юзов"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Парсинг", callback_data="menu:parse")],
            [InlineKeyboardButton(text="🧩 Фильтры", callback_data="menu:filters")],
            [InlineKeyboardButton(text=afk, callback_data="menu:afk")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
        ]
    )


def difficulty_inline(prefix: str = "diff") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{rid}")]
        for rid, label, _, _ in DIFFICULTIES
    ]
    rows.append(
        [InlineKeyboardButton(text="🧩 Сначала фильтры", callback_data="menu:filters")]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
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
            [InlineKeyboardButton(text="🎯 Сложность фильтр-поиска:", callback_data="fdiff:noop")],
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
                    text=("✅" if f.short_username else "⬜️") + " Короткий юз (6–8)",
                    callback_data="flt:short",
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
                    text="🔎 Искать (БД+лайв)", callback_data="flt:run"
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
        "🧩 <b>Поиск по фильтрам</b>\n"
        "<i>Отдельно от парсера — чеки/seen не трогает</i>\n\n"
        f"Сложность поиска: <b>{app.filter_range_label}</b>\n"
        f"{_filters_label(app.filters)}\n\n"
        "🟢 лёгкий · 🟡 средний · 🔴 сложный · 💀 impossible\n"
        "• Мало гифтов — gifts ≤ 5\n"
        "• Мелкий lvl — rating ≤ 5\n"
        "• Короткий юз — длина 6–8 (4–5 всегда бан)\n"
        "• Без TGP — не Premium\n\n"
        "Искать = старые из БД + свежие с маркета"
    )


def settings_inline() -> InlineKeyboardMarkup:
    stop = "⏹ Стоп парсинг" if app.running else "▶️ Парсинг выкл"
    afk = "🌙 AFK стоп" if app.afk_running else "🌙 AFK старт"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Сложность парсера", callback_data="menu:parse")],
            [InlineKeyboardButton(text="🧩 Фильтр-поиск", callback_data="menu:filters")],
            [InlineKeyboardButton(text=stop, callback_data="menu:stop")],
            [InlineKeyboardButton(text=afk, callback_data="menu:afk")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
        ]
    )


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
    text = prefix + (
        "💡 <b>Меню</b>\n\n"
        f"Акк: <b>{app.account_name}</b>\n"
        f"Парсер: <b>{app.range_label}</b>\n"
        f"Фильтр-поиск: <b>{app.filter_range_label}</b>\n"
        f"Фильтры: {_filters_label(app.filters)}\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b> · "
        f"AFK: <b>{'🌙' if app.afk_running else '⏹'}</b>\n"
        f"🚫 4–5 значные юзы скрыты · фильтры ≠ парсер"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=main_inline())
        await target.answer()
    else:
        await target.answer(text, reply_markup=main_inline())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(".", reply_markup=ReplyKeyboardRemove())
    if app.logged_in:
        await _send_menu(message)
        return
    wipe_disk_junk()
    await state.set_state(AuthStates.phone)
    await message.answer(
        "🎁 <b>Gifts parser</b>\nВход нужен для маркета Telegram.\n"
        "📱 <code>+79991234567</code>"
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    text = await app.stop_monitor()
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
    await _send_menu(message, "Вход ок.\n")


@router.message(StateFilter(AuthStates.password))
async def got_password(message: Message, state: FSMContext) -> None:
    try:
        await app.confirm_password(message.text or "")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await _send_menu(message, "Вход ок.\n")


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
        "🎯 <b>Выбери сложность:</b>\n\n"
        "🟢 <b>Лёгкий</b> — 2k–5k\n"
        "🟡 <b>Средний</b> — 5k–15k\n"
        "🔴 <b>Сложный</b> — 15k–30k\n"
        "💀 <b>Impossible</b> — 30k–60k\n\n"
        f"Фильтры сейчас: {_filters_label(app.filters)}\n"
        "Выдача random · без 4–5 значных юзов",
        reply_markup=difficulty_inline("diff"),
    )
    await callback.answer()


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
    elif key == "tgp":
        app.filters.no_premium = not app.filters.no_premium
    elif key == "run":
        await callback.answer("Ищу…")
        await callback.message.edit_text(
            f"🔎 Фильтр-поиск · <b>{app.filter_range_label}</b>\n"
            f"{_filters_label(app.filters)}\n"
            f"Парсер не затрагивается.",
            reply_markup=main_inline(),
        )
        await app.run_filter_search(callback.from_user.id)
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
        "⚙️ <b>Настройки</b>\n"
        f"Парсер: <b>{app.range_label}</b>\n"
        f"Фильтр-поиск: <b>{app.filter_range_label}</b>\n"
        f"Фильтры: {_filters_label(app.filters)}\n"
        f"Чеков: <b>{app.checks}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"AFK: <b>{'🌙' if app.afk_running else '⏹'}</b>",
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:stop")
async def cb_stop(callback: CallbackQuery) -> None:
    text = await app.stop_monitor()
    await callback.message.edit_text(text, reply_markup=settings_inline())
    await callback.answer("Стоп")


@router.callback_query(F.data == "menu:afk")
async def cb_afk(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    if app.afk_running:
        text = await app.stop_afk()
        await callback.message.edit_text(text, reply_markup=main_inline())
        await callback.answer("AFK стоп")
        return
    try:
        text = await app.start_afk(callback.from_user.id)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(text, reply_markup=main_inline())
    await callback.answer("AFK старт")


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📊 <b>Статус</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"AFK: <b>{'🌙' if app.afk_running else '⏹'}</b>\n"
        f"Фильтр-поиск: <b>{'🔎' if app.filter_search_running else '⏹'}</b>\n"
        f"Парсер: <b>{app.range_label}</b>\n"
        f"Фильтр-поиск цена: <b>{app.filter_range_label}</b>\n"
        f"Чеков: <b>{app.checks}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
        f"Seen: <b>{len(app._seen)}</b>\n"
        f"🗄 Моделей: <b>{app.db.count()}</b> "
        f"(уник. {app.db.count_models()})\n"
        f"👤 Юзов: <b>{app.db.count_users():,}</b> / {creds.AFK_USER_CAP:,}\n"
        f"🎁 Коллекций: <b>{app.db.count_collections()}</b>"
        f"/{app.afk_collections_total or len(app.market._gift_ids) or '?'}\n"
        f"Err: {_esc(app.last_error[:120]) if app.last_error else '—'}",
        reply_markup=settings_inline(),
    )
    await callback.answer()


async def _start_with_range(
    callback: CallbackQuery, label: str, mn: int, mx: int
) -> None:
    app.set_range(label, mn, mx)
    extra = ""
    if app._filters_active():
        extra = f"\nФильтры: {_filters_label(app.filters)}"
    await callback.message.edit_text(
        f"▶️ Старт · <b>{label}</b>{extra}\n"
        f"Сначала быстрый поиск…\n"
        f"🚫 4–5 значные юзы скрыты · выдача random",
        reply_markup=main_inline(),
    )
    await callback.answer("Старт")
    try:
        await app.start_monitor(callback.from_user.id)
    except RuntimeError as exc:
        await callback.message.answer(f"⚠️ {exc}")


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
async def cb_diff_start(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _diff_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    await _start_with_range(callback, label, mn, mx)


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
    await _start_with_range(callback, label, mn, mx)


@router.callback_query(F.data.startswith("search:"))
async def cb_price_search(callback: CallbackQuery) -> None:
    """Legacy: цена только для парсера (фильтр-поиск — через fdiff)."""
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _range_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    app.set_range(label, mn, mx)
    if app.running:
        await app.start_monitor(callback.from_user.id)
        text = f"▶️ Парсер: <b>{label}</b>, перезапустил."
    else:
        text = f"▶️ Парсер: <b>{label}</b>. Жми Парсинг."
    await callback.message.edit_text(text, reply_markup=main_inline())
    await callback.answer("Ок")


async def main() -> None:
    wipe_disk_junk()
    bot = Bot(
        token=creds.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    app.bot = bot
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Меню"),
            BotCommand(command="stop", description="Стоп"),
            BotCommand(command="block", description="Блок @user"),
            BotCommand(command="unblock", description="Разблок @user"),
            BotCommand(command="blocked", description="Список блоклиста"),
            BotCommand(command="logout", description="Сброс акка"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Ready | burst search + per-second checks")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        await app.stop_afk()
        if app.client.is_connected():
            await app.client.disconnect()
        app.db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
