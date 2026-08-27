"""@jsjeigiejwhnewbot — вход, /status, /test (всегда онлайн)."""

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

logger = logging.getLogger("tracker_bot")


class LoginStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


def channel_file_path(data_dir: Path) -> Path:
    return data_dir / "tracker_channel_id.txt"


class ChannelStore:
    """Персистентный id канала."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._id: int | None = None
        self._ready = asyncio.Event()

    def load(self) -> int | None:
        if self._id is not None:
            return self._id
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            if raw and re.fullmatch(r"-?\d+", raw):
                self._id = int(raw)
                self._ready.set()
        except OSError:
            pass
        return self._id

    def get(self) -> int | None:
        return self._id

    def save(self, chat_id: int) -> None:
        self._id = int(chat_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(self._id), encoding="utf-8")
        self._ready.set()
        logger.info("Канал сохранён: %s → %s", self.path, self._id)

    async def wait(self, timeout: float = 60.0) -> int | None:
        if self._id is not None:
            return self._id
        self.load()
        if self._id is not None:
            return self._id
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self._id
        return self._id


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


def build_router(
    client: TelegramClient,
    auth: AuthFlow,
    store: ChannelStore,
    *,
    login_done: asyncio.Event | None = None,
    tracker_version: str = "",
    control: ControlBot | None = None,
) -> Router:
    router = Router()

    async def _authorized() -> bool:
        try:
            return await client.is_user_authorized()
        except Exception:  # noqa: BLE001
            return False

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        if await _authorized():
            cid = store.get() or store.load()
            ch = f"<code>{cid}</code>" if cid else "—"
            await message.answer(
                f"🤖 <b>Гифт-трекер</b> v{tracker_version}\n"
                f"Аккаунт: подключён ✅\n"
                f"Канал: {ch}\n\n"
                "<b>Команды:</b>\n"
                "/status — статус\n"
                "/test — тестовая карточка в канал\n"
                "/resetseen — сбросить seen (тест)",
            )
            if login_done and not login_done.is_set():
                login_done.set()
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
        cid = store.get() or store.load()
        if not await _authorized():
            await message.answer("❌ Не авторизован — /start")
            return
        me = await client.get_me()
        name = me.username or me.first_name or me.id
        rt = control.runtime if control else None
        cfg = rt.cfg if rt else None
        lines = [
            f"✅ Трекер v{tracker_version}",
            f"Аккаунт: {name}",
            f"Канал: <code>{cid or '—'}</code>",
        ]
        if cfg:
            lines.append(
                f"Диапазон: {int(cfg.min_stars)}–{int(cfg.max_stars)}⭐"
            )
            lines.append(
                f"Фильтры: RU={'да' if cfg.strict_ru else 'нет'} · "
                f"free={'строго' if cfg.strict_free else 'не платные'} · "
                f"lvl≤{getattr(cfg, 'max_account_level', 2)}"
            )
        if rt:
            lines.extend(
                [
                    f"Проходов: {rt.passes}",
                    f"Коллекций: {rt.collections_total or '—'} "
                    f"(parallel {rt.scan_parallel or 8})",
                    f"Последний скан: {rt.last_scan_batch} колл · "
                    f"{rt.last_scan_parsed} лотов · {rt.last_scan_elapsed}s",
                    f"Всего отправлено: {rt.posted_total}",
                    f"В очереди: {rt.queue_pending}",
                    f"Последний проход: +{rt.last_fresh} новых → {rt.last_posted} в очередь",
                    f"Отсев: ru−{rt.last_skip_ru} dm−{rt.last_skip_dm} "
                    f"dup−{rt.last_skip_dup} noseller−{rt.last_skip_noseller} "
                    f"lvl−{rt.last_skip_level}",
                    f"Seen лотов: {rt.seen_lots}",
                ]
            )
            if rt.last_scan_errors:
                lines.append(
                    f"⚠️ Ошибки API: {rt.last_scan_errors}"
                    + (f" — {rt.last_api_error[:120]}" if rt.last_api_error else "")
                )
        await message.answer("\n".join(lines))

    @router.message(Command("test"))
    async def cmd_test(message: Message) -> None:
        if not await _authorized():
            await message.answer("Сначала /start")
            return
        from tracker import DEFAULT_BOT_USERNAME

        if not control or not control.sender or not control.sender.chat_id:
            await message.answer("Канал ещё не готов — подожди запуска трекера.")
            return
        from market import Lot

        lot = Lot(
            id="test-post",
            title="Test Gift",
            number=1,
            stars=600.0,
            slug="TestGift-1",
            model="Test",
            seller="testuser",
            seller_id=1,
            free_dm=True,
            account_level=1,
            is_premium=False,
        )
        try:
            await control.sender.send(lot)
            await message.answer(
                f"✅ Тест отправлен в канал <code>{control.sender.chat_id}</code>"
            )
        except Exception as exc:  # noqa: BLE001
            await message.answer(
                f"❌ Не отправилось: {_esc(str(exc)[:200])}\n"
                f"Проверь: @{DEFAULT_BOT_USERNAME} — админ канала с правом публикации."
            )

    @router.message(Command("resetseen"))
    async def cmd_resetseen(message: Message) -> None:
        if not await _authorized():
            await message.answer("Сначала /start")
            return
        rt = control.runtime if control else None
        if not rt or not rt.state or not rt.state_path:
            await message.answer("Трекер ещё не запущен полностью.")
            return
        from tracker import save_state

        n = len(rt.state.get("seen", {}))
        rt.state["seen"] = {}
        save_state(rt.state_path, rt.state)
        rt.seen_lots = 0
        await message.answer(
            f"✅ Seen сброшен ({n} лотов). Новые листинги снова будут ловиться."
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
            await message.answer("🔒 Введи пароль 2FA:")
            return
        await state.clear()
        me = await client.get_me()
        name = f"@{me.username}" if me.username else (me.first_name or str(me.id))
        await message.answer(f"✅ Вход: {name}\nТрекер запускается…")
        if login_done:
            login_done.set()

    @router.message(StateFilter(LoginStates.password))
    async def got_password(message: Message, state: FSMContext) -> None:
        try:
            await auth.confirm_password(message.text or "")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ {exc}")
            return
        await state.clear()
        await message.answer("✅ Вход выполнен.\nТрекер запускается…")
        if login_done:
            login_done.set()

    return router


class ControlBot:
    def __init__(
        self,
        token: str,
        client: TelegramClient,
        session_file: str,
        store: ChannelStore,
        *,
        tracker_version: str = "",
    ) -> None:
        self.token = token
        self.client = client
        self.session_file = session_file
        self.store = store
        self.tracker_version = tracker_version
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._task: asyncio.Task | None = None
        self._login_done = asyncio.Event()
        self._auth = AuthFlow(client, session_file)
        self.runtime: Any | None = None
        self.sender: Any | None = None
        self.post_queue: Any | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        from tracker import DEFAULT_CHANNEL_ID

        if self.store.load() is None:
            self.store.save(DEFAULT_CHANNEL_ID)
        self._bot = Bot(
            token=self.token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher(storage=MemoryStorage())
        router = build_router(
            self.client,
            self._auth,
            self.store,
            login_done=self._login_done,
            tracker_version=self.tracker_version,
            control=self,
        )
        self._dp.include_router(router)
        info = await self._bot.get_me()
        logger.info("Control bot @%s запущен", info.username)
        self._task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="tracker-control-bot",
        )

    async def wait_login(self, timeout: float | None = None) -> None:
        if await self.client.is_user_authorized():
            self._login_done.set()
            return
        logger.info("Жду вход через бота…")
        if timeout is None:
            await self._login_done.wait()
        else:
            await asyncio.wait_for(self._login_done.wait(), timeout=timeout)

    async def stop(self) -> None:
        if self._dp and self._bot:
            try:
                await self._dp.stop_polling()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("bot task: %s", exc)
        if self._bot:
            await self._bot.session.close()
