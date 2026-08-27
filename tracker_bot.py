"""@markskskdbot — вход, /setchannel, /channels, /status (всегда онлайн)."""

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
from telethon.utils import get_peer_id

logger = logging.getLogger("tracker_bot")


class LoginStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


def channel_file_path(data_dir: Path) -> Path:
    return data_dir / "tracker_channel_id.txt"


class ChannelStore:
    """Персистентный id канала + ожидание /setchannel."""

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


async def parse_channel_arg(client: TelegramClient, raw: str) -> int:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Пусто")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})$", text)
    handle = m.group(1) if m else text.lstrip("@").strip()
    if not handle or handle.startswith("+"):
        raise ValueError("Формат: @channel или -100…")
    entity = await client.get_entity(handle)
    return get_peer_id(entity)


async def list_user_channels(client: TelegramClient, limit: int = 30) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    async for dlg in client.iter_dialogs(limit=limit):
        if not getattr(dlg, "is_channel", False):
            continue
        name = (dlg.name or "").strip() or "?"
        out.append((name, get_peer_id(dlg.entity)))
    return out


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
            ch = f"<code>{cid}</code>" if cid else "не задан"
            await message.answer(
                f"🤖 <b>Гифт-трекер</b> v{tracker_version}\n"
                f"Аккаунт: подключён ✅\n"
                f"Канал: {ch}\n\n"
                "<b>Команды:</b>\n"
                "/setchannel @username — канал для постов\n"
                "/setchannel -100… — id канала\n"
                "/channels — список твоих каналов\n"
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
            "<code>+79991234567</code>\n\n"
            "После входа: /setchannel @имя_канала",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(Command("setchannel"))
    async def cmd_setchannel(message: Message) -> None:
        if not await _authorized():
            await message.answer("Сначала /start и войди в аккаунт.")
            return
        arg = (message.text or "").split(maxsplit=1)
        if len(arg) < 2:
            await message.answer(
                "Использование:\n"
                "<code>/setchannel @mychannel</code>\n"
                "<code>/setchannel -100123456789</code>"
            )
            return
        try:
            cid = await parse_channel_arg(client, arg[1])
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ Не нашёл канал: {exc}")
            return
        store.save(cid)
        if control:
            if control.runtime is not None:
                control.runtime.channel_id = cid
            if control.sender is not None:
                control.sender.chat_id = cid
        await message.answer(f"✅ Канал задан: <code>{cid}</code>")

    @router.message(Command("channels"))
    async def cmd_channels(message: Message) -> None:
        if not await _authorized():
            await message.answer("Сначала /start")
            return
        try:
            rows = await list_user_channels(client)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ {exc}")
            return
        if not rows:
            await message.answer(
                "Каналов в диалогах нет. Добавь аккаунт в канал и /channels снова."
            )
            return
        lines = [f"• <b>{n}</b> → <code>{cid}</code>" for n, cid in rows[:25]]
        await message.answer(
            "Твои каналы (скопируй id или /setchannel @name):\n" + "\n".join(lines)
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
            f"Канал: <code>{cid or 'не задан'}</code>",
        ]
        if cfg:
            lines.append(
                f"Диапазон: {int(cfg.min_stars)}–{int(cfg.max_stars)}⭐"
            )
            loh = getattr(cfg, "loh_mode", True)
            max_gifts = getattr(cfg, "max_gifts_count", 3)
            lines.append(
                f"Фильтры: RU={'да' if cfg.strict_ru else 'нет'} · "
                f"free={'строго' if cfg.strict_free else 'не платные'} · "
                f"лохи={'да' if loh else 'нет'} · "
                f"lvl≤{getattr(cfg, 'max_account_level', 2)} · gifts≤{max_gifts}"
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
                    f"lvl−{rt.last_skip_level} premium−{rt.last_skip_premium} "
                    f"pro−{rt.last_skip_pro}",
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
        if not control or not control.sender or not control.sender.chat_id:
            await message.answer("Канал не задан — /setchannel @channel")
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
                "Проверь: @markskskdbot — админ канала с правом публикации."
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
        await message.answer(
            f"✅ Вход: {name}\n"
            "Теперь задай канал:\n"
            "<code>/setchannel @имя_канала</code>\n"
            "или <code>/channels</code> — список"
        )
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
        await message.answer(
            "✅ Вход выполнен.\n"
            "Задай канал: <code>/setchannel @имя</code> или /channels"
        )
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
