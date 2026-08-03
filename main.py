"""
Telegram Market bot · Stars gifts.

После входа — меню: Парсинг | Настройки.
В парсинге выбираешь цену → кидает только НОВЫЕ лоты.
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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
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

PRICE_RANGES: list[tuple[str, int, int]] = [
    ("Easy 2k–5k", 2000, 5000),
    ("Medium 5k–10k", 5000, 10000),
    ("Hard 15k–30k", 15000, 30000),
    ("Impossible 30k–65k", 30000, 65000),
    ("Unreal 65k–100k", 65000, 100000),
    ("Все 2k–100k", 2000, 100000),
]


class AuthStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


class MenuStates(StatesGroup):
    main = State()
    pick_price = State()
    settings = State()


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
        except Exception as exc:  # noqa: BLE001
            logger.warning("disconnect: %s", exc)
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
            "Запоминаю рынок, дальше кидаю только <b>НОВЫЕ</b> лоты…"
        )

    async def stop_monitor(self) -> str:
        if not self.running and self._task is None:
            return "⏹ Парсинг уже выключен."
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        return f"⏹ Стоп. Новых лотов за сессию: {self.lots_notified}"

    def _in_price(self, lot: Lot) -> bool:
        return self.min_stars <= lot.stars <= self.max_stars

    def _purge_old_seen(self) -> None:
        now = time.monotonic()
        for k in [k for k, ts in self._seen.items() if now - ts > FRESH_WINDOW_SEC]:
            del self._seen[k]

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
                            "⏳ Сканирую Telegram Market…",
                        )
                    lots = await self.market.fetch_newest(
                        per_collection=creds.PER_COLLECTION
                    )
                    now = time.monotonic()
                    for lot in lots:
                        self._seen[lot.id] = now
                    primed = True
                    in_range = [lot for lot in lots if self._in_price(lot)]
                    stats = self.market.last_stats
                    if self.owner_id and self.bot:
                        await self.bot.send_message(
                            self.owner_id,
                            "✅ Рынок зафиксирован\n"
                            f"Коллекций: <b>{stats.get('collections', 0)}</b>\n"
                            f"Лотов Stars всего: <b>{len(lots)}</b>\n"
                            f"В твоём диапазоне: <b>{len(in_range)}</b>\n"
                            f"API ok/empty/err: {stats.get('ok', 0)}/"
                            f"{stats.get('empty', 0)}/{stats.get('errors', 0)}\n\n"
                            "Дальше шлю только <b>новые</b> выставления.\n"
                            "Стоп — в Настройках или /stop",
                        )
                    logger.info(
                        "primed total=%s in_range=%s stats=%s",
                        len(lots),
                        len(in_range),
                        stats,
                    )
                else:
                    self._purge_old_seen()
                    fresh_n = 0
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
                            if not self._in_price(lot):
                                continue
                            fresh_n += 1
                            self.lots_notified += 1
                            logger.info(
                                "NEW %.0f⭐ [%s] %s",
                                lot.stars,
                                lot.category,
                                lot.display,
                            )
                            await self._notify(lot)
                    if ticks % 15 == 0:
                        logger.info(
                            "wave#%s fresh=%s seen=%s notified=%s",
                            ticks,
                            fresh_n,
                            len(self._seen),
                            self.lots_notified,
                        )
                ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("poll failed: %s", exc)
                if self.owner_id and self.bot and ticks % 20 == 0:
                    try:
                        await self.bot.send_message(
                            self.owner_id,
                            f"⚠️ Ошибка парсера: {_esc(str(exc)[:200])}",
                        )
                    except Exception:  # noqa: BLE001
                        pass

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.05, creds.POLL_INTERVAL - elapsed))

    async def _notify(self, lot: Lot) -> None:
        if not self.bot or not self.owner_id:
            return
        seller = f"@{lot.seller}" if lot.seller else "—"
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
                [
                    InlineKeyboardButton(
                        text="✍️ Написать", url=f"https://t.me/{lot.seller}"
                    )
                ]
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


# ----- keyboards -----


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Парсинг")],
            [KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


def price_menu_kb() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=label)] for label, _, _ in PRICE_RANGES]
    rows.append([KeyboardButton(text="⬅️ Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def settings_kb(running: bool) -> ReplyKeyboardMarkup:
    stop_or_status = "⏹ Стоп парсинг" if running else "▶️ Парсинг выключен"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=stop_or_status)],
            [KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="🔄 Сменить аккаунт")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
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
    old_pkg = root / "bot"
    if old_pkg.is_dir():
        shutil.rmtree(old_pkg, ignore_errors=True)


async def _show_main_menu(message: Message, state: FSMContext, prefix: str = "") -> None:
    await state.set_state(MenuStates.main)
    text = prefix + (
        f"✅ Акк: <b>{app.account_name}</b>\n\n"
        "Выбери:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


# ----- auth -----


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if app.logged_in:
        await _show_main_menu(message, state)
        return
    wipe_disk_junk()
    await state.set_state(AuthStates.phone)
    await message.answer(
        "🎁 <b>Telegram Market · Stars</b>\n\n"
        "Сначала вход (номер → код).\n"
        "Потом меню: Парсинг / Настройки.\n\n"
        "📱 Номер: <code>+79991234567</code>",
        reply_markup=phone_kb(),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext) -> None:
    text = await app.stop_monitor()
    if app.logged_in:
        await state.set_state(MenuStates.main)
        await message.answer(text, reply_markup=main_menu_kb())
    else:
        await state.clear()
        await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await app.reset_auth()
    await state.set_state(AuthStates.phone)
    await message.answer("Аккаунт сброшен. Пришли номер:", reply_markup=phone_kb())


@router.message(StateFilter(AuthStates.phone), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.code), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.password), F.text == "❌ Отмена")
async def cancel_auth(message: Message, state: FSMContext) -> None:
    await state.clear()
    await app.reset_auth()
    await message.answer("Отменено. /start — заново.", reply_markup=ReplyKeyboardRemove())


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
        await message.answer(f"⚠️ Не удалось отправить код: {exc}")
        return
    await state.set_state(AuthStates.code)
    await message.answer(f"{reply}\nПришли код:", reply_markup=phone_kb())


@router.message(StateFilter(AuthStates.code))
async def got_code(message: Message, state: FSMContext) -> None:
    try:
        result = await app.confirm_code(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Ошибка входа: {exc}")
        return
    if result == "NEED_PASSWORD":
        await state.set_state(AuthStates.password)
        await message.answer("🔒 2FA пароль:", reply_markup=phone_kb())
        return
    await _show_main_menu(message, state, prefix="Вход ок.\n")


@router.message(StateFilter(AuthStates.password))
async def got_password(message: Message, state: FSMContext) -> None:
    try:
        await app.confirm_password(message.text or "")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Пароль не принят: {exc}")
        return
    await _show_main_menu(message, state, prefix="Вход ок.\n")


# ----- main menu -----


@router.message(MenuStates.main, F.text == "🔍 Парсинг")
async def menu_parsing(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuStates.pick_price)
    await message.answer(
        "Выбери диапазон цены (Stars).\n"
        "После выбора запомню рынок и буду кидать только <b>новые</b> лоты.",
        reply_markup=price_menu_kb(),
    )


@router.message(MenuStates.main, F.text == "⚙️ Настройки")
async def menu_settings(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuStates.settings)
    await message.answer(
        "⚙️ Настройки\n"
        f"Акк: <b>{app.account_name}</b>\n"
        f"Диапазон: <b>{app.range_label}</b> "
        f"({int(app.min_stars)}–{int(app.max_stars)} ⭐)\n"
        f"Парсинг: <b>{'▶️ идёт' if app.running else '⏹ выкл'}</b>\n"
        f"Новых лотов: <b>{app.lots_notified}</b>",
        reply_markup=settings_kb(app.running),
    )


@router.message(MenuStates.pick_price, F.text == "⬅️ Назад")
@router.message(MenuStates.settings, F.text == "⬅️ Назад")
async def back_to_main(message: Message, state: FSMContext) -> None:
    await _show_main_menu(message, state)


@router.message(MenuStates.pick_price)
async def pick_price(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    chosen = next((r for r in PRICE_RANGES if r[0] == text), None)
    if not chosen:
        await message.answer("Выбери диапазон кнопкой ниже.", reply_markup=price_menu_kb())
        return

    label, min_s, max_s = chosen
    app.set_range(label, min_s, max_s)
    try:
        status = await app.start_monitor(message.from_user.id)
    except RuntimeError as exc:
        await message.answer(f"⚠️ {exc}")
        await state.set_state(AuthStates.phone)
        await message.answer("Пришли номер:", reply_markup=phone_kb())
        return

    await state.set_state(MenuStates.main)
    await message.answer(status, reply_markup=main_menu_kb())


@router.message(MenuStates.settings, F.text == "⏹ Стоп парсинг")
async def settings_stop(message: Message, state: FSMContext) -> None:
    text = await app.stop_monitor()
    await message.answer(text, reply_markup=settings_kb(app.running))


@router.message(MenuStates.settings, F.text == "▶️ Парсинг выключен")
async def settings_already_off(message: Message) -> None:
    await message.answer(
        "Парсинг выключен. Запуск — через «🔍 Парсинг» и выбор цены.",
        reply_markup=settings_kb(False),
    )


@router.message(MenuStates.settings, F.text == "📊 Статус")
async def settings_status(message: Message) -> None:
    stats = app.market.last_stats
    await message.answer(
        "📊 Статус\n"
        f"Акк: <b>{app.account_name}</b>\n"
        f"Парсинг: <b>{'▶️' if app.running else '⏹'}</b>\n"
        f"Диапазон: <b>{app.range_label}</b>\n"
        f"Новых за сессию: <b>{app.lots_notified}</b>\n"
        f"В памяти seen: <b>{len(app._seen)}</b>\n"
        f"Последний скан: lots={stats.get('lots', 0)} "
        f"ok={stats.get('ok', 0)} empty={stats.get('empty', 0)} "
        f"err={stats.get('errors', 0)}",
        reply_markup=settings_kb(app.running),
    )


@router.message(MenuStates.settings, F.text == "🔄 Сменить аккаунт")
async def settings_relogin(message: Message, state: FSMContext) -> None:
    await app.reset_auth()
    await state.set_state(AuthStates.phone)
    await message.answer("Пришли новый номер:", reply_markup=phone_kb())


@router.message(MenuStates.main)
@router.message(MenuStates.settings)
@router.message(MenuStates.pick_price)
async def menu_fallback(message: Message, state: FSMContext) -> None:
    st = await state.get_state()
    if st == MenuStates.pick_price.state:
        await message.answer("Жми кнопку с ценой.", reply_markup=price_menu_kb())
    elif st == MenuStates.settings.state:
        await message.answer("Жми кнопку настроек.", reply_markup=settings_kb(app.running))
    else:
        await message.answer("Меню:", reply_markup=main_menu_kb())


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
            BotCommand(command="stop", description="Стоп парсинга"),
            BotCommand(command="logout", description="Сменить аккаунт"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Ready | menu Parsing/Settings | new lots only")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
