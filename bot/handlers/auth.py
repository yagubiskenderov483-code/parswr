from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.keyboards import MARKET_PICK_TITLES, markets_keyboard
from bot.models import MarketName
from bot.services.auth import AuthService
from bot.services.monitor import MonitorService

router = Router(name="auth")

# Markets that need Telethon login
_AUTH_MARKETS = {MarketName.MRKT.value, MarketName.TELEGRAM.value}


class AuthStates(StatesGroup):
    choosing_markets = State()
    phone = State()
    code = State()
    password = State()


def _phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _markets_help() -> str:
    return (
        "🎁 <b>Старт</b>\n\n"
        "Где парсить новые лоты?\n\n"
        "• <b>Все</b> — Tonnel + Portal + MRKT + Telegram\n"
        "• <b>Tonnel / Portal</b> — сразу, без входа\n"
        "• <b>MRKT / Telegram</b> — нужен вход в аккаунт\n\n"
        "Выбери один вариант:"
    )


def _resolve_selection(choice: str) -> set[str]:
    if choice == "all":
        return {m.value for m in MarketName}
    if choice in MarketName._value2member_map_:
        return {choice}
    return {m.value for m in MarketName}


def _needs_auth(selected: set[str]) -> bool:
    return bool(selected & _AUTH_MARKETS)


async def _start_with_markets(
    message: Message,
    monitor: MonitorService,
    selected: set[str],
) -> None:
    text = await monitor.start(message.from_user.id, selected_markets=selected)
    titles = ", ".join(MARKET_PICK_TITLES.get(m, m) for m in sorted(selected))
    await message.answer(
        f"{text}\n\n"
        f"Маркеты: <b>{titles}</b>\n"
        "Стоп — в синем меню бота (команда /stop).",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, monitor: MonitorService) -> None:
    await state.clear()
    if monitor.is_running:
        await monitor.stop()
    await state.set_state(AuthStates.choosing_markets)
    await message.answer(
        _markets_help(),
        reply_markup=markets_keyboard(),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext, monitor: MonitorService) -> None:
    await state.clear()
    text = await monitor.stop()
    await message.answer(
        f"{text}\n\nНажми <b>Старт</b>, чтобы выбрать маркеты снова.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext, monitor: MonitorService) -> None:
    await cmd_start(message, state, monitor)


@router.callback_query(AuthStates.choosing_markets, F.data.startswith("marketpick:"))
async def on_market_pick(
    callback: CallbackQuery,
    state: FSMContext,
    auth: AuthService,
    monitor: MonitorService,
) -> None:
    await callback.answer()
    choice = (callback.data or "").split(":", 1)[-1]
    selected = _resolve_selection(choice)
    label = MARKET_PICK_TITLES.get(choice, choice)

    await state.update_data(selected_markets=list(selected), market_label=label)

    if _needs_auth(selected) and not await auth.is_authorized():
        await state.set_state(AuthStates.phone)
        # Start public markets immediately if "all" was chosen
        public = selected - _AUTH_MARKETS
        if public:
            parsers = await auth.build_authorized_parsers()
            # Without login build_authorized_parsers still returns Tonnel+Portal
            await monitor.reload_parsers(parsers)
            await monitor.start(callback.from_user.id, selected_markets=public)
            await callback.message.answer(
                f"✅ Выбрано: <b>{label}</b>\n"
                "Tonnel/Portal уже парсятся.\n\n"
                "Для MRKT / Telegram нужен вход.\n"
                "📱 Номер в формате <code>+79991234567</code>\n"
                "Или ❌ Отмена — останутся только публичные маркеты.",
                reply_markup=_phone_kb(),
            )
            return

        await callback.message.answer(
            f"✅ Выбрано: <b>{label}</b>\n\n"
            "Для этого маркета нужен вход в Telegram.\n"
            "📱 Номер в формате <code>+79991234567</code>",
            reply_markup=_phone_kb(),
        )
        return

    # Authorized or public-only markets
    if await auth.is_authorized():
        parsers = await auth.build_authorized_parsers()
        await monitor.reload_parsers(parsers)
    await state.clear()
    # Use message from callback for answer helper
    text = await monitor.start(callback.from_user.id, selected_markets=selected)
    await callback.message.answer(
        f"{text}\n\nСтоп — в синем меню бота (/stop).",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(AuthStates.phone), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.code), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.password), F.text == "❌ Отмена")
async def cancel_auth(message: Message, state: FSMContext, monitor: MonitorService) -> None:
    data = await state.get_data()
    await state.clear()
    if monitor.is_running:
        await message.answer(
            "Ок, без полного входа. Парсинг публичных маркетов продолжается.\n"
            "Стоп — /stop.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    # Fall back to public markets from selection
    selected = set(data.get("selected_markets") or [])
    public = selected - _AUTH_MARKETS or {MarketName.TONNEL.value, MarketName.PORTAL.value}
    text = await monitor.start(message.from_user.id, selected_markets=public)
    await message.answer(
        f"Ок, без входа.\n{text}\nСтоп — /stop.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(AuthStates.phone))
async def got_phone(message: Message, state: FSMContext, auth: AuthService) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    try:
        reply = await auth.send_code(text)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Не удалось отправить код: {exc}")
        return
    await state.set_state(AuthStates.code)
    await message.answer(f"{reply}\n\nПришли код:", reply_markup=_phone_kb())


@router.message(StateFilter(AuthStates.code))
async def got_code(
    message: Message,
    state: FSMContext,
    auth: AuthService,
    monitor: MonitorService,
) -> None:
    code = (message.text or "").strip()
    try:
        result = await auth.confirm_code(code)
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

    await _finish_login(message, state, auth, monitor)


@router.message(StateFilter(AuthStates.password))
async def got_password(
    message: Message,
    state: FSMContext,
    auth: AuthService,
    monitor: MonitorService,
) -> None:
    try:
        await auth.confirm_password(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Ошибка 2FA: {exc}")
        return

    await _finish_login(message, state, auth, monitor)


async def _finish_login(
    message: Message,
    state: FSMContext,
    auth: AuthService,
    monitor: MonitorService,
) -> None:
    data = await state.get_data()
    selected = set(data.get("selected_markets") or [m.value for m in MarketName])
    label = data.get("market_label") or "выбранные маркеты"
    await state.clear()

    parsers = await auth.build_authorized_parsers()
    await monitor.reload_parsers(parsers)
    if monitor.is_running:
        await monitor.stop()

    text = await monitor.start(message.from_user.id, selected_markets=selected)
    await message.answer(
        f"✅ Вошли как <b>{auth.authorized_as}</b>\n"
        f"Запуск: <b>{label}</b>\n\n"
        f"{text}\n"
        "Стоп — /stop.",
        reply_markup=ReplyKeyboardRemove(),
    )
