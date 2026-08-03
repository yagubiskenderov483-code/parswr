"""
Telegram Market bot · Stars.

Цены: 2–5k / 5–15k / 15–30k / 30–60k / 60–100k
Inline-кнопки. Стабильный парсер свежих лотов.
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
from market import FRESH_WINDOW_SEC, Lot, TelegramMarket

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
        self.market = TelegramMarket(self.client, parallel=creds.PARALLEL)
        self.bot: Bot | None = None
        self.chat_id: int | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self._seen: dict[str, float] = {}
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.min_stars = 2000.0
        self.max_stars = 5000.0
        self.range_label = "2k–5k ⭐"
        self.logged_in = False
        self.account_name = ""
        self.lots_notified = 0

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
            raise ValueError(f"Слишком много попыток. Подожди {exc.seconds} сек.") from exc
        self.phone = phone
        self.phone_code_hash = result.phone_code_hash
        self.logged_in = False
        return "Код отправлен в Telegram / SMS."

    async def confirm_code(self, code: str) -> str:
        if not self.phone or not self.phone_code_hash:
            raise ValueError("Сначала отправь номер.")
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
            raise ValueError("Код истёк. /start заново.") from exc
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
        self.market = TelegramMarket(self.client, parallel=creds.PARALLEL)
        wipe_disk_junk()

    def set_range(self, label: str, min_s: int, max_s: int) -> None:
        self.range_label = label
        self.min_stars = float(min_s)
        self.max_stars = float(max_s)

    async def start_monitor(self, chat_id: int) -> str:
        if not self.logged_in:
            raise RuntimeError("Сначала авторизация.")
        if self.running:
            await self.stop_monitor()
        self.chat_id = chat_id
        self.running = True
        self._seen.clear()
        self.lots_notified = 0
        self._task = asyncio.create_task(self._loop(), name="market-loop")
        return (
            f"▶️ Парсинг <b>{self.range_label}</b>\n"
            f"{int(self.min_stars)}–{int(self.max_stars)} ⭐\n"
            "Ищу недавно выставленные гифты…"
        )

    async def stop_monitor(self) -> str:
        if not self.running and self._task is None:
            return "⏹ Уже выключен."
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        return f"⏹ Стоп. Новых лотов: {self.lots_notified}"

    def _in_price(self, lot: Lot) -> bool:
        return self.min_stars <= lot.stars <= self.max_stars

    def _purge_old(self) -> None:
        now = time.monotonic()
        for k in [k for k, ts in self._seen.items() if now - ts > FRESH_WINDOW_SEC]:
            del self._seen[k]

    async def _say(self, text: str, reply_markup=None) -> None:
        if not self.bot or not self.chat_id:
            return
        try:
            await self.bot.send_message(
                self.chat_id, text, reply_markup=reply_markup
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("send failed: %s", exc)

    async def _loop(self) -> None:
        primed = False
        ticks = 0
        while self.running:
            started = time.monotonic()
            try:
                await self.market.ensure_connected()

                if not primed:
                    await self._say("⏳ Сканирую маркет (спокойно, без бана)…")
                    lots = await self.market.seed_market(
                        per_collection=creds.PER_COLLECTION
                    )
                    now = time.monotonic()
                    for lot in lots:
                        self._seen[lot.id] = now
                    in_range = sum(1 for lot in lots if self._in_price(lot))
                    primed = True
                    st = self.market.last_stats
                    await self._say(
                        "✅ Готово. Рынок запомнил.\n"
                        f"Лотов Stars: <b>{len(lots)}</b>\n"
                        f"В диапазоне {self.range_label}: <b>{in_range}</b>\n"
                        f"ok/empty/err/flood: {st.get('ok', 0)}/{st.get('empty', 0)}/"
                        f"{st.get('errors', 0)}/{st.get('floods', 0)}\n\n"
                        "Дальше кидаю только <b>новые</b> выставления.",
                        reply_markup=main_inline(),
                    )
                else:
                    self._purge_old()
                    lots = await self.market.poll_batch(
                        per_collection=creds.PER_COLLECTION
                    )
                    now = time.monotonic()
                    fresh = []
                    for lot in lots:
                        if lot.id in self._seen:
                            continue
                        self._seen[lot.id] = now
                        if self._in_price(lot):
                            fresh.append(lot)

                    for lot in fresh:
                        try:
                            await self.market.resolve_owner(
                                lot, timeout=creds.OWNER_TIMEOUT
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        self.lots_notified += 1
                        logger.info(
                            "NEW %.0f⭐ @%s %s",
                            lot.stars,
                            lot.seller or "—",
                            lot.display,
                        )
                        await self._notify(lot)

                    if ticks > 0 and ticks % creds.HEARTBEAT_EVERY == 0:
                        await self._say(
                            f"💓 Жив · seen={len(self._seen)} · новых={self.lots_notified} · "
                            f"{self.range_label}",
                            reply_markup=main_inline(),
                        )

                ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("loop error: %s", exc)
                await self._say(f"⚠️ Сбой парсера, продолжаю…\n<code>{_esc(str(exc)[:180])}</code>")
                await asyncio.sleep(2.0)
                try:
                    await self.market.ensure_connected()
                except Exception:  # noqa: BLE001
                    pass

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.15, creds.POLL_INTERVAL - elapsed))

    async def _notify(self, lot: Lot) -> None:
        if not self.bot or not self.chat_id:
            return
        seller = f"@{lot.seller}" if lot.seller else "скрыт / нет юза"
        text = (
            "🆕 <b>НОВЫЙ лот</b>\n\n"
            f"🎁 <b>{_esc(lot.display)}</b>\n"
            f"💰 <b>{_fmt(lot.stars)} ⭐</b>\n"
            f"👤 {seller}\n"
            f'🖼 <a href="{lot.nft_url}">{lot.nft_url}</a>'
        )
        rows = [[InlineKeyboardButton(text="🖼 NFT", url=lot.nft_url)]]
        if lot.seller and re.fullmatch(r"[A-Za-z0-9_]{4,64}", lot.seller):
            rows.append(
                [InlineKeyboardButton(text="✍️ Написать", url=f"https://t.me/{lot.seller}")]
            )
        try:
            await self.bot.send_message(
                self.chat_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
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
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{rid}")]
        for rid, label, _, _ in PRICE_RANGES
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_inline() -> InlineKeyboardMarkup:
    stop = "⏹ Стоп" if app.running else "▶️ Парсинг выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Цена поиска", callback_data="menu:search")],
            [InlineKeyboardButton(text=stop, callback_data="menu:stop")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:home")],
        ]
    )


def _normalize_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("8") and len(phone) == 11:
        phone = "+7" + phone[1:]
    if not phone.startswith("+"):
        phone = "+" + phone
    if not re.fullmatch(r"\+\d{10,15}", phone):
        raise ValueError("Номер должен быть в формате +79991234567")
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
    old = root / "bot"
    if old.is_dir():
        shutil.rmtree(old, ignore_errors=True)


def _range_by_id(rid: str) -> tuple[str, int, int] | None:
    for i, label, mn, mx in PRICE_RANGES:
        if i == rid:
            return label, mn, mx
    return None


async def _send_menu(target: Message | CallbackQuery, prefix: str = "") -> None:
    text = prefix + (
        f"✅ Акк: <b>{app.account_name}</b>\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n\n"
        "Выбери:"
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
        "🎁 <b>Telegram Market</b>\n\n"
        "Вход: номер → код\n"
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
    await message.answer("Сброшено. Пришли номер:")


@router.message(StateFilter(AuthStates.phone))
async def got_phone(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    try:
        reply = await app.send_code(text)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    await state.set_state(AuthStates.code)
    await message.answer(f"{reply}\nПришли код:")


@router.message(StateFilter(AuthStates.code))
async def got_code(message: Message, state: FSMContext) -> None:
    try:
        result = await app.confirm_code(message.text or "")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    if result == "NEED_PASSWORD":
        await state.set_state(AuthStates.password)
        await message.answer("🔒 2FA пароль:")
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
        "🔍 <b>Парсинг</b>\nВыбери цену — ищу свежие гифты в диапазоне:",
        reply_markup=prices_inline("price"),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n"
        f"Акк: <b>{app.account_name}</b>\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>",
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:search")
async def cb_search(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🔎 <b>Цена поиска</b>\nВыбери диапазон:",
        reply_markup=prices_inline("search"),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:stop")
async def cb_stop(callback: CallbackQuery) -> None:
    text = await app.stop_monitor()
    await callback.message.edit_text(f"{text}\n\n⚙️", reply_markup=settings_inline())
    await callback.answer("Стоп")


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery) -> None:
    st = app.market.last_stats
    await callback.message.edit_text(
        "📊 <b>Статус</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Цена: <b>{app.range_label}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
        f"Seen: <b>{len(app._seen)}</b>\n"
        f"ok/err/flood: {st.get('ok', 0)}/{st.get('errors', 0)}/{st.get('floods', 0)}",
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
    try:
        status = await app.start_monitor(callback.from_user.id)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(status, reply_markup=main_inline())
    await callback.answer("Старт")


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
        status = await app.start_monitor(callback.from_user.id)
        text = f"🔎 {label}\n{status}"
    else:
        text = f"🔎 Цена: <b>{label}</b>\nЗапуск — «Парсинг»."
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
            BotCommand(command="start", description="Меню / вход"),
            BotCommand(command="stop", description="Стоп"),
            BotCommand(command="logout", description="Сбросить акк"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Ready | stable parser | 5 price ranges")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
