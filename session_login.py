"""Вход юзербота через Telegram-бот: /start → телефон → код → 2FA."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardRemove
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

logger = logging.getLogger("session_login")


class LoginStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


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


class AuthFlow:
    def __init__(self, client: TelegramClient, session_file: str) -> None:
        self.client = client
        self.session_file = session_file
        self.phone: str | None = None
        self.phone_code_hash: str | None = None

    async def send_code(self, phone: str) -> str:
        phone = _normalize_phone(phone)
        if not self.client.is_connected():
            await self.client.connect()
        try:
            result = await self.client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            raise ValueError("Неверный номер. Пример: +79991234567") from exc
        except FloodWaitError as exc:
            raise ValueError(f"Подожди {exc.seconds} сек.") from exc
        self.phone = phone
        self.phone_code_hash = result.phone_code_hash
        return "Код отправлен."

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
            raise ValueError("Код истёк. Отправь /start и номер заново.") from exc
        await self._save_session()
        return "OK"

    async def confirm_password(self, password: str) -> None:
        try:
            await self.client.sign_in(password=password.strip())
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Неверный пароль 2FA.") from exc
        await self._save_session()

    async def _save_session(self) -> None:
        session = StringSession.save(self.client.session)
        path = Path(self.session_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session, encoding="utf-8")
        logger.info("Сессия сохранена в %s", path)


async def bot_login_wizard(cfg) -> TelegramClient:
    """Запускает aiogram polling для входа, затем возвращает клиент Telethon."""
    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.connect()

    auth = AuthFlow(client, cfg.session_file)
    login_done = asyncio.Event()

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()

    async def finish_success(message: Message) -> None:
        me = await client.get_me()
        name = f"@{me.username}" if me.username else (me.first_name or str(me.id))
        await message.answer(
            f"✅ Вход выполнен: {name}\n"
            "Сессия сохранена. Трекер продолжает работу."
        )
        login_done.set()
        await dp.stop_polling()

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(LoginStates.phone)
        await message.answer(
            "🔐 <b>Вход для гифт-трекера</b>\n"
            "Отправь номер телефона:\n"
            "<code>+79991234567</code>",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(StateFilter(LoginStates.phone))
    async def got_phone(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if text.startswith("/"):
            return
        try:
            reply = await auth.send_code(text)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ {exc}")
            return
        await state.set_state(LoginStates.code)
        await message.answer(f"{reply} Пришли код из Telegram:")

    @router.message(StateFilter(LoginStates.code))
    async def got_code(message: Message, state: FSMContext) -> None:
        try:
            result = await auth.confirm_code(message.text or "")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ {exc}")
            return
        if result == "NEED_PASSWORD":
            await state.set_state(LoginStates.password)
            await message.answer("🔒 Введи пароль двухфакторной аутентификации:")
            return
        await state.clear()
        await finish_success(message)

    @router.message(StateFilter(LoginStates.password))
    async def got_password(message: Message, state: FSMContext) -> None:
        try:
            await auth.confirm_password(message.text or "")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ {exc}")
            return
        await state.clear()
        await finish_success(message)

    dp.include_router(router)

    bot_info = await bot.get_me()
    logger.info(
        "Сессия не найдена. Напишите /start боту @%s для входа.",
        bot_info.username,
    )

    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))

    try:
        await login_done.wait()
    finally:
        if not polling_task.done():
            await dp.stop_polling()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("polling stopped: %s", exc)
        await bot.session.close()

    if not await client.is_user_authorized():
        raise RuntimeError("Вход не завершён")

    return client
