"""
Простой бот: парсит Telegram Market (только лоты за Stars)
и кидает самые свежие.

Старт / Стоп — в синем меню бота.
"""

from __future__ import annotations

import asyncio
import logging
import re
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

import credentials as creds
from market import Lot, TelegramMarket

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
        self.market = TelegramMarket(self.client)
        self.bot: Bot | None = None
        self.owner_id: int | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self._seen: set[str] = set()
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

    async def start_monitor(self, user_id: int) -> str:
        if self.running:
            await self.stop_monitor()
        self.owner_id = user_id
        self.running = True
        self._seen.clear()
        self._task = asyncio.create_task(self._loop(), name="market-loop")
        return (
            f"▶️ Парсинг Telegram Market (только Stars)\n"
            f"Диапазон: {int(self.min_stars)}–{int(self.max_stars)} ⭐\n"
            f"Кидаю самые свежие лоты…"
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

    async def _loop(self) -> None:
        primed = False
        while self.running:
            try:
                lots = await self.market.fetch_latest(limit=50)
                in_range = [
                    lot
                    for lot in lots
                    if self.min_stars <= lot.stars <= self.max_stars
                ]

                if not primed:
                    self._seen = {lot.id for lot in lots}
                    primed = True
                    preview = in_range[: creds.PREVIEW_LOTS]
                    if self.owner_id and self.bot:
                        await self.bot.send_message(
                            self.owner_id,
                            f"📡 Живой парсер Telegram Market\n"
                            f"Сейчас в диапазоне: <b>{len(in_range)}</b> лотов за Stars\n"
                            f"Показываю {len(preview)} свежих, дальше только новые:",
                        )
                    for lot in preview:
                        self._seen.add(lot.id)
                        await self._notify(lot)
                    await asyncio.sleep(creds.POLL_INTERVAL)
                    continue

                fresh = [lot for lot in in_range if lot.id not in self._seen]
                for lot in fresh:
                    self._seen.add(lot.id)
                    logger.info("NEW %.0f⭐ %s", lot.stars, lot.display)
                    await self._notify(lot)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("poll failed: %s", exc)

            await asyncio.sleep(creds.POLL_INTERVAL)

    async def _notify(self, lot: Lot) -> None:
        if not self.bot or not self.owner_id:
            return
        seller = f"@{lot.seller}" if lot.seller else "—"
        text = (
            "🆕 <b>Новый лот · Telegram Market</b>\n\n"
            f"🎁 <b>{_esc(lot.display)}</b>\n"
            f"💰 <b>{_fmt(lot.stars)} ⭐</b>\n"
            f"👤 {seller}\n"
            f'🖼 <a href="{lot.nft_url}">{lot.nft_url}</a>'
        )
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🖼 NFT", url=lot.nft_url)]
        ]
        if lot.seller:
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
        text = await app.start_monitor(message.from_user.id)
        await message.answer(
            f"✅ Вход: <b>{who}</b>\n\n{text}\n\nСтоп — /stop",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await state.set_state(AuthStates.phone)
    await message.answer(
        "🎁 <b>Telegram Market · только Stars</b>\n\n"
        "Чтобы парсить официальный маркет, нужен вход в Telegram.\n"
        "📱 Пришли номер в формате <code>+79991234567</code>",
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


async def main() -> None:
    bot = Bot(
        token=creds.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    app.bot = bot
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Старт"),
            BotCommand(command="stop", description="Стоп"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Telegram Market Stars parser ready")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
