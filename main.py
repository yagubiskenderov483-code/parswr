"""
Бот: Telegram Market · только лоты за Stars.
Нужен вход один раз (номер+код) — официальный API маркета без юзера не работает.

Кидает самые свежие выставления (окно ~1 час), фильтр по Stars.
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
    CallbackQuery,
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

import credentials as creds
from market import FRESH_WINDOW_SEC, Lot, TelegramMarket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

router = Router()


class AuthStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


class App:
    def __init__(self) -> None:
        Path("data").mkdir(exist_ok=True)
        self.client = TelegramClient(creds.SESSION, creds.API_ID, creds.API_HASH)
        self.market = TelegramMarket(self.client, concurrency=16)
        self.bot: Bot | None = None
        self.owner_id: int | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        # id → first_seen_monotonic
        self._seen: dict[str, float] = {}
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.min_stars = float(creds.MIN_STARS)
        self.max_stars = float(creds.MAX_STARS)

    async def ensure_connected(self) -> TelegramClient:
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def is_authorized(self) -> bool:
        await self.ensure_connected()
        return await self.client.is_user_authorized()

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
        return "OK"

    async def confirm_password(self, password: str) -> None:
        await self.client.sign_in(password=password.strip())

    async def logout(self) -> None:
        """Drop Telethon session so bot asks for a new phone bind."""
        await self.stop_monitor()
        try:
            if self.client.is_connected():
                if await self.client.is_user_authorized():
                    try:
                        await self.client.log_out()
                    except Exception:  # noqa: BLE001
                        pass
                await self.client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.warning("logout disconnect: %s", exc)

        root = Path(__file__).resolve().parent
        data = root / "data"
        for path in data.glob("market_session*"):
            try:
                path.unlink(missing_ok=True)
                logger.info("removed session file %s", path)
            except OSError as exc:
                logger.warning("cannot remove %s: %s", path, exc)

        self.phone = None
        self.phone_code_hash = None
        self.client = TelegramClient(creds.SESSION, creds.API_ID, creds.API_HASH)
        self.market = TelegramMarket(self.client, concurrency=16)

    async def start_monitor(self, user_id: int) -> str:
        if self.running:
            await self.stop_monitor()
        self.owner_id = user_id
        self.running = True
        self._seen.clear()
        self._task = asyncio.create_task(self._loop(), name="market-loop")
        return (
            "▶️ Telegram Market · только ⭐\n"
            f"Диапазон: <b>{int(self.min_stars)}–{int(self.max_stars)} ⭐</b>\n"
            "Ищу лоты, выставленные только что (окно ~1 час).\n"
            "Старый инвентарь не спамлю — только свежие выставления."
        )

    async def stop_monitor(self) -> str:
        if not self.running and self._task is None:
            return "⏹ Уже остановлено."
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        return "⏹ Стоп."

    def _in_price(self, lot: Lot) -> bool:
        return self.min_stars <= lot.stars <= self.max_stars

    def _purge_old_seen(self) -> None:
        now = time.monotonic()
        old = [k for k, ts in self._seen.items() if now - ts > FRESH_WINDOW_SEC]
        for k in old:
            del self._seen[k]

    async def _loop(self) -> None:
        primed = False
        ticks = 0
        while self.running:
            started = time.monotonic()
            try:
                lots = await self.market.fetch_newest(per_collection=creds.PER_COLLECTION)
                in_range = [lot for lot in lots if self._in_price(lot)]
                self._purge_old_seen()

                if not primed:
                    # Seed current market silently — это «уже висит», не «только что выставили»
                    now = time.monotonic()
                    for lot in lots:
                        self._seen[lot.id] = now
                    primed = True

                    # Показать срез самых свежих в диапазоне (верх выдачи API = newest)
                    preview = in_range[: creds.PREVIEW_LOTS]
                    if self.owner_id and self.bot:
                        await self.bot.send_message(
                            self.owner_id,
                            "📡 Живой парсер · Telegram Market (Stars)\n"
                            f"Сканирую все коллекции. В диапазоне сейчас: <b>{len(in_range)}</b>\n"
                            f"Показываю топ-{len(preview)} самых свежих, "
                            "дальше кидаю только новые выставления (~1 час):",
                        )
                    for lot in preview:
                        await self._notify(lot)
                    logger.info(
                        "primed seen=%s in_range=%s preview=%s",
                        len(self._seen),
                        len(in_range),
                        len(preview),
                    )
                else:
                    fresh = [lot for lot in in_range if lot.id not in self._seen]
                    now = time.monotonic()
                    # Also mark out-of-range as seen so we don't later notify on price drift spam
                    for lot in lots:
                        if lot.id not in self._seen:
                            if self._in_price(lot):
                                continue  # handled below
                            self._seen[lot.id] = now

                    for lot in fresh:
                        self._seen[lot.id] = now
                        logger.info(
                            "NEW %.0f⭐ [%s] %s @%s",
                            lot.stars,
                            lot.category,
                            lot.display,
                            lot.seller or "—",
                        )
                        await self._notify(lot)

                    if ticks % 20 == 0:
                        logger.info(
                            "tick#%s lots=%s in_range=%s fresh=%s seen=%s",
                            ticks,
                            len(lots),
                            len(in_range),
                            len(fresh),
                            len(self._seen),
                        )

                ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("poll failed: %s", exc)

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.05, creds.POLL_INTERVAL - elapsed))

    async def _notify(self, lot: Lot) -> None:
        if not self.bot or not self.owner_id:
            return
        seller = f"@{lot.seller}" if lot.seller else "—"
        text = (
            "🆕 <b>Свежий лот · Telegram Market</b>\n\n"
            f"🎁 <b>{_esc(lot.display)}</b>\n"
            f"💰 <b>{_fmt(lot.stars)} ⭐</b>\n"
            f"📈 {_esc(lot.category)}\n"
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


def _auth_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Парсить с этим акком", callback_data="auth:continue")],
            [InlineKeyboardButton(text="🔄 Привязать новый аккаунт", callback_data="auth:rebind")],
        ]
    )


def _phone_kb() -> ReplyKeyboardMarkup:
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


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await app.is_authorized():
        me = await app.client.get_me()
        who = f"@{me.username}" if me.username else (me.first_name or str(me.id))
        await message.answer(
            f"Сейчас привязан аккаунт: <b>{who}</b>\n\n"
            "Продолжить с ним или привязать новый?",
            reply_markup=_auth_choice_kb(),
        )
        return

    await _ask_phone(message, state)


@router.callback_query(F.data == "auth:continue")
async def on_auth_continue(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not await app.is_authorized():
        await callback.message.answer("Сессии нет. Пришли номер:")
        await _ask_phone(callback.message, state)
        return
    me = await app.client.get_me()
    who = f"@{me.username}" if me.username else (me.first_name or str(me.id))
    text = await app.start_monitor(callback.from_user.id)
    await callback.message.answer(
        f"✅ Вход: <b>{who}</b>\n\n{text}\n\nСтоп — /stop",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(F.data == "auth:rebind")
async def on_auth_rebind(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Сбрасываю старый аккаунт…")
    await app.logout()
    await state.clear()
    await callback.message.answer(
        "Старая привязка сброшена.\n"
        "Пришли номер нового аккаунта:",
        reply_markup=_phone_kb(),
    )
    await state.set_state(AuthStates.phone)


@router.message(Command("logout"))
@router.message(Command("relogin"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    await app.logout()
    await message.answer(
        "🔄 Старый аккаунт отвязан.\n"
        "Пришли номер для новой привязки:",
        reply_markup=_phone_kb(),
    )
    await state.set_state(AuthStates.phone)


async def _ask_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(AuthStates.phone)
    await message.answer(
        "🎁 <b>Telegram Market · лоты за ⭐</b>\n\n"
        "⚠️ Нужна привязка Telegram-аккаунта (номер → код).\n"
        "Без этого официальный маркет не парсится.\n\n"
        "📱 Номер: <code>+79991234567</code>",
        reply_markup=_phone_kb(),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = await app.stop_monitor()
    await message.answer(
        f"{text}\nНажми Старт, чтобы снова искать лоты.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(AuthStates.phone), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.code), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.password), F.text == "❌ Отмена")
async def cancel_auth(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Нажми /start.", reply_markup=ReplyKeyboardRemove())


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
    await message.answer(f"{reply}\nПришли код:", reply_markup=_phone_kb())


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
        await message.answer("🔒 2FA пароль:", reply_markup=_phone_kb())
        return

    await state.clear()
    text = await app.start_monitor(message.from_user.id)
    await message.answer(
        f"✅ Вход выполнен.\n\n{text}\nСтоп — /stop",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(AuthStates.password))
async def got_password(message: Message, state: FSMContext) -> None:
    try:
        await app.confirm_password(message.text or "")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Пароль не принят: {exc}")
        return
    await state.clear()
    text = await app.start_monitor(message.from_user.id)
    await message.answer(
        f"✅ Вход выполнен.\n\n{text}\nСтоп — /stop",
        reply_markup=ReplyKeyboardRemove(),
    )


def wipe_old_data() -> None:
    root = Path(__file__).resolve().parent
    data = root / "data"
    data.mkdir(exist_ok=True)
    removed: list[str] = []

    # Старые сессии (v1) — форсим новую привязку
    for path in [
        data / "market_session.session",
        data / "market_session.session-journal",
        *data.glob("market_session.session*"),
    ]:
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            logger.warning("cannot delete %s: %s", path, exc)

    for path in [
        data / "bot.db",
        root / "bot.db",
        *data.glob("*.db"),
        *data.glob("*.db-*"),
    ]:
        try:
            if path.is_file() and "market_session" not in path.name:
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            logger.warning("cannot delete %s: %s", path, exc)

    old_pkg = root / "bot"
    if old_pkg.is_dir():
        import shutil

        shutil.rmtree(old_pkg, ignore_errors=True)
        removed.append(str(old_pkg))

    if removed:
        logger.info("Wiped old data: %s", removed)


async def main() -> None:
    wipe_old_data()
    bot = Bot(
        token=creds.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    app.bot = bot
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Старт / сменить аккаунт"),
            BotCommand(command="stop", description="Стоп"),
            BotCommand(command="logout", description="Отвязать аккаунт"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Telegram Market Stars parser ready | session=%s", creds.SESSION)
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
