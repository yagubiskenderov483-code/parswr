"""@jsjeigiejwhnewbot — вход (/start) и /status. Команды не ждут Telethon."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
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

_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="/status"), KeyboardButton(text="/start")],
    ],
    resize_keyboard=True,
)


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
            result = await asyncio.wait_for(
                self.client.send_code_request(phone), timeout=25.0
            )
        except PhoneNumberInvalidError as exc:
            raise ValueError("Неверный номер. Пример: +79991234567") from exc
        except FloodWaitError as exc:
            raise ValueError(f"Подожди {exc.seconds} сек.") from exc
        except asyncio.TimeoutError as exc:
            raise ValueError("Telegram не ответил. Нажми /start и ещё раз номер.") from exc
        self.phone = phone
        self.phone_code_hash = result.phone_code_hash
        logger.info("send_code %s type=%s", phone, type(getattr(result, "type", None)).__name__)
        return _sent_code_hint(result)

    async def resend_sms(self) -> str:
        if not self.phone:
            raise ValueError("Сначала отправь номер.")
        try:
            result = await asyncio.wait_for(
                self.client.send_code_request(self.phone, force_sms=True),
                timeout=25.0,
            )
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
            await asyncio.wait_for(
                self.client.sign_in(
                    phone=self.phone,
                    code=code,
                    phone_code_hash=self.phone_code_hash,
                ),
                timeout=25.0,
            )
        except SessionPasswordNeededError:
            return "NEED_PASSWORD"
        except PhoneCodeInvalidError as exc:
            raise ValueError("Неверный код.") from exc
        except PhoneCodeExpiredError as exc:
            raise ValueError("Код истёк. Отправь /start и номер заново.") from exc
        except asyncio.TimeoutError as exc:
            raise ValueError("Telegram не ответил на код. /start заново.") from exc
        await self._save_session()
        return "OK"

    async def confirm_password(self, password: str) -> None:
        try:
            await asyncio.wait_for(
                self.client.sign_in(password=password.strip()), timeout=25.0
            )
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
        self.authorized = False
        self.account_name = "—"
        self._login_done = asyncio.Event()
        self._bot = Bot(
            token=config.bot_token(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher(storage=MemoryStorage())
        self._auth = AuthFlow(client, session_file)
        self._dp.include_router(self._router())
        self._task: asyncio.Task | None = None

    @property
    def aiogram_bot(self) -> Bot:
        return self._bot

    def mark_authorized(self, name: str = "") -> None:
        self.authorized = True
        if name:
            self.account_name = name
        self._login_done.set()

    async def start(self) -> None:
        try:
            me = await asyncio.wait_for(self._bot.get_me(), timeout=15.0)
            if me.username:
                self.bot_username = me.username
        except Exception as exc:  # noqa: BLE001
            logger.warning("bot get_me: %s", exc)
        try:
            await self._bot.set_my_commands(
                [
                    BotCommand(command="start", description="Старт / вход"),
                    BotCommand(command="status", description="Статус трекера"),
                ]
            )
        except Exception:  # noqa: BLE001
            pass
        self._task = asyncio.create_task(self._poll_loop(), name="control-bot")
        logger.info("Бот @%s слушает команды", self.bot_username)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._bot.delete_webhook(drop_pending_updates=True)
                logger.info("polling старт @%s", self.bot_username)
                await self._dp.start_polling(
                    self._bot,
                    handle_signals=False,
                    allowed_updates=["message", "callback_query"],
                    close_bot_session=False,
                    handle_as_tasks=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("polling упал: %s — рестарт через 3с", exc)
                await asyncio.sleep(3.0)

    async def stop(self) -> None:
        try:
            await self._dp.stop_polling()
        except Exception:  # noqa: BLE001
            pass
        if self._task:
            self._task.cancel()
        await self._bot.session.close()

    async def wait_login(self) -> None:
        if self.authorized:
            self._login_done.set()
            return
        await self._login_done.wait()

    def _status_text(self) -> str:
        rt = self.runtime
        lines = [
            f"✅ Трекер v{config.TRACKER_VERSION}",
            f"Аккаунт: {self.account_name if self.authorized else 'не вошёл'}",
            f"Канал: <code>{config.CHANNEL_ID}</code>",
            f"Диапазон: {config.MIN_STARS}–{config.MAX_STARS}⭐ (цель 5k–25k)",
            f"Фильтры: русские девочки · ≤{config.MAX_NFTS} дорогих NFT · free ЛС · "
            f"lvl≤{config.MAX_ACCOUNT_LEVEL} · пост/{int(config.POST_INTERVAL)}с",
            "Обход: новые id сверху · не всплытие старых",
        ]
        if rt:
            coll = f"Коллекций: {rt.collections}"
            if rt.collections < config.MIN_COLLECTIONS:
                coll += f" (мало, нужно ≥{config.MIN_COLLECTIONS})"
            lines.extend(
                [
                    f"Страницы: {'готовы' if rt.snapshot_ready else 'синхрон'} ({rt.snapshot})",
                    f"Проходов: {rt.passes}",
                    coll,
                    f"Отправлено: {rt.posted}",
                    f"В очереди: {rt.queue}",
                    f"Последний проход: выставили {getattr(rt, 'last_found', rt.last_fresh)} → очередь +{rt.last_fresh}",
                ]
            )
            skip = rt.skip_total or {}
            if any(skip.values()):
                lines.append(
                    "Отсев: "
                    + " ".join(f"{k}−{v}" for k, v in skip.items() if v)
                )
            funnel = getattr(rt, "funnel", None) or {}
            if funnel.get("fresh_detected") or funnel.get("fresh"):
                fd = funnel.get("fresh_detected", funnel.get("fresh", 0))
                lines.append(
                    "Воронка: "
                    f"fresh={fd} "
                    f"price={funnel.get('price_pass', 0)}/{funnel.get('price_checked', 0)} "
                    f"seen={funnel.get('seen_pass', 0)}/{funnel.get('seen_checked', 0)} "
                    f"dup_s={funnel.get('dup_seller', 0)} dup_l={funnel.get('dup_listing', 0)} "
                    f"work={funnel.get('work_in', 0)} deq={funnel.get('dequeued', 0)} "
                    f"ru={funnel.get('ru_pass', 0)}/{funnel.get('ru_checked', 0)} "
                    f"girl={funnel.get('girl_pass', 0)}/{funnel.get('girl_checked', 0)} "
                    f"dm={funnel.get('dm_pass', 0)}/{funnel.get('dm_checked', 0)} "
                    f"lvl={funnel.get('level_pass', 0)}/{funnel.get('level_checked', 0)} "
                    f"nft={funnel.get('nft_pass', 0)}/{funnel.get('nft_checked', 0)} "
                    f"send={funnel.get('sent', 0)}/{funnel.get('send_attempt', 0)}"
                )
                lines.extend(
                    [
                        "PIPELINE",
                        f"fresh_detected: {fd}",
                        f"price: checked={funnel.get('price_checked', 0)} "
                        f"passed={funnel.get('price_pass', 0)} "
                        f"rejected={funnel.get('price_reject', 0)}",
                        f"seen: checked={funnel.get('seen_checked', 0)} "
                        f"passed={funnel.get('seen_pass', 0)} "
                        f"rejected={funnel.get('seen_reject', 0)}",
                        f"duplicates: seller={funnel.get('dup_seller', 0)} "
                        f"listing={funnel.get('dup_listing', 0)} "
                        f"work_in={funnel.get('work_in', 0)} "
                        f"dequeued={funnel.get('dequeued', 0)}",
                        f"ru: checked={funnel.get('ru_checked', 0)} "
                        f"passed={funnel.get('ru_pass', 0)} "
                        f"rejected={funnel.get('ru_reject', 0)} "
                        f"incomplete={funnel.get('reject_incomplete', 0)}",
                        f"girl: checked={funnel.get('girl_checked', 0)} "
                        f"passed={funnel.get('girl_pass', 0)} "
                        f"rejected={funnel.get('girl_reject', 0)}",
                        f"dm: checked={funnel.get('dm_checked', 0)} "
                        f"passed={funnel.get('dm_pass', 0)} "
                        f"rejected={funnel.get('dm_reject', 0)}",
                        f"level: checked={funnel.get('level_checked', 0)} "
                        f"passed={funnel.get('level_pass', 0)} "
                        f"rejected={funnel.get('level_reject', 0)}",
                        f"nft: checked={funnel.get('nft_checked', 0)} "
                        f"passed={funnel.get('nft_pass', 0)} "
                        f"rejected={funnel.get('nft_reject', 0)}",
                        f"send_attempt={funnel.get('send_attempt', 0)} "
                        f"sent={funnel.get('sent', 0)} "
                        f"failed={funnel.get('failed', 0)}",
                    ]
                )
            if rt.last_error:
                lines.append(f"⚠️ {_esc(str(rt.last_error)[:160])}")
        else:
            lines.append("⏳ Сканер ещё поднимается — подожди пару секунд.")
        return "\n".join(lines)

    def _hello_text(self) -> str:
        return (
            f"🤖 <b>Гифт-трекер</b> v{config.TRACKER_VERSION}\n"
            f"Аккаунт: {'подключён ✅' if self.authorized else 'нужен вход'}\n"
            f"Канал: <code>{config.CHANNEL_ID}</code>\n"
            f"Цена: <b>{config.MIN_STARS}–{config.MAX_STARS}⭐</b>\n"
            f"Level ≤ {config.MAX_ACCOUNT_LEVEL} · дорогих NFT ≤ {config.MAX_NFTS}\n"
            f"Русские девочки · ≤{config.MAX_NFTS} дорогих NFT · пост / {int(config.POST_INTERVAL)}с\n\n"
            "Жми /status или кнопку ниже."
        )

    def _router(self) -> Router:
        router = Router()
        auth = self._auth

        @router.message(CommandStart())
        async def cmd_start(message: Message, state: FSMContext) -> None:
            try:
                if self.authorized:
                    await state.clear()
                    self._login_done.set()
                    await message.answer(self._hello_text(), reply_markup=_MENU)
                    return
                await state.clear()
                await state.set_state(LoginStates.phone)
                await message.answer(
                    "🔐 <b>Вход для гифт-трекера</b>\n"
                    "Отправь номер телефона:\n"
                    "<code>+79991234567</code>",
                    reply_markup=ReplyKeyboardRemove(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("cmd_start")
                await message.answer(f"⚠️ {exc}")

        @router.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            try:
                if not self.authorized:
                    await message.answer("❌ Не авторизован — /start")
                    return
                await message.answer(self._status_text(), reply_markup=_MENU)
            except Exception as exc:  # noqa: BLE001
                logger.exception("cmd_status")
                await message.answer(f"⚠️ {exc}")

        @router.message(F.text.lower().in_({"старт", "start", "статус", "status"}))
        async def cmd_aliases(message: Message, state: FSMContext) -> None:
            raw = (message.text or "").strip().lower()
            if raw in {"статус", "status"}:
                await cmd_status(message)
                return
            await cmd_start(message, state)

        @router.message(StateFilter(LoginStates.phone))
        async def on_phone(message: Message, state: FSMContext) -> None:
            text = (message.text or "").strip()
            if not text or text.startswith("/"):
                return
            try:
                hint = await auth.send_code(text)
            except ValueError as exc:
                await message.answer(f"⚠️ {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("send_code")
                await message.answer(f"⚠️ Ошибка входа: {exc}")
                return
            await state.set_state(LoginStates.code)
            await message.answer(hint)

        @router.message(StateFilter(LoginStates.code))
        async def on_code(message: Message, state: FSMContext) -> None:
            raw = (message.text or "").strip()
            if not raw or raw.startswith("/"):
                return
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
            except Exception as exc:  # noqa: BLE001
                logger.exception("confirm_code")
                await message.answer(f"⚠️ {exc}")
                return
            if reply == "NEED_PASSWORD":
                await state.set_state(LoginStates.password)
                await message.answer("Нужен пароль 2FA. Отправь его сюда.")
                return
            await state.clear()
            self.mark_authorized(self.phone_label())
            await message.answer(
                "✅ Вход выполнен. Трекер сканирует маркет.\nЖми /status",
                reply_markup=_MENU,
            )

        @router.message(StateFilter(LoginStates.password))
        async def on_password(message: Message, state: FSMContext) -> None:
            try:
                await auth.confirm_password(message.text or "")
            except ValueError as exc:
                await message.answer(f"⚠️ {exc}")
                return
            await state.clear()
            self.mark_authorized(self.phone_label())
            await message.answer(
                "✅ Вход выполнен (2FA). Жми /status",
                reply_markup=_MENU,
            )

        @router.message()
        async def fallback(message: Message) -> None:
            if self.authorized:
                await message.answer(
                    "Команды: /start и /status",
                    reply_markup=_MENU,
                )
            else:
                await message.answer("Нажми /start и отправь номер.")

        return router

    def phone_label(self) -> str:
        phone = self._auth.phone or ""
        return phone or "аккаунт"
