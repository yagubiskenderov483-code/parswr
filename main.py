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
import re
import time
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
    BufferedInputFile,
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
from market import Lot, PrepareStats, TelegramMarket
from store import store

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

FRESH_OPTIONS = [(60, "1 мин"), (120, "2 мин")]
LEVEL_OPTIONS = [3, 4]  # жёстко ≤4, без lvl5
GIFTS_OPTIONS = [20, 40]  # без 60 — киты режем
RU_OPTIONS = [1, 2, 3]


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
        self._notify_times: list[float] = []
        self.session_users: list[str] = []
        self._last_titles: list[str] = []
        self._last_sellers: list[str] = []
        self._emit_gap = int(getattr(creds, "EMIT_GAP", 5))

    def _new_client(self) -> TelegramClient:
        return TelegramClient(StringSession(), creds.API_ID, creds.API_HASH)

    def apply_filters_to_market(self) -> None:
        # clamp к допустимым опциям
        if int(creds.MAX_ACCOUNT_LEVEL) > 4:
            creds.MAX_ACCOUNT_LEVEL = 4
        if int(creds.MAX_PROFILE_GIFTS) > 40:
            creds.MAX_PROFILE_GIFTS = 40
        if int(creds.FRESH_MAX_AGE_SEC) > 120:
            creds.FRESH_MAX_AGE_SEC = 120
        self.market.max_level = creds.MAX_ACCOUNT_LEVEL
        self.market.max_gifts = creds.MAX_PROFILE_GIFTS
        self.market.min_ru = creds.MIN_RU_SCORE
        self.market.fresh_age = float(creds.FRESH_MAX_AGE_SEC)
        self.market.fresh_rank = int(creds.FRESH_MAX_RANK)
        self.market.online_mode = str(creds.ONLINE_MODE)
        self.market.search_min = float(self.min_stars)
        self.market.search_max = float(self.max_stars)

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
        self.apply_filters_to_market()

    async def reset_auth(self) -> None:
        await self.stop_monitor()
        store.flush()
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
        self.apply_filters_to_market()
        wipe_disk_junk()

    def set_range(self, label: str, mn: int, mx: int) -> None:
        self.range_label = label
        self.min_stars = float(mn)
        self.max_stars = float(mx)
        self.apply_filters_to_market()

    async def start_monitor(self, chat_id: int) -> None:
        if not self.logged_in:
            raise RuntimeError("Сначала вход.")
        if self.running:
            await self.stop_monitor()
        self.chat_id = chat_id
        self.running = True
        self._seen.clear()
        self.lots_notified = 0
        self.checks = 0
        self.last_check_lots = 0
        self.last_error = ""
        self._status_msg_id = None
        self._notify_times.clear()
        self.session_users.clear()
        self._last_titles.clear()
        self._last_sellers.clear()
        self._emit_gap = int(getattr(creds, "EMIT_GAP", 5))
        self.apply_filters_to_market()
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
        store.flush()
        return f"⏹ Стоп. Чеков: {self.checks}. Новых: {self.lots_notified}"

    def _in_price(self, lot: Lot) -> bool:
        return self.min_stars <= lot.stars <= self.max_stars

    async def _throttle_notify(self) -> None:
        """Антифлуд: не больше NOTIFY_PER_MIN карточек в минуту."""
        now = time.monotonic()
        self._notify_times = [t for t in self._notify_times if now - t < 60.0]
        if len(self._notify_times) >= creds.NOTIFY_PER_MIN:
            wait = 60.0 - (now - self._notify_times[0]) + 0.05
            await asyncio.sleep(max(0.05, wait))
            now = time.monotonic()
            self._notify_times = [t for t in self._notify_times if now - t < 60.0]
        self._notify_times.append(time.monotonic())
        await asyncio.sleep(creds.NOTIFY_GAP)

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

    async def _loop(self) -> None:
        await self._say(
            f"⚡ Старт сразу · <b>{self.range_label}</b> · "
            f"floorΔ={self.market.floor_delta()}\n"
            f"Скрытые профили резолвлю · выдаю по мере нахождения…"
        )
        prep = PrepareStats()
        emitted = 0
        now = time.monotonic()
        lines_buf: list[str] = []
        try:
            async for lot in self.market.live_parse(
                self.min_stars,
                self.max_stars,
                parallel=creds.BURST_PARALLEL,
                per_collection=creds.BURST_PER_COLLECTION,
                max_collections=creds.BURST_MAX_COLLECTIONS,
                gap=creds.BURST_GAP,
                timeout=creds.API_TIMEOUT,
                limit_results=creds.RESULT_LIMIT,
                time_budget=creds.BURST_TIME_BUDGET,
                require_fresh=True,
                check_rank=True,
                stats=prep,
            ):
                self._seen.setdefault(lot.id, now)
                if not await self._notify_lot(lot, count_as_new=False):
                    continue
                emitted += 1
                self.lots_notified += 1
                lvl = lot.level if lot.level is not None else "?"
                gifts = lot.gifts_count if lot.gifts_count is not None else "?"
                floor = (
                    f"{lot.floor_stars:.0f}" if lot.floor_stars is not None else "?"
                )
                lines_buf.append(
                    f'🔍 <a href="{lot.nft_url}">NFT</a> | @{lot.seller} | '
                    f"{_fmt(lot.stars)}⭐ · fl{floor} · lvl{lvl} · g{gifts}"
                )
                if len(lines_buf) >= 8:
                    await self._say("\n".join(lines_buf))
                    lines_buf.clear()
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            await self._say(f"⚠️ Ошибка поиска: {_esc(str(exc)[:200])}")

        if lines_buf:
            await self._say("\n".join(lines_buf))
        await self._say(
            f"✅ Выдал: <b>{emitted}</b> / сырьё {prep.input}\n"
            f"фильтр: paid−{prep.paid_skip} · fresh−{prep.fresh_skip} · "
            f"price−{prep.price_skip} · lvl−{prep.level_skip} · "
            f"gifts−{prep.gifts_skip} · ru−{prep.ru_skip} · "
            f"online−{prep.online_skip} · ban−{prep.black_skip}"
        )
        self.checks = self.market.check_no

        await self._say(
            "📡 Мониторю новые. Скрытые → ищу owner. Floor-фильтр активен.",
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
                fresh = []
                for lot in result.lots:
                    if lot.id in self._seen:
                        continue
                    self._seen[lot.id] = now
                    if self._in_price(lot):
                        fresh.append(lot)

                prep = PrepareStats()
                writable_n = 0
                if fresh:
                    async for lot in self.market.stream_prepared(
                        fresh,
                        require_fresh=True,
                        check_rank=True,
                        stats=prep,
                    ):
                        if await self._notify_lot(lot, count_as_new=True):
                            writable_n += 1
                            self.lots_notified += 1

                filter_note = ""
                if fresh:
                    filter_note = (
                        f" · paid{prep.paid_skip}"
                        f" price{prep.price_skip}"
                        f" fresh{prep.fresh_skip}"
                        f" lvl{prep.level_skip}"
                        f" gifts{prep.gifts_skip}"
                        f" ru{prep.ru_skip}"
                        f" on{prep.online_skip}"
                        f" ban{prep.black_skip}"
                    )

                await self._edit_status(
                    f"💓 <b>Чек #{self.checks}</b>\n"
                    f"Режим: <b>{self.range_label}</b> · "
                    f"floorΔ=<b>{self.market.floor_delta()}</b>\n"
                    f"Обошёл: <b>{result.scanned}</b>/{result.collections_total}\n"
                    f"Лотов: <b>{len(result.lots)}</b>\n"
                    f"Новых: <b>{writable_n}</b>{filter_note}\n"
                    f"Всего: <b>{self.lots_notified}</b> · Seen <b>{len(self._seen)}</b>\n"
                    f"ok/err/flood: {result.ok}/{result.errors}/{result.floods}\n"
                    f"⏱ {result.elapsed:.2f}с · online={creds.ONLINE_MODE}"
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

    def _too_similar_recent(self, lot: Lot) -> bool:
        """True если такой же гифт или владелец был в последних EMIT_GAP выдачах."""
        gap = max(3, int(self._emit_gap))
        title = (lot.title or "").strip().lower()
        seller = (lot.seller or "").strip().lower()
        recent_t = self._last_titles[-gap:]
        recent_s = self._last_sellers[-gap:]
        if title and (title in recent_t or (recent_t and title == recent_t[-1])):
            return True
        if seller and (seller in recent_s or (recent_s and seller == recent_s[-1])):
            return True
        return False

    async def _notify_lot(self, lot: Lot, count_as_new: bool) -> bool:
        if not self.bot or not self.chat_id:
            return False
        # только реальный @username — никаких «скрыт»
        if lot.paid_dm or not lot.writable or not lot.seller:
            return False
        if not self._in_price(lot):
            return False
        if self._too_similar_recent(lot):
            logger.info(
                "skip consecutive title=%s seller=%s", lot.title, lot.seller
            )
            return False
        await self._throttle_notify()
        title = "🆕 <b>НОВЫЙ лот</b>" if count_as_new else "🎁 <b>Лот</b>"
        lvl = lot.level if lot.level is not None else "?"
        gifts = lot.gifts_count if lot.gifts_count is not None else "?"
        floor = f"{lot.floor_stars:.0f}" if lot.floor_stars is not None else "?"
        online = "🟢" if lot.online else ("🟡" if lot.recently else "⚪")
        text = (
            f"{title}\n\n"
            f"🎁 <b>{_esc(lot.display)}</b>\n"
            f"💰 <b>{_fmt(lot.stars)} ⭐</b> · floor <b>{floor}</b>\n"
            f"👤 @{lot.seller} {online} · lvl <b>{lvl}</b> · "
            f"gifts <b>{gifts}</b> · RU {lot.ru_score}\n"
            f'🖼 <a href="{lot.nft_url}">{lot.nft_url}</a>'
        )
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="🖼 NFT", url=lot.nft_url),
                InlineKeyboardButton(
                    text="✍️ Написать", url=f"https://t.me/{lot.seller}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 В ЧС",
                    callback_data=f"ban:{lot.seller[:40]}",
                )
            ],
        ]
        try:
            await self.bot.send_message(
                self.chat_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=False),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            title_key = (lot.title or "").strip().lower()
            seller_key = (lot.seller or "").strip().lower()
            if title_key:
                self._last_titles.append(title_key)
            if seller_key:
                self._last_sellers.append(seller_key)
            keep = max(20, self._emit_gap * 4)
            if len(self._last_titles) > keep:
                self._last_titles = self._last_titles[-keep:]
            if len(self._last_sellers) > keep:
                self._last_sellers = self._last_sellers[-keep:]
            store.add_found(
                lot.seller,
                {
                    "lvl": lot.level,
                    "gifts": lot.gifts_count,
                    "stars": lot.stars,
                    "slug": lot.slug,
                },
            )
            if lot.seller not in self.session_users:
                self.session_users.append(lot.seller)
            if creds.AUTO_BLACKLIST:
                store.block(lot.seller, lot.seller_id, reason="shown")
            store.save_users()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("notify: %s", exc)
            return False


app = App()


def main_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Парсинг", callback_data="menu:parse")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
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
    stop = "⏹ Стоп" if app.running else "▶️ Выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Цена поиска", callback_data="menu:search")],
            [
                InlineKeyboardButton(
                    text="⚙️ Settings · lvl / online / RU",
                    callback_data="menu:filters",
                )
            ],
            [InlineKeyboardButton(text="📤 Экспорт юзов", callback_data="menu:export")],
            [InlineKeyboardButton(text=stop, callback_data="menu:stop")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
        ]
    )


def filters_inline() -> InlineKeyboardMarkup:
    online_label = {
        "any": "в сети: любой",
        "recent": "в сети: recent",
        "online": "в сети: online",
    }.get(str(creds.ONLINE_MODE), f"в сети: {creds.ONLINE_MODE}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⏱ Свежесть {int(creds.FRESH_MAX_AGE_SEC)}с",
                    callback_data="flt:fresh",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"lvl ≤ {creds.MAX_ACCOUNT_LEVEL}",
                    callback_data="flt:level",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"gifts ≤ {creds.MAX_PROFILE_GIFTS}",
                    callback_data="flt:gifts",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"RU ≥ {creds.MIN_RU_SCORE}",
                    callback_data="flt:ru",
                )
            ],
            [
                InlineKeyboardButton(
                    text=online_label,
                    callback_data="flt:online",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:settings")],
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


def _cycle(options: list, current):
    try:
        idx = options.index(current)
    except ValueError:
        return options[0]
    return options[(idx + 1) % len(options)]


ONLINE_OPTIONS = ["any", "recent", "online"]


def _settings_hub_text() -> str:
    return (
        "⚙️ <b>Настройки</b>\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Свежесть: ≤<b>{int(creds.FRESH_MAX_AGE_SEC)}с</b>\n"
        f"lvl≤{creds.MAX_ACCOUNT_LEVEL} · gifts≤{creds.MAX_PROFILE_GIFTS} · "
        f"RU≥{creds.MIN_RU_SCORE}\n"
        f"Чеков: <b>{app.checks}</b> · новых: <b>{app.lots_notified}</b>\n"
        f"Юзов в сессии: <b>{len(app.session_users)}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>"
    )


def _filters_text() -> str:
    delta = app.market.floor_delta() if app.logged_in else "?"
    return (
        "⚙️ <b>Settings · фильтры</b>\n"
        f"Свежесть: ≤ <b>{int(creds.FRESH_MAX_AGE_SEC)}с</b>\n"
        f"lvl ≤ <b>{creds.MAX_ACCOUNT_LEVEL}</b>\n"
        f"gifts ≤ <b>{creds.MAX_PROFILE_GIFTS}</b>\n"
        f"RU ≥ <b>{creds.MIN_RU_SCORE}</b>\n"
        f"В сети: <b>{creds.ONLINE_MODE}</b>\n"
        f"Floor Δ режима: <b>{delta}</b>\n"
        f"Антифлуд: <b>{creds.NOTIFY_PER_MIN}</b>/мин\n"
        "Жми кнопку — цикл значений."
    )

async def _send_menu(target: Message | CallbackQuery, prefix: str = "") -> None:
    text = prefix + (
        "💡 <b>Выберите режим поиска:</b>\n\n"
        f"Акк: <b>{app.account_name}</b>\n"
        f"Сейчас: <b>{app.range_label}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>"
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


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not app.logged_in:
        await message.answer("Сначала /start и вход.")
        return
    await message.answer(_filters_text(), reply_markup=filters_inline())


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    text = await app.stop_monitor()
    await message.answer(text, reply_markup=main_inline() if app.logged_in else None)


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await app.reset_auth()
    await state.set_state(AuthStates.phone)
    await message.answer("Сброшено. Номер:")


@router.message(Command("ban"))
async def cmd_ban(message: Message, state: FSMContext) -> None:
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Юз: /ban username")
        return
    username = parts[1].strip().lstrip("@")
    store.block(username, reason="manual")
    await message.answer(f"🚫 В ЧС @{username}")


@router.message(Command("export"))
async def cmd_export(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = store.export_usernames() or "\n".join(f"@{u}" for u in app.session_users)
    if not text:
        await message.answer("Пока пусто")
        return
    await message.answer_document(
        BufferedInputFile(text.encode("utf-8"), filename="usernames.txt")
    )


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
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    try:
        result = await app.confirm_code(text)
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
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    try:
        await app.confirm_password(text)
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
        "🌲 <b>2k–5k</b> — лёгкий\n"
        "⚡️ <b>5k–15k</b>\n"
        "🔻 <b>15k–30k</b>\n"
        "💎 <b>30k–60k</b>\n"
        "👑 <b>60k–100k</b>",
        reply_markup=prices_inline("price"),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        _settings_hub_text(),
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:filters")
async def cb_filters(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_filters_text(), reply_markup=filters_inline())
    await callback.answer()


@router.callback_query(F.data.startswith("flt:"))
async def cb_filter_cycle(callback: CallbackQuery) -> None:
    kind = (callback.data or "").split(":", 1)[-1]
    if kind == "fresh":
        ages = [a for a, _ in FRESH_OPTIONS]
        creds.FRESH_MAX_AGE_SEC = _cycle(ages, int(creds.FRESH_MAX_AGE_SEC))
    elif kind == "level":
        creds.MAX_ACCOUNT_LEVEL = _cycle(LEVEL_OPTIONS, int(creds.MAX_ACCOUNT_LEVEL))
    elif kind == "gifts":
        creds.MAX_PROFILE_GIFTS = _cycle(GIFTS_OPTIONS, int(creds.MAX_PROFILE_GIFTS))
    elif kind == "ru":
        creds.MIN_RU_SCORE = _cycle(RU_OPTIONS, int(creds.MIN_RU_SCORE))
    elif kind == "online":
        creds.ONLINE_MODE = _cycle(ONLINE_OPTIONS, str(creds.ONLINE_MODE))
    app.apply_filters_to_market()
    await callback.message.edit_text(_filters_text(), reply_markup=filters_inline())
    await callback.answer("Ок")


@router.callback_query(F.data == "menu:export")
async def cb_export(callback: CallbackQuery) -> None:
    text = store.export_usernames()
    if not text:
        session = "\n".join(f"@{u}" for u in app.session_users)
        text = session
    if not text:
        await callback.answer("Пока пусто", show_alert=True)
        return
    data = text.encode("utf-8")
    await callback.message.answer_document(
        BufferedInputFile(data, filename="usernames.txt"),
        caption=f"Юзов: {text.count(chr(10)) + 1}",
    )
    # коротким списком тоже
    preview = text.splitlines()[:40]
    await callback.message.answer(
        "📤 <code>" + _esc(" ".join(preview)) + "</code>"
        + ("…" if len(text.splitlines()) > 40 else "")
    )
    await callback.answer("Экспорт")


@router.callback_query(F.data.startswith("ban:"))
async def cb_ban(callback: CallbackQuery) -> None:
    username = (callback.data or "").split(":", 1)[-1].lstrip("@")
    if not username:
        await callback.answer("?", show_alert=True)
        return
    store.block(username, reason="manual")
    await callback.answer(f"В ЧС @{username}", show_alert=True)


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
    await callback.message.edit_text(text, reply_markup=settings_inline())
    await callback.answer("Стоп")


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📊 <b>Статус</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Свежесть: ≤<b>{int(creds.FRESH_MAX_AGE_SEC)}с</b>\n"
        f"Чеков: <b>{app.checks}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
        f"Юзов: <b>{len(app.session_users)}</b>\n"
        f"Seen: <b>{len(app._seen)}</b>\n"
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
        f"▶️ Старт · <b>{label}</b>\nСначала быстрый поиск…",
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
            BotCommand(command="export", description="Экспорт юзов"),
            BotCommand(command="ban", description="В ЧС: /ban user"),
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
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
