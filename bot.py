"""@jsjeigiejwhnewbot — вход в аккаунт (/start) и /status."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardRemove
from html import escape as _esc
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import config

logger = logging.getLogger("bot")


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


def _sent_code_hint(result: Any) -> str:
    t = getattr(result, "type", None)
    name = type(t).__name__ if t is not None else ""
    timeout = getattr(t, "timeout", None) or getattr(result, "timeout", None)
    extra = f"\nКод живёт ~{int(timeout)}с." if timeout else ""
    if "App" in name:
        return (
            "Код ушёл <b>в приложение Telegram</b> на этот номер "
            "(чат «Telegram» / уведомление входа) — это не SMS."
            f"{extra}\n"
            "Нет кода? Напиши <code>смс</code> — отправим SMS."
        )
    if "Word" in name or "Phrase" in name:
        return "Telegram прислал <b>слово/фразу</b>. Введи её как есть." + extra
    if "Call" in name or "Flash" in name or "Missed" in name:
        return (
            "Код придёт <b>звонком</b> (последние цифры — код)."
            f"{extra}\nНет звонка? Напиши <code>смс</code>."
        )
    if "Email" in name:
        return "Код ушёл на <b>email</b>." + extra
    if "Sms" in name or "Firebase" in name:
        return "Код ушёл <b>SMS</b>. Подожди 1–2 минуты." + extra
    return (
        "Код отправлен. Смотри приложение Telegram на этом номере (не SMS)."
        f"{extra}\nНет кода? Напиши <code>смс</code>."
    )


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
        logger.info("send_code %s type=%s", phone, type(getattr(result, "type", None)).__name__)
        return _sent_code_hint(result)

    async def resend_sms(self) -> str:
        if not self.phone:
            raise ValueError("Сначала отправь номер.")
        try:
            result = await self.client.send_code_request(self.phone, force_sms=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"SMS не ушло: {exc}. Смотри код в приложении Telegram."
            ) from exc
        self.phone_code_hash = result.phone_code_hash
        return _sent_code_hint(result)

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


class ControlBot:
    def __init__(self, client: TelegramClient, session_file: str) -> None:
        self.client = client
        self.session_file = session_file
        self.runtime: Any = None
        self.queue: Any = None
        self.bot_username = config.BOT_USERNAME
        self._login_done = asyncio.Event()
        self._bot = Bot(
            token=config.bot_token(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher(storage=MemoryStorage())
        self._auth = AuthFlow(client, session_file)
        self._dp.include_router(self._router())
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            me = await self._bot.get_me()
            if me.username:
                self.bot_username = me.username
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._bot.delete_webhook(drop_pending_updates=True)
        except Exception:  # noqa: BLE001
            pass
        self._task = asyncio.create_task(
            self._dp.start_polling(self._bot), name="control-bot"
        )
        logger.info("Бот @%s слушает команды", self.bot_username)

    async def stop(self) -> None:
        if self._task:
            await self._dp.stop_polling()
            self._task.cancel()
        await self._bot.session.close()

    async def wait_login(self) -> None:
        if await self.client.is_user_authorized():
            self._login_done.set()
            return
        await self._login_done.wait()

    def _router(self) -> Router:
        router = Router()
        auth = self._auth

        async def _ok() -> bool:
            try:
                return await self.client.is_user_authorized()
            except Exception:  # noqa: BLE001
                return False

        @router.message(CommandStart())
        async def cmd_start(message: Message, state: FSMContext) -> None:
            if await _ok():
                await state.clear()
                self._login_done.set()
                await message.answer(
                    f"🤖 <b>Гифт-трекер</b> v{config.TRACKER_VERSION}\n"
                    f"Аккаунт: подключён ✅\n"
                    f"Канал: <code>{config.CHANNEL_ID}</code>\n"
                    f"Цена: <b>{config.MIN_STARS}–{config.MAX_STARS}⭐</b>\n"
                    f"Level ≤ {config.MAX_ACCOUNT_LEVEL} · NFT ≤ {config.MAX_NFTS}\n"
                    f"Только девочки · бесплатные ЛС · пост / {int(config.POST_INTERVAL)}с\n\n"
                    "/status — статус"
                )
                return
            await state.clear()
            await state.set_state(LoginStates.phone)
            await message.answer(
                "🔐 <b>Вход для гифт-трекера</b>\n"
                "Отправь номер телефона:\n"
                "<code>+79991234567</code>",
                reply_markup=ReplyKeyboardRemove(),
            )

        @router.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            if not await _ok():
                await message.answer("❌ Не авторизован — /start")
                return
            me = await self.client.get_me()
            rt = self.runtime
            lines = [
                f"✅ Трекер v{config.TRACKER_VERSION}",
                f"Аккаунт: {me.username or me.first_name}",
                f"Канал: <code>{config.CHANNEL_ID}</code>",
                f"Диапазон: {config.MIN_STARS}–{config.MAX_STARS}⭐",
                f"Фильтры: девочки · free ЛС · lvl≤{config.MAX_ACCOUNT_LEVEL} · "
                f"NFT≤{config.MAX_NFTS} · пост/{int(config.POST_INTERVAL)}с",
            ]
            if rt:
                lines.extend(
                    [
                        f"Снимок: {'готов' if rt.snapshot_ready else 'строится'} ({rt.snapshot})",
                        f"Проходов: {rt.passes}",
                        f"Коллекций: {rt.collections}",
                        f"Отправлено: {rt.posted}",
                        f"В очереди: {rt.queue}",
                        f"Последний проход: +{rt.last_fresh}",
                    ]
                )
                if rt.last_error:
                    lines.append(f"⚠️ {_esc(rt.last_error[:160])}")
            await message.answer("\n".join(lines))

        @router.message(StateFilter(LoginStates.phone))
        async def on_phone(message: Message, state: FSMContext) -> None:
            text = (message.text or "").strip()
            if not text:
                return
            try:
                hint = await auth.send_code(text)
            except ValueError as exc:
                await message.answer(f"⚠️ {exc}")
                return
            await state.set_state(LoginStates.code)
            await message.answer(hint)

        @router.message(StateFilter(LoginStates.code))
        async def on_code(message: Message, state: FSMContext) -> None:
            raw = (message.text or "").strip()
            if raw.lower() in {"смс", "sms", "повтор", "resend"}:
                try:
                    hint = await auth.resend_sms()
                except ValueError as exc:
                    await message.answer(f"⚠️ {exc}")
                    return
                await message.answer(hint)
                return
            try:
                reply = await auth.confirm_code(raw)
            except ValueError as exc:
                await message.answer(f"⚠️ {exc}")
                return
            if reply == "NEED_PASSWORD":
                await state.set_state(LoginStates.password)
                await message.answer("Нужен пароль 2FA. Отправь его сюда.")
                return
            await state.clear()
            self._login_done.set()
            await message.answer(
                "✅ Вход выполнен. Трекер сканирует маркет.\n/status"
            )

        @router.message(StateFilter(LoginStates.password))
        async def on_password(message: Message, state: FSMContext) -> None:
            try:
                await auth.confirm_password(message.text or "")
            except ValueError as exc:
                await message.answer(f"⚠️ {exc}")
                return
            await state.clear()
            self._login_done.set()
            await message.answer("✅ Вход выполнен (2FA). /status")

        return router
