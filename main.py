"""
Telegram Market bot · Stars.

Inline-кнопки под сообщениями (не reply-клавиатура).
Парсинг быстрый, юзы резолвятся даже если гифт скрыт.
В настройках — смена диапазона поиска (цены), не смена акка.
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

# id, label, min, max
PRICE_RANGES: list[tuple[str, str, int, int]] = [
    ("r2_5", "2k–5k", 2000, 5000),
    ("r5_10", "5k–10k", 5000, 10000),
    ("r5_15", "5k–15k", 5000, 15000),
    ("r10_20", "10k–20k", 10000, 20000),
    ("r15_30", "15k–30k", 15000, 30000),
    ("r30_65", "30k–65k", 30000, 65000),
    ("r65_100", "65k–100k", 65000, 100000),
    ("r2_100", "Все 2k–100k", 2000, 100000),
]


class AuthStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


class App:
    def __init__(self) -> None:
        Path("data").mkdir(exist_ok=True)
        self.client = TelegramClient(StringSession(), creds.API_ID, creds.API_HASH)
        self.market = TelegramMarket(self.client, concurrency=creds.CONCURRENCY)
        self.bot: Bot | None = None
        self.owner_id: int | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self._seen: dict[str, float] = {}
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.min_stars = float(creds.MIN_STARS)
        self.max_stars = float(creds.MAX_STARS)
        self.range_label = "Все 2k–100k"
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
            raise ValueError("Код истёк. Нажми /start заново.") from exc
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
        self.market = TelegramMarket(self.client, concurrency=creds.CONCURRENCY)
        wipe_disk_junk()

    def set_range(self, label: str, min_s: int, max_s: int) -> None:
        self.range_label = label
        self.min_stars = float(min_s)
        self.max_stars = float(max_s)

    async def start_monitor(self, user_id: int) -> str:
        if not self.logged_in:
            raise RuntimeError("Сначала авторизация.")
        if self.running:
            await self.stop_monitor()
        self.owner_id = user_id
        self.running = True
        self._seen.clear()
        self.lots_notified = 0
        self._task = asyncio.create_task(self._loop(), name="market-loop")
        return (
            f"▶️ Парсинг · <b>{self.range_label}</b>\n"
            f"{int(self.min_stars)}–{int(self.max_stars)} ⭐\n"
            "Быстрый скан → только <b>НОВЫЕ</b> лоты."
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
        return f"⏹ Стоп. Новых: {self.lots_notified}"

    def _in_price(self, lot: Lot) -> bool:
        return self.min_stars <= lot.stars <= self.max_stars

    def _purge_old_seen(self) -> None:
        now = time.monotonic()
        for k in [k for k, ts in self._seen.items() if now - ts > FRESH_WINDOW_SEC]:
            del self._seen[k]

    async def _enrich(self, lot: Lot) -> None:
        try:
            await self.market.resolve_owner(lot, timeout=creds.OWNER_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass

    async def _loop(self) -> None:
        primed = False
        ticks = 0
        while self.running:
            started = time.monotonic()
            try:
                if not primed:
                    if self.owner_id and self.bot:
                        await self.bot.send_message(
                            self.owner_id,
                            "⏳ Быстрый скан маркета…",
                            reply_markup=main_inline(),
                        )
                    # Seed ASAP via overlapping waves, then one full pass
                    now = time.monotonic()
                    seeded = 0
                    in_range_n = 0
                    for _ in range(3):
                        async for chunk in self.market.iter_wave(
                            per_collection=creds.PER_COLLECTION,
                            batch_size=creds.WAVE_BATCH,
                        ):
                            for lot in chunk:
                                if lot.id not in self._seen:
                                    self._seen[lot.id] = now
                                    seeded += 1
                                    if self._in_price(lot):
                                        in_range_n += 1

                    # Finish remaining collections once
                    lots = await self.market.fetch_newest(
                        per_collection=creds.PER_COLLECTION
                    )
                    for lot in lots:
                        if lot.id not in self._seen:
                            self._seen[lot.id] = now
                            seeded += 1
                            if self._in_price(lot):
                                in_range_n += 1
                    primed = True
                    stats = self.market.last_stats
                    if self.owner_id and self.bot:
                        await self.bot.send_message(
                            self.owner_id,
                            "✅ Рынок зафиксирован\n"
                            f"Seen: <b>{len(self._seen)}</b> · в диапазоне ≈ <b>{in_range_n}</b>\n"
                            f"API ok/err: {stats.get('ok', 0)}/{stats.get('errors', 0)}\n"
                            "Дальше только <b>новые</b> лоты.",
                            reply_markup=main_inline(),
                        )
                    logger.info("primed seen=%s", len(self._seen))
                else:
                    self._purge_old_seen()
                    fresh: list[Lot] = []
                    async for chunk in self.market.iter_wave(
                        per_collection=creds.PER_COLLECTION,
                        batch_size=creds.WAVE_BATCH,
                    ):
                        if not self.running:
                            break
                        now = time.monotonic()
                        for lot in chunk:
                            if lot.id in self._seen:
                                continue
                            self._seen[lot.id] = now
                            if self._in_price(lot):
                                fresh.append(lot)

                    if fresh:
                        await asyncio.gather(
                            *[self._enrich(lot) for lot in fresh]
                        )
                        for lot in fresh:
                            self.lots_notified += 1
                            logger.info(
                                "NEW %.0f⭐ @%s %s",
                                lot.stars,
                                lot.seller or "—",
                                lot.display,
                            )
                            await self._notify(lot)

                    if ticks % 20 == 0:
                        logger.info(
                            "wave#%s fresh=%s seen=%s",
                            ticks,
                            len(fresh),
                            len(self._seen),
                        )
                ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("poll failed: %s", exc)

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.01, creds.POLL_INTERVAL - elapsed))

    async def _notify(self, lot: Lot) -> None:
        if not self.bot or not self.owner_id:
            return
        seller = f"@{lot.seller}" if lot.seller else "скрыт / нет юза"
        text = (
            "🆕 <b>НОВЫЙ лот</b>\n\n"
            f"🎁 <b>{_esc(lot.display)}</b>\n"
            f"💰 <b>{_fmt(lot.stars)} ⭐</b>\n"
            f"📈 {lot.category}\n"
            f"👤 {seller}\n"
            f'🖼 <a href="{lot.nft_url}">{lot.nft_url}</a>'
        )
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🖼 NFT", url=lot.nft_url)]
        ]
        if lot.seller and re.fullmatch(r"[A-Za-z0-9_]{4,64}", lot.seller):
            rows.append(
                [InlineKeyboardButton(text="✍️ Написать", url=f"https://t.me/{lot.seller}")]
            )
        try:
            await self.bot.send_message(
                self.owner_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("notify failed: %s", exc)


app = App()


# ----- inline keyboards -----


def main_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Парсинг", callback_data="menu:parse")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
        ]
    )


def prices_inline(prefix: str = "price") -> InlineKeyboardMarkup:
    """prefix=price for start parse, prefix=search for change range in settings."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for rid, label, _, _ in PRICE_RANGES:
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{rid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_inline() -> InlineKeyboardMarkup:
    stop = "⏹ Стоп" if app.running else "▶️ Парсинг выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Поиск / цена", callback_data="menu:search")],
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
        f"✅ Акк: <b>{app.account_name}</b>\n\n"
        f"Диапазон: <b>{app.range_label}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n\n"
        "Выбери:"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=main_inline())
        await target.answer()
    else:
        await target.answer(text, reply_markup=main_inline())


# ----- auth -----


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("…", reply_markup=ReplyKeyboardRemove())
    if app.logged_in:
        await _send_menu(message)
        return
    wipe_disk_junk()
    await state.set_state(AuthStates.phone)
    await message.answer(
        "🎁 <b>Telegram Market · Stars</b>\n\n"
        "Вход: номер → код.\n"
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
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    await state.set_state(AuthStates.code)
    await message.answer(f"{reply}\nПришли код:")


@router.message(StateFilter(AuthStates.code))
async def got_code(message: Message, state: FSMContext) -> None:
    try:
        result = await app.confirm_code(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    if result == "NEED_PASSWORD":
        await state.set_state(AuthStates.password)
        await message.answer("🔒 2FA пароль:")
        return
    await state.clear()
    await _send_menu(message, prefix="Вход ок.\n")


@router.message(StateFilter(AuthStates.password))
async def got_password(message: Message, state: FSMContext) -> None:
    try:
        await app.confirm_password(message.text or "")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await _send_menu(message, prefix="Вход ок.\n")


# ----- inline menu -----


@router.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала /start и вход", show_alert=True)
        return
    await _send_menu(callback)


@router.callback_query(F.data == "menu:parse")
async def cb_parse(callback: CallbackQuery) -> None:
    if not app.logged_in:
        await callback.answer("Сначала вход", show_alert=True)
        return
    await callback.message.edit_text(
        "🔍 <b>Парсинг</b>\nВыбери диапазон Stars — потом только новые лоты:",
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
        f"Поиск: <b>{app.range_label}</b> "
        f"({int(app.min_stars)}–{int(app.max_stars)} ⭐)\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>",
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:search")
async def cb_search(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🔎 <b>Поиск / цена</b>\n"
        "Смени диапазон (например с 2–5k на 5–15k).\n"
        "Если парсинг уже идёт — перезапустится с новым фильтром.",
        reply_markup=prices_inline("search"),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:stop")
async def cb_stop(callback: CallbackQuery) -> None:
    text = await app.stop_monitor()
    await callback.message.edit_text(
        f"{text}\n\n⚙️ Настройки",
        reply_markup=settings_inline(),
    )
    await callback.answer("Остановлено")


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery) -> None:
    stats = app.market.last_stats
    await callback.message.edit_text(
        "📊 <b>Статус</b>\n"
        f"Акк: <b>{app.account_name}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Поиск: <b>{app.range_label}</b>\n"
        f"Новых: <b>{app.lots_notified}</b>\n"
        f"Seen: <b>{len(app._seen)}</b>\n"
        f"Скан: lots={stats.get('lots', 0)} ok={stats.get('ok', 0)} "
        f"err={stats.get('errors', 0)}",
        reply_markup=settings_inline(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("price:"))
async def cb_price_start(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":", 1)[-1]
    chosen = _range_by_id(rid)
    if not chosen:
        await callback.answer("Неизвестный диапазон", show_alert=True)
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
        await callback.answer("Неизвестный диапазон", show_alert=True)
        return
    label, mn, mx = chosen
    app.set_range(label, mn, mx)
    was_running = app.running
    if was_running:
        status = await app.start_monitor(callback.from_user.id)
        text = f"🔎 Поиск: <b>{label}</b>\n{status}"
    else:
        text = (
            f"🔎 Поиск сохранён: <b>{label}</b>\n"
            f"{mn}–{mx} ⭐\n"
            "Запуск — кнопка «Парсинг»."
        )
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
            BotCommand(command="logout", description="Сбросить аккаунт"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Ready | inline UI | fast parse | owner resolve")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
