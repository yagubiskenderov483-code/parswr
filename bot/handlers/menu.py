from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from bot.database import session_scope
from bot.database.repositories import get_or_create_settings, update_settings
from bot.keyboards import price_presets, settings_keyboard
from bot.services.rates import RateService
from bot.services.stats import build_stats_text

router = Router(name="main")


def _settings_text(cfg) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"💰 Диапазон: <b>{int(cfg.min_stars)}–{int(cfg.max_stars)} ⭐</b>\n"
        f"⏱ Интервал: <b>{cfg.poll_interval:g} сек</b>\n"
        f"🔔 Уведомления: <b>{'вкл' if cfg.notifications_enabled else 'выкл'}</b>"
    )


@router.message(Command("stats"))
async def show_stats(message: Message) -> None:
    await message.answer(await build_stats_text(), reply_markup=ReplyKeyboardRemove())


@router.message(Command("rates"))
async def refresh_rates(message: Message, rates: RateService) -> None:
    current = await rates.refresh()
    await message.answer(
        "🔄 Курсы обновлены\n\n"
        f"1 TON = <b>${current.ton_usd:.4f}</b>\n"
        f"1 ⭐ = <b>${current.stars_usd:.5f}</b>\n"
        f"1 TON ≈ <b>{max(current.ton_to_stars, 300):.0f} ⭐</b>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("settings"))
async def show_settings(message: Message) -> None:
    async with session_scope() as session:
        cfg = await get_or_create_settings(session, message.from_user.id)
        text = _settings_text(cfg)
        kb = settings_keyboard(cfg)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "settings:back")
async def settings_back(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        cfg = await get_or_create_settings(session, callback.from_user.id)
        text = _settings_text(cfg)
        kb = settings_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.in_({"settings:min", "settings:max", "settings:interval"}))
async def settings_choose_value(callback: CallbackQuery) -> None:
    kind = callback.data.split(":")[1]
    titles = {
        "min": "Минимальная цена ⭐",
        "max": "Максимальная цена ⭐",
        "interval": "Интервал",
    }
    await callback.message.edit_text(titles[kind], reply_markup=price_presets(kind))
    await callback.answer()


@router.callback_query(F.data.startswith("set:"))
async def settings_set_value(callback: CallbackQuery) -> None:
    _, kind, value_raw = callback.data.split(":")
    value = float(value_raw)
    fields: dict = {}
    if kind == "min":
        fields["min_stars"] = value
    elif kind == "max":
        fields["max_stars"] = value
    elif kind == "interval":
        fields["poll_interval"] = max(0.15, value)

    async with session_scope() as session:
        cfg = await update_settings(session, callback.from_user.id, **fields)
        if cfg.min_stars > cfg.max_stars:
            cfg = await update_settings(
                session,
                callback.from_user.id,
                min_stars=min(cfg.min_stars, cfg.max_stars),
                max_stars=max(cfg.min_stars, cfg.max_stars),
            )
        text = _settings_text(cfg)
        kb = settings_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("OK")


@router.callback_query(F.data == "settings:notify")
async def toggle_notify(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        cfg = await get_or_create_settings(session, callback.from_user.id)
        cfg = await update_settings(
            session,
            callback.from_user.id,
            notifications_enabled=not cfg.notifications_enabled,
        )
        text = _settings_text(cfg)
        kb = settings_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("OK")
