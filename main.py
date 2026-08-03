"""
Telegram Market bot · Stars.

Как FreeGiftsParser по UX:
- выбор режима цены inline
- сразу выдача моделей/NFT с юзами
- дальше чеки раз в секунду

Settings фильтруют выдачу (online / lvl / gifts / RU / анти-реклама).
Сам market-парсер не режем.
"""

from __future__ import annotations

import asyncio
import logging
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
from market import Lot, TelegramMarket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")
router = Router()

PRICE_RANGES: list[tuple[str, str, int, int]] = [
    ("r2_5", "2k–5k ⭐", 2000, 5000),
    ("r5_15", "5k–15k ⭐", 5000, 15000),
    ("r15_30", "15k–30k ⭐", 15000, 30000),
    ("r30_60", "30k–60k ⭐", 30000, 60000),
    ("r60_100", "60k–100k ⭐", 60000, 100000),
]

LEVEL_PRESETS: list[int | None] = [None, 3, 5, 10, 20, 50]
GIFTS_PRESETS: list[int | None] = [None, 3, 5, 10, 25, 50]

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
# Реклама / раздачи в bio — скипаем
_AD_BIO_RE = re.compile(
    r"("
    r"дарю\s*гифт|дарю\s*gift|дарю\s*подар|раздач|"
    r"бесплатн|free\s*gift|giveaway|акци[яи]|"
    r"пиши\s*в\s*лс|в\s*л[сc]\s*|реклам|"
    r"продам\s*гифт|купл[юу]\s*гифт|взаимн|"
    r"nft\s*drop|airdrop|крипт|казино|ставки|"
    r"100%\s*profit|заработок|инвест"
    r")",
    re.IGNORECASE,
)


@dataclass
class FilterSettings:
    require_username: bool = True
    russian_only: bool = True
    online_only: bool = False
    skip_ad_bio: bool = True
    max_level: int | None = None
    max_gifts: int | None = None
    unique_owners: bool = True
    diversify_models: bool = True

    def needs_full_profile(self) -> bool:
        return bool(
            self.skip_ad_bio
            or self.max_level is not None
            or self.max_gifts is not None
        )


class AuthStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


class App:
    def __init__(self) -> None:
        Path("data").mkdir(exist_ok=True)
        self.client = TelegramClient(StringSession(), creds.API_ID, creds.API_HASH)
        self.market = TelegramMarket(self.client)
        self.bot: Bot | None = None
        self.chat_id: int | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self._status_msg_id: int | None = None
        self._seen: dict[str, float] = {}
        self._seen_owners: set[str] = set()
        self._recent_collections: list[str] = []
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.min_stars = 2000.0
        self.max_stars = 5000.0
        self.range_label = "2k–5k ⭐"
        self.logged_in = False
        self.account_name = ""
        self.lots_notified = 0
        self.checks = 0
        self.last_check_lots = 0
        self.last_error = ""
        self.filters = FilterSettings()

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
        self.range_label = label
        self.min_stars = float(mn)
        self.max_stars = float(mx)

    async def start_monitor(self, chat_id: int) -> None:
        if not self.logged_in:
            raise RuntimeError("Сначала вход.")
        if self.running:
            await self.stop_monitor()
        self.chat_id = chat_id
        self.running = True
        self._seen.clear()
        self._seen_owners.clear()
        self._recent_collections.clear()
        self.lots_notified = 0
        self.checks = 0
        self.last_check_lots = 0
        self.last_error = ""
        self._status_msg_id = None
        self._task = asyncio.create_task(self._loop(), name="monitor")

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

    def _passes_settings(self, lot: Lot) -> bool:
        f = self.filters
        if f.require_username and not lot.seller:
            return False
        if f.russian_only and not _is_russian(lot):
            return False
        if f.online_only and lot.is_online is not True:
            return False
        if f.skip_ad_bio and _has_ad_bio(lot):
            return False
        if f.max_level is not None and lot.account_level is not None:
            if lot.account_level > f.max_level:
                return False
        if f.max_gifts is not None and lot.gifts_count is not None:
            if lot.gifts_count > f.max_gifts:
                return False
        return True

    def _diversity_key(self, lot: Lot) -> str:
        """Ключ разнообразия: коллекция + модель — чтобы подряд не шли одинаковые гифты."""
        title = (lot.title or "").strip().lower()
        model = (lot.model or "").strip().lower()
        return f"{title}|{model}" if model else title or lot.id

    def _pick_diverse(self, lots: list[Lot], limit: int | None = None) -> list[Lot]:
        """Владелец 1 раз + коллекции/модели вразнобой, без одинаковых подряд."""
        f = self.filters
        buckets: dict[str, list[Lot]] = {}
        keys: list[str] = []
        for lot in lots:
            if not self._passes_settings(lot):
                continue
            if f.unique_owners and lot.owner_key in self._seen_owners:
                continue
            mk = self._diversity_key(lot) if f.diversify_models else "_all"
            if mk not in buckets:
                buckets[mk] = []
                keys.append(mk)
            buckets[mk].append(lot)

        out: list[Lot] = []
        last = self._recent_collections[-1] if self._recent_collections else ""
        recent = set(self._recent_collections[-6:])

        while True:
            progressed = False
            # Сначала ключи, которых давно не было / не last
            ordered = sorted(
                keys,
                key=lambda k: (
                    k == last,
                    k in recent,
                    -len(buckets.get(k, [])),
                ),
            )
            for mk in ordered:
                bucket = buckets.get(mk) or []
                while bucket:
                    lot = bucket.pop(0)
                    if f.unique_owners and lot.owner_key in self._seen_owners:
                        continue
                    # жёстко: не ставим ту же коллекцию/модель подряд
                    if out and self._diversity_key(out[-1]) == mk:
                        continue
                    out.append(lot)
                    if f.unique_owners:
                        self._seen_owners.add(lot.owner_key)
                    last = mk
                    self._recent_collections.append(mk)
                    if len(self._recent_collections) > 40:
                        self._recent_collections = self._recent_collections[-40:]
                    recent = set(self._recent_collections[-6:])
                    progressed = True
                    break
                if progressed:
                    break
            if not progressed:
                # если остались только одинаковые — добираем, но всё равно через владельцев
                leftover = False
                for mk in keys:
                    bucket = buckets.get(mk) or []
                    while bucket:
                        lot = bucket.pop(0)
                        if f.unique_owners and lot.owner_key in self._seen_owners:
                            continue
                        out.append(lot)
                        if f.unique_owners:
                            self._seen_owners.add(lot.owner_key)
                        leftover = True
                        break
                    if leftover:
                        break
                if not leftover:
                    break
            if limit is not None and len(out) >= limit:
                break
        return out

    async def _say(self, text: str, reply_markup=None) -> Message | None:
        if not self.bot or not self.chat_id:
            return None
        try:
            return await self.bot.send_message(
                self.chat_id, text, reply_markup=reply_markup
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

    async def _stream_burst(self, lots: list[Lot]) -> list[Lot]:
        """Сразу кидаем разношерстные RU-модели; Settings (bio/lvl) — параллельно."""
        now = time.monotonic()
        await self.market.resolve_owners(lots, timeout=creds.OWNER_TIMEOUT)

        candidates: list[Lot] = []
        for lot in lots:
            self._seen[lot.id] = now
            if self.filters.require_username and not lot.seller:
                continue
            if self.filters.russian_only and not _is_russian(lot):
                continue
            candidates.append(lot)

        # Сразу разнообразие — одинаковые гифты подряд не летят
        quick = self._pick_diverse(candidates, limit=creds.PREVIEW_COUNT)

        enrich_task: asyncio.Task | None = None
        if self.filters.needs_full_profile() and quick:
            enrich_task = asyncio.create_task(
                self.market.enrich_profiles(
                    quick,
                    need_full=True,
                    timeout=creds.OWNER_TIMEOUT,
                    parallel=12,
                )
            )

        if quick:
            lines = []
            for lot in quick:
                user = f"@{lot.seller}"
                model = lot.model or lot.title
                lines.append(
                    f'🎁 <a href="{lot.nft_url}">{_esc(model)}</a> | {user} | '
                    f"{_fmt(lot.stars)}⭐"
                )
            await self._say(f"⚡️ Модели · <b>{len(lines)}</b> шт. (RU · вразнобой):")
            for i in range(0, len(lines), 10):
                await self._say("\n".join(lines[i : i + 10]))
        else:
            await self._say("⚡️ Нет RU-моделей с @username в этой пачке.")

        if enrich_task is not None:
            try:
                await enrich_task
            except Exception:  # noqa: BLE001
                logger.exception("enrich failed")

        # после bio/lvl — ещё раз отфильтровать (owners уже учтены)
        # не дублируем seen_owners: временно откатим ключи quick и пересоберём
        for lot in quick:
            self._seen_owners.discard(lot.owner_key)
        if self._recent_collections:
            # уберём ключи этой пачки из recent, пересоберём порядок
            keys = {self._diversity_key(lot) for lot in quick}
            self._recent_collections = [
                k for k in self._recent_collections if k not in keys
            ]
        return self._pick_diverse(quick, limit=creds.PREVIEW_COUNT)

    async def _loop(self) -> None:
        await self._say(f"⚡ Ищу модели · <b>{self.range_label}</b>…")
        try:
            burst = await self.market.burst_search(
                self.min_stars,
                self.max_stars,
                parallel=creds.BURST_PARALLEL,
                per_collection=creds.BURST_PER_COLLECTION,
                max_collections=creds.BURST_MAX_COLLECTIONS,
                gap=creds.BURST_GAP,
                timeout=creds.API_TIMEOUT,
                limit_results=max(creds.PREVIEW_COUNT, 40),
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            await self._say(f"⚠️ Ошибка поиска: {_esc(str(exc)[:200])}")
            burst = None

        shown: list[Lot] = []
        if burst and burst.lots:
            await self._say(
                f"🔍 Сырых лотов: <b>{len(burst.lots)}</b> "
                f"(~{burst.elapsed:.1f}с · {burst.scanned}/{burst.collections_total})"
            )
            shown = await self._stream_burst(burst.lots)
            if shown:
                await self._say(f"✅ После Settings: <b>{len(shown)}</b> карточек")
                for lot in shown[:8]:
                    await self._notify_lot(lot, count_as_new=False)
            else:
                await self._say("После Settings пусто — мониторю дальше…")
            self.checks = burst.check_no
        else:
            err = (burst.error if burst else self.last_error) or "пусто"
            await self._say(
                f"Пока пусто за {getattr(burst, 'elapsed', 0):.1f}с.\n"
                f"({_esc(err)})\nЖду новые…"
            )

        await self._say(
            "📡 Мониторю новые выставления.\nЧек ~каждую секунду.",
            reply_markup=main_inline(),
        )

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

                now = time.monotonic()
                candidates: list[Lot] = []
                for lot in result.lots:
                    if lot.id in self._seen:
                        continue
                    self._seen[lot.id] = now
                    if self._in_price(lot):
                        candidates.append(lot)

                fresh: list[Lot] = []
                if candidates:
                    await self.market.resolve_owners(
                        candidates, timeout=creds.OWNER_TIMEOUT
                    )
                    basic = [
                        lot
                        for lot in candidates
                        if (not self.filters.require_username or lot.seller)
                        and (not self.filters.russian_only or _is_russian(lot))
                    ]
                    if self.filters.needs_full_profile() and basic:
                        await self.market.enrich_profiles(
                            basic[:20],
                            need_full=True,
                            timeout=min(creds.OWNER_TIMEOUT, 0.7),
                            parallel=10,
                        )
                    fresh = self._pick_diverse(basic)

                for lot in fresh:
                    self.lots_notified += 1
                    await self._notify_lot(lot, count_as_new=True)

                await self._edit_status(
                    f"💓 <b>Чек #{self.checks}</b>\n"
                    f"Режим: <b>{self.range_label}</b>\n"
                    f"Коллекции: <b>{result.scanned}</b>/{result.collections_total}\n"
                    f"Лотов: <b>{len(result.lots)}</b> · новых: <b>{len(fresh)}</b>\n"
                    f"Всего новых: <b>{self.lots_notified}</b>\n"
                    f"Seen: <b>{len(self._seen)}</b> · owners: <b>{len(self._seen_owners)}</b>\n"
                    f"Settings: {_filters_short(self.filters)}\n"
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
        if not self.bot or not self.chat_id:
            return
        if not lot.seller:
            return
        seller = f"@{lot.seller}"
        title = "🆕 <b>НОВЫЙ лот</b>" if count_as_new else "🎁 <b>Лот</b>"
        meta = []
        if lot.model:
            meta.append(lot.model)
        if lot.is_online is True:
            meta.append("online")
        if lot.account_level is not None:
            meta.append(f"lvl {lot.account_level}")
        if lot.gifts_count is not None:
            meta.append(f"gifts {lot.gifts_count}")
        meta_line = f"\n📌 {_esc(', '.join(meta))}" if meta else ""
        text = (
            f"{title}\n\n"
            f"🎁 <b>{_esc(lot.display)}</b>\n"
            f"💰 <b>{_fmt(lot.stars)} ⭐</b>\n"
            f"👤 {seller}{meta_line}\n"
            f'🖼 <a href="{lot.nft_url}">{lot.nft_url}</a>'
        )
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🖼 NFT / LINK", url=lot.nft_url)]
        ]
        if re.fullmatch(r"[A-Za-z0-9_]{4,64}", lot.seller):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✍️ Написать", url=f"https://t.me/{lot.seller}"
                    )
                ]
            )
        try:
            await self.bot.send_message(
                self.chat_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=False),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("notify: %s", exc)


app = App()


def _is_russian(lot: Lot) -> bool:
    """Только русские: lang=ru или кириллица в имени. Остальных режем."""
    lang = (lot.lang_code or "").strip().lower()
    if lang.startswith("ru"):
        return True
    if lang and not lang.startswith("ru"):
        return False
    name = f"{lot.first_name} {lot.last_name}".strip()
    if name and _CYRILLIC_RE.search(name):
        return True
    return False


def _has_ad_bio(lot: Lot) -> bool:
    blob = " ".join(
        x
        for x in (lot.about, lot.first_name, lot.last_name)
        if x
    )
    if not blob:
        return False
    return bool(_AD_BIO_RE.search(blob))


def main_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Парсинг", callback_data="menu:parse")],
            [InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings")],
        ]
    )


def prices_inline(prefix: str = "price") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"✔️ {label}", callback_data=f"{prefix}:{rid}")]
        for rid, label, _, _ in PRICE_RANGES
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_inline() -> InlineKeyboardMarkup:
    f = app.filters
    online = "🟢 Онлайн: ВКЛ" if f.online_only else "⚪️ Онлайн: выкл"
    ru = "🇷🇺 RU: ВКЛ" if f.russian_only else "🌐 RU: выкл"
    ads = "🚫 Анти-реклама: ВКЛ" if f.skip_ad_bio else "📢 Анти-реклама: выкл"
    lvl = f"lvl ≤ {f.max_level}" if f.max_level is not None else "lvl: ∞"
    gifts = f"gifts ≤ {f.max_gifts}" if f.max_gifts is not None else "gifts: ∞"
    stop = "⏹ Стоп парсинг" if app.running else "▶️ Парсинг выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=online, callback_data="set:online")],
            [InlineKeyboardButton(text=ru, callback_data="set:ru")],
            [InlineKeyboardButton(text=ads, callback_data="set:ads")],
            [InlineKeyboardButton(text=f"📶 Max lvl · {lvl}", callback_data="set:lvl")],
            [
                InlineKeyboardButton(
                    text=f"🎁 Max gifts · {gifts}", callback_data="set:gifts"
                )
            ],
            [InlineKeyboardButton(text="🔎 Цена поиска", callback_data="menu:search")],
            [InlineKeyboardButton(text=stop, callback_data="menu:stop")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
        ]
    )


def level_inline() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for val in LEVEL_PRESETS:
        label = "∞" if val is None else str(val)
        mark = "•" if app.filters.max_level == val else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{label}",
                callback_data=f"lvl:{'none' if val is None else val}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gifts_inline() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for val in GIFTS_PRESETS:
        label = "∞" if val is None else str(val)
        mark = "•" if app.filters.max_gifts == val else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{label}",
                callback_data=f"gifts:{'none' if val is None else val}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _normalize_phone(phone: str) -> str:
    phone = (
        phone.strip()
        .replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )
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


def _filters_short(f: FilterSettings) -> str:
    parts = [
        "online" if f.online_only else "any",
        "RU" if f.russian_only else "all",
        "noAds" if f.skip_ad_bio else "adsOK",
        f"lvl≤{f.max_level}" if f.max_level is not None else "lvl∞",
        f"gifts≤{f.max_gifts}" if f.max_gifts is not None else "gifts∞",
    ]
    return " · ".join(parts)


def _settings_text() -> str:
    f = app.filters
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Онлайн: <b>{'только в сети' if f.online_only else 'любой'}</b>\n"
        f"Только RU: <b>{'да' if f.russian_only else 'нет'}</b>\n"
        f"Анти-реклама bio: <b>{'да' if f.skip_ad_bio else 'нет'}</b>\n"
        f"Max lvl: <b>{f.max_level if f.max_level is not None else '∞'}</b>\n"
        f"Max gifts: <b>{f.max_gifts if f.max_gifts is not None else '∞'}</b>\n"
        f"Без @юза не показываем · владелец 1 раз\n\n"
        f"Чеков: <b>{app.checks}</b> · новых: <b>{app.lots_notified}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>"
    )


def wipe_disk_junk() -> None:
    root = Path(__file__).resolve().parent
    data = root / "data"
    data.mkdir(exist_ok=True)
    for folder in (data, root):
        for pattern in ("*session*", "*.db", "*.db-*", "*.sqlite*"):
            for path in folder.glob(pattern):
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


async def _send_menu(target: Message | CallbackQuery, prefix: str = "") -> None:
    text = prefix + (
        "💡 <b>Выберите режим поиска:</b>\n\n"
        f"Акк: <b>{app.account_name}</b>\n"
        f"Сейчас: <b>{app.range_label}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Settings: {_filters_short(app.filters)}"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=main_inline())
        await target.answer()
    else:
        await target.answer(text, reply_markup=main_inline())


async def _show_settings(target: Message | CallbackQuery) -> None:
    text = _settings_text()
    markup = settings_inline()
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


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


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if not app.logged_in:
        await message.answer("Сначала /start и вход.")
        return
    await _show_settings(message)


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
        "💡 <b>Выберите режим поиска:</b>\n\n"
        "🌲 <b>2k–5k</b>\n"
        "⚡️ <b>5k–15k</b>\n"
        "🔻 <b>15k–30k</b>\n"
        "💎 <b>30k–60k</b>\n"
        "👑 <b>60k–100k</b>\n\n"
        "Сразу кинет модели, Settings режет выдачу.",
        reply_markup=prices_inline("price"),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    await _show_settings(callback)


@router.callback_query(F.data == "set:online")
async def cb_set_online(callback: CallbackQuery) -> None:
    app.filters.online_only = not app.filters.online_only
    await _show_settings(callback)


@router.callback_query(F.data == "set:ru")
async def cb_set_ru(callback: CallbackQuery) -> None:
    app.filters.russian_only = not app.filters.russian_only
    await _show_settings(callback)


@router.callback_query(F.data == "set:ads")
async def cb_set_ads(callback: CallbackQuery) -> None:
    app.filters.skip_ad_bio = not app.filters.skip_ad_bio
    await _show_settings(callback)


@router.callback_query(F.data == "set:lvl")
async def cb_set_lvl_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📶 <b>Макс. lvl аккаунта</b>\n∞ = без лимита.",
        reply_markup=level_inline(),
    )
    await callback.answer()


@router.callback_query(F.data == "set:gifts")
async def cb_set_gifts_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🎁 <b>Макс. гифтов у акка</b>\n∞ = без лимита.",
        reply_markup=gifts_inline(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lvl:"))
async def cb_lvl_value(callback: CallbackQuery) -> None:
    raw = (callback.data or "").split(":", 1)[-1]
    app.filters.max_level = None if raw == "none" else int(raw)
    await _show_settings(callback)


@router.callback_query(F.data.startswith("gifts:"))
async def cb_gifts_value(callback: CallbackQuery) -> None:
    raw = (callback.data or "").split(":", 1)[-1]
    app.filters.max_gifts = None if raw == "none" else int(raw)
    await _show_settings(callback)


@router.callback_query(F.data == "menu:search")
async def cb_search(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🔎 Цена поиска:",
        reply_markup=prices_inline("search"),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:stop")
async def cb_stop(callback: CallbackQuery) -> None:
    text = await app.stop_monitor()
    await callback.message.edit_text(
        text + "\n\n" + _settings_text(), reply_markup=settings_inline()
    )
    await callback.answer("Стоп")


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📊 <b>Статус</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Чеков: <b>{app.checks}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
        f"Seen: <b>{len(app._seen)}</b>\n"
        f"Owners: <b>{len(app._seen_owners)}</b>\n"
        f"Settings: {_filters_short(app.filters)}\n"
        f"Err: {_esc(app.last_error[:120]) if app.last_error else '—'}",
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("price:"))
async def cb_price_start(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _range_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    app.set_range(label, mn, mx)
    await callback.message.edit_text(
        f"▶️ Старт · <b>{label}</b>\nСразу кину модели…",
        reply_markup=main_inline(),
    )
    await callback.answer("Старт")
    try:
        await app.start_monitor(callback.from_user.id)
    except RuntimeError as exc:
        await callback.message.answer(f"⚠️ {exc}")


@router.callback_query(F.data.startswith("search:"))
async def cb_price_search(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _range_by_id(rid)
    if not chosen:
        await callback.answer("?", show_alert=True)
        return
    label, mn, mx = chosen
    app.set_range(label, mn, mx)
    if app.running:
        await app.start_monitor(callback.from_user.id)
        text = f"🔎 Переключил на <b>{label}</b>, перезапустил."
    else:
        text = f"🔎 Цена: <b>{label}</b>. Жми Парсинг."
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
            BotCommand(command="settings", description="Settings · фильтры"),
            BotCommand(command="stop", description="Стоп"),
            BotCommand(command="logout", description="Сброс акка"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Ready | fast parser + settings filters")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
