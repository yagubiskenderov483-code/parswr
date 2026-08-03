"""
Бот: Telegram Market · только лоты за Stars.

- Без файла сессии на диске (RAM StringSession)
- Если НЕ вошёл → /start просит номер+код
- Если УЖЕ вошёл → /start сразу жжёт парсинг
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
        self.logged_in = False
        self.account_name = ""

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

    async def start_monitor(self, user_id: int) -> str:
        if not self.logged_in:
            raise RuntimeError("Сначала авторизация (номер + код).")
        if self.running:
            await self.stop_monitor()
        self.owner_id = user_id
        self.running = True
        self._seen.clear()
        self._task = asyncio.create_task(self._loop(), name="market-loop")
        return (
            "▶️ <b>Парсинг запущен</b>\n"
            f"Акк: <b>{self.account_name}</b>\n"
            f"Диапазон: <b>{int(self.min_stars)}–{int(self.max_stars)} ⭐</b>\n"
            "Жгу свежие лоты Telegram Market (~1 час)…"
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
        for k in [k for k, ts in self._seen.items() if now - ts > FRESH_WINDOW_SEC]:
            del self._seen[k]

    async def _loop(self) -> None:
        primed = False
        ticks = 0
        # First tick: full market snapshot (seed), then hot waves forever
        while self.running:
            started = time.monotonic()
            try:
                if not primed:
                    lots = await self.market.fetch_newest(per_collection=creds.PER_COLLECTION)
                    now = time.monotonic()
                    for lot in lots:
                        self._seen[lot.id] = now
                    primed = True
                    in_range = [lot for lot in lots if self._in_price(lot)]
                    preview = in_range[: creds.PREVIEW_LOTS]
                    if self.owner_id and self.bot:
                        await self.bot.send_message(
                            self.owner_id,
                            "📡 Парсер живой · Telegram Market (Stars)\n"
                            f"Коллекций просканировано, в диапазоне: <b>{len(in_range)}</b>\n"
                            f"Кидаю топ-{len(preview)}, дальше только новые выставления:",
                        )
                    for lot in preview:
                        await self._notify(lot)
                    logger.info("primed lots=%s in_range=%s", len(lots), len(in_range))
                else:
                    self._purge_old_seen()
                    fresh_total = 0
                    scanned = 0
                    async for chunk in self.market.iter_wave(
                        per_collection=creds.PER_COLLECTION,
                        batch_size=creds.WAVE_BATCH,
                    ):
                        if not self.running:
                            break
                        scanned += len(chunk)
                        now = time.monotonic()
                        for lot in chunk:
                            if lot.id in self._seen:
                                continue
                            self._seen[lot.id] = now
                            if not self._in_price(lot):
                                continue
                            fresh_total += 1
                            logger.info(
                                "NEW %.0f⭐ [%s] %s",
                                lot.stars,
                                lot.category,
                                lot.display,
                            )
                            await self._notify(lot)

                    if ticks % 10 == 0:
                        logger.info(
                            "wave#%s scanned=%s fresh=%s seen=%s",
                            ticks,
                            scanned,
                            fresh_total,
                            len(self._seen),
                        )
                ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("poll failed: %s", exc)

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.02, creds.POLL_INTERVAL - elapsed))

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


def wipe_disk_junk() -> None:
    """Сносит старые .session/.db с диска — автоподхвата акка нет."""
    root = Path(__file__).resolve().parent
    data = root / "data"
    data.mkdir(exist_ok=True)
    removed: list[str] = []
    for folder in (data, root):
        for pattern in ("*session*", "*.db", "*.db-*", "*.sqlite*"):
            for path in folder.glob(pattern):
                if path.is_file():
                    try:
                        path.unlink()
                        removed.append(str(path))
                    except OSError:
                        pass
    old_pkg = root / "bot"
    if old_pkg.is_dir():
        shutil.rmtree(old_pkg, ignore_errors=True)
        removed.append(str(old_pkg))
    if removed:
        logger.info("Wiped disk junk: %s", removed)


async def _ask_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(AuthStates.phone)
    await message.answer(
        "🎁 <b>Telegram Market · лоты за ⭐</b>\n\n"
        "Нужен вход (номер → код), потом /start сразу парсит.\n\n"
        "📱 Номер: <code>+79991234567</code>",
        reply_markup=_phone_kb(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()

    # Уже авторизован в этой сессии процесса → сразу парсинг
    if app.logged_in:
        try:
            text = await app.start_monitor(message.from_user.id)
        except RuntimeError as exc:
            await message.answer(f"⚠️ {exc}")
            await _ask_phone(message, state)
            return
        await message.answer(
            f"{text}\n\nСтоп — /stop · сменить акк — /logout",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Не вошёл — просим номер (старый диск-акк не подтягиваем)
    wipe_disk_junk()
    await _ask_phone(message, state)


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = await app.stop_monitor()
    await message.answer(
        f"{text}\n/start — снова парсить (если уже вошёл).",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    await app.reset_auth()
    await _ask_phone(message, state)


@router.message(StateFilter(AuthStates.phone), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.code), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.password), F.text == "❌ Отмена")
async def cancel_auth(message: Message, state: FSMContext) -> None:
    await state.clear()
    await app.reset_auth()
    await message.answer(
        "Отменено. Без входа парсинг не стартует.\n/start — заново.",
        reply_markup=ReplyKeyboardRemove(),
    )


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
        f"✅ Вошли как <b>{app.account_name}</b>\n\n{text}\n"
        "Дальше /start сразу парсит. Стоп — /stop.",
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
        f"✅ Вошли как <b>{app.account_name}</b>\n\n{text}\n"
        "Дальше /start сразу парсит. Стоп — /stop.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def main() -> None:
    wipe_disk_junk()
    bot = Bot(
        token=creds.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    app.bot = bot
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Старт парсинга / вход"),
            BotCommand(command="stop", description="Стоп"),
            BotCommand(command="logout", description="Сменить аккаунт"),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Ready | RAM session | /start parses if logged in")
    try:
        await dp.start_polling(bot)
    finally:
        await app.stop_monitor()
        if app.client.is_connected():
            await app.client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
