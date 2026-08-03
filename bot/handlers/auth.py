from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from bot.keyboards import main_menu
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
    )


async def begin_auth(message: Message, state: FSMContext) -> None:
    await state.set_state(AuthStates.phone)
    await message.answer(
        "🔐 <b>Авторизация Telegram-аккаунта</b>\n\n"
        "Нужна, чтобы парсить MRKT / Portal / Telegram Market.\n"
        "Пришли номер в формате:\n"
        "<code>+79991234567</code>",
        reply_markup=_phone_kb(),
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    auth: AuthService,
    monitor: MonitorService,
) -> None:
    await state.clear()
    if await auth.is_authorized():
        await message.answer(
            f"✅ Аккаунт уже авторизован: <b>{auth.authorized_as}</b>\n\n"
            "🎁 Парсю новые лоты с Tonnel / Portal / MRKT / Telegram Market.\n"
            "Нажми ▶️ чтобы начать.",
            reply_markup=main_menu(),
        )
        # Ensure parsers are wired with tokens
        if not any(p.last_count or p.last_error is None for p in monitor.parsers):
            pass
        return
    await begin_auth(message, state)


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext, auth: AuthService) -> None:
    await state.clear()
    await begin_auth(message, state)


@router.message(StateFilter(AuthStates.phone), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.code), F.text == "❌ Отмена")
@router.message(StateFilter(AuthStates.password), F.text == "❌ Отмена")
async def cancel_auth(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Авторизация отменена. Без неё будет работать только Tonnel.\n"
        "Чтобы войти позже: /login",
        reply_markup=main_menu(),
    )


@router.message(StateFilter(AuthStates.phone))
async def got_phone(message: Message, state: FSMContext, auth: AuthService) -> None:
    phone = (message.text or "").strip()
    try:
        text = await auth.send_code(phone)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Не удалось отправить код: {exc}")
        return
    await state.set_state(AuthStates.code)
    await message.answer(
        f"{text}\n\nПришли код из Telegram/SMS:",
        reply_markup=_phone_kb(),
    )


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
        await message.answer(
            "🔒 На аккаунте включён 2FA.\nПришли облачный пароль:",
            reply_markup=_phone_kb(),
        )
        return

    await state.clear()
    parsers = await auth.build_authorized_parsers()
    await monitor.reload_parsers(parsers)
    await message.answer(
        f"✅ Готово! Вошли как <b>{auth.authorized_as}</b>\n\n"
        "Маркеты подключены. Можно запускать парсинг.",
        reply_markup=main_menu(),
    )


@router.message(StateFilter(AuthStates.password))
async def got_password(
    message: Message,
    state: FSMContext,
    auth: AuthService,
    monitor: MonitorService,
) -> None:
    password = message.text or ""
    try:
        await auth.confirm_password(password)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Ошибка 2FA: {exc}")
        return

    await state.clear()
    parsers = await auth.build_authorized_parsers()
    await monitor.reload_parsers(parsers)
    await message.answer(
        f"✅ Готово! Вошли как <b>{auth.authorized_as}</b>\n\n"
        "Маркеты подключены. Можно запускать парсинг.",
        reply_markup=main_menu(),
    )
