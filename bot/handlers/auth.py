from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from bot.services.auth import AuthService
from bot.services.monitor import MonitorService

router = Router(name="auth")


class AuthStates(StatesGroup):
    phone = State()
    code = State()
    password = State()


def _phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _start_parsing_now(message: Message, monitor: MonitorService) -> None:
    if monitor.is_running:
        await message.answer(
            "♻️ Парсинг уже идёт — жду новые лоты…",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    text = await monitor.start(message.from_user.id)
    await message.answer(
        f"{text}\n\nМониторю Tonnel / MRKT / Portal / Telegram Market.\n"
        "Как только выйдет новый лот в твоём диапазоне — пришлю сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    auth: AuthService,
    monitor: MonitorService,
) -> None:
    await state.clear()

    # Always remove old reply keyboards
    if await auth.is_authorized():
        await message.answer(
            f"✅ Вход: <b>{auth.authorized_as}</b>\nЗапускаю парсинг…",
            reply_markup=ReplyKeyboardRemove(),
        )
        parsers = await auth.build_authorized_parsers()
        await monitor.reload_parsers(parsers)
        await _start_parsing_now(message, monitor)
        return

    # Without login — start Tonnel immediately, then ask phone for other markets
    await message.answer(
        "🎁 <b>Старт</b>\n\n"
        "Сейчас включу парсинг Tonnel (без входа).\n"
        "Чтобы ловить MRKT / Portal / Telegram Market — пришли номер:",
        reply_markup=_phone_kb(),
    )
    await _start_parsing_now(message, monitor)
    await state.set_state(AuthStates.phone)
    await message.answer(
        "📱 Номер в формате <code>+79991234567</code>\n"
        "Или нажми ❌ Отмена — останется только Tonnel.",
        reply_markup=_phone_kb(),
    )


@router.message(StateFilter(AuthStates.phone), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.code), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.password), F.text == "❌ Отмена")
async def cancel_auth(message: Message, state: FSMContext, monitor: MonitorService) -> None:
    await state.clear()
    await message.answer(
        "Ок, без входа. Парсинг Tonnel продолжается.",
        reply_markup=ReplyKeyboardRemove(),
    )
    if not monitor.is_running:
        await _start_parsing_now(message, monitor)


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

    await state.clear()
    parsers = await auth.build_authorized_parsers()
    await monitor.reload_parsers(parsers)
    # Restart monitor with full market set
    if monitor.is_running:
        await monitor.stop()
    await message.answer(
        f"✅ Вошли как <b>{auth.authorized_as}</b>\nПодключил все маркеты, запускаю…",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _start_parsing_now(message, monitor)


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

    await state.clear()
    parsers = await auth.build_authorized_parsers()
    await monitor.reload_parsers(parsers)
    if monitor.is_running:
        await monitor.stop()
    await message.answer(
        f"✅ Вошли как <b>{auth.authorized_as}</b>\nЗапускаю парсинг…",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _start_parsing_now(message, monitor)


# Keep /login as alias to /start auth part
@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext, auth: AuthService, monitor: MonitorService) -> None:
    await cmd_start(message, state, auth, monitor)
