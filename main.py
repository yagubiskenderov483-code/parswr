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
import shutil
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
        # 1) Быстрый выброс как FreeGiftsParser
        await self._say(f"⚡ Ищу свежие лоты · <b>{self.range_label}</b>…")
        try:
            burst = await self.market.burst_search(
                self.min_stars,
                self.max_stars,
                parallel=creds.BURST_PARALLEL,
                per_collection=creds.BURST_PER_COLLECTION,
                max_collections=creds.BURST_MAX_COLLECTIONS,
                gap=creds.BURST_GAP,
                timeout=creds.API_TIMEOUT,
                limit_results=creds.RESULT_LIMIT,
                time_budget=creds.BURST_TIME_BUDGET,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            await self._say(f"⚠️ Ошибка поиска: {_esc(str(exc)[:200])}")
            burst = None

        now = time.monotonic()
        if burst and burst.lots:
            await self.market.resolve_owners(burst.lots, timeout=creds.OWNER_TIMEOUT)
            # не выдаём юзы тех, кому в ЛС только за Stars
            writable = await self.market.filter_paid_dms(
                burst.lots, timeout=creds.PAID_DM_TIMEOUT
            )
            writable = writable[: creds.RESULT_LIMIT]
            skipped_paid = sum(1 for l in burst.lots if l.paid_dm)
            await self._say(
                f"🔍 Найдено подарков: <b>{len(writable)}</b> шт. "
                f"(~{burst.elapsed:.1f}с · коллекции {burst.scanned}/{burst.collections_total})"
                + (f"\n🚫 без платных ЛС: −{skipped_paid}" if skipped_paid else "")
            )
            lines = []
            for lot in writable[: creds.PREVIEW_COUNT]:
                self._seen[lot.id] = now
                lines.append(
                    f'🔍 <a href="{lot.nft_url}">NFT</a> | @{lot.seller} | '
                    f"{_fmt(lot.stars)}⭐"
                )
            for i in range(0, len(lines), 10):
                await self._say("\n".join(lines[i : i + 10]))

            for lot in writable[:5]:
                await self._notify_lot(lot, count_as_new=False)
        else:
            err = (burst.error if burst else self.last_error) or "пусто"
            samples = ""
            if burst and burst.price_samples:
                samples = "\nпримеры цен: " + ", ".join(burst.price_samples[:5])
            await self._say(
                f"Пока в диапазоне ничего не нашёл за {getattr(burst, 'elapsed', 0):.1f}с.\n"
                f"({_esc(err)}){samples}\nЖду новые…"
            )

        if burst:
            for lot in burst.lots:
                self._seen.setdefault(lot.id, now)
            self.checks = burst.check_no

        await self._say(
            "📡 Мониторю новые выставления.\nЧек ~каждую секунду.",
            reply_markup=main_inline(),
        )

        # 2) Чеки раз в секунду
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

                if fresh:
                    await self.market.resolve_owners(fresh, timeout=creds.OWNER_TIMEOUT)
                    writable = await self.market.filter_paid_dms(
                        fresh, timeout=creds.PAID_DM_TIMEOUT
                    )
                    paid_skip = sum(1 for l in fresh if l.paid_dm)
                    for lot in writable:
                        self.lots_notified += 1
                        await self._notify_lot(lot, count_as_new=True)
                else:
                    writable = []
                    paid_skip = 0

                # статус чека (edit одного сообщения)
                await self._edit_status(
                    f"💓 <b>Чек #{self.checks}</b>\n"
                    f"Режим: <b>{self.range_label}</b>\n"
                    f"Обошёл коллекций: <b>{result.scanned}</b>/"
                    f"{result.collections_total}\n"
                    f"Лотов в ответе: <b>{len(result.lots)}</b>\n"
                    f"Новых за чек: <b>{len(writable)}</b>"
                    + (f" · 🚫paid {paid_skip}" if paid_skip else "")
                    + "\n"
                    f"Всего новых: <b>{self.lots_notified}</b>\n"
                    f"Seen: <b>{len(self._seen)}</b>\n"
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
        # юзы платных ЛС не показываем
        if lot.paid_dm or not lot.seller:
            return
        seller = f"@{lot.seller}"
        title = "🆕 <b>НОВЫЙ лот</b>" if count_as_new else "🎁 <b>Лот</b>"
        text = (
            f"{title}\n\n"
            f"🎁 <b>{_esc(lot.display)}</b>\n"
            f"💰 <b>{_fmt(lot.stars)} ⭐</b>\n"
            f"👤 {seller}\n"
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
            [InlineKeyboardButton(text=stop, callback_data="menu:stop")],
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
        "⚙️ <b>Настройки</b>\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Чеков: <b>{app.checks}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>",
        reply_markup=settings_inline(),
    )
    await callback.answer()


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
        f"Чеков: <b>{app.checks}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
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
            BotCommand(command="stop", description="Стоп"),
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
