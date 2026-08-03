from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.database import session_scope
from bot.database.repositories import get_or_create_settings, update_settings
from bot.keyboards import main_menu, price_presets, settings_keyboard
from bot.services.auth import AuthService
from bot.services.monitor import MonitorService
from bot.services.rates import RateService
from bot.services.stats import build_stats_text

router = Router(name="main")


def _settings_text(cfg) -> str:
    markets = []
    if cfg.market_tonnel:
        markets.append("Tonnel")
    if cfg.market_mrkt:
        markets.append("MRKT")
    if cfg.market_portal:
        markets.append("Portal")
    if cfg.market_telegram:
        markets.append("Telegram")
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"💰 Диапазон: <b>{int(cfg.min_stars)}–{int(cfg.max_stars)} ⭐</b>\n"
        f"⏱ Интервал: <b>{cfg.poll_interval:g} сек</b>\n"
        f"🔔 Уведомления: <b>{'вкл' if cfg.notifications_enabled else 'выкл'}</b>\n"
        f"🌐 Маркеты: <b>{', '.join(markets) or 'нет'}</b>\n\n"
        "Категории:\n"
        "Easy 2–5к · Medium 5–10к · Hard 15–30к\n"
        "Impossible 30–65к · Unreal 65–100к"
    )


@router.message(F.text == "▶️ Запустить парсинг")
@router.message(Command("start_parse"))
async def start_parsing(
    message: Message,
    monitor: MonitorService,
    auth: AuthService,
) -> None:
    if not await auth.is_authorized():
        await message.answer(
            "⚠️ Сначала авторизуйся через /login (номер + код),\n"
            "иначе полноценно работает только Tonnel.",
            reply_markup=main_menu(),
        )
    text = await monitor.start(message.from_user.id)
    await message.answer(text, reply_markup=main_menu())


@router.message(F.text == "⏹ Остановить парсинг")
@router.message(Command("stop_parse"))
async def stop_parsing(message: Message, monitor: MonitorService) -> None:
    text = await monitor.stop()
    await message.answer(text, reply_markup=main_menu())


@router.message(F.text == "📊 Статистика")
@router.message(Command("stats"))
async def show_stats(message: Message) -> None:
    await message.answer(await build_stats_text(), reply_markup=main_menu())


@router.message(F.text == "🔄 Обновить курсы валют")
@router.message(Command("rates"))
async def refresh_rates(message: Message, rates: RateService) -> None:
    current = await rates.refresh()
    await message.answer(
        "🔄 Курсы обновлены\n\n"
        f"1 TON = <b>${current.ton_usd:.4f}</b>\n"
        f"1 ⭐ = <b>${current.stars_usd:.5f}</b>\n"
        f"1 TON ≈ <b>{current.ton_to_stars:.2f} ⭐</b>",
        reply_markup=main_menu(),
    )


@router.message(F.text == "⚙️ Настройки")
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
        "min": "Выбери минимальную цену ⭐",
        "max": "Выбери максимальную цену ⭐",
        "interval": "Выбери интервал проверки",
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
        fields["poll_interval"] = max(1.0, value)

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
    await callback.answer("Сохранено")


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
    await callback.answer("Уведомления переключены")


@router.callback_query(F.data.startswith("settings:market:"))
async def toggle_market(callback: CallbackQuery) -> None:
    market = callback.data.split(":")[-1]
    field_map = {
        "tonnel": "market_tonnel",
        "mrkt": "market_mrkt",
        "portal": "market_portal",
        "telegram": "market_telegram",
    }
    field = field_map[market]
    async with session_scope() as session:
        cfg = await get_or_create_settings(session, callback.from_user.id)
        cfg = await update_settings(
            session,
            callback.from_user.id,
            **{field: not getattr(cfg, field)},
        )
        text = _settings_text(cfg)
        kb = settings_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Маркет переключён")


@router.callback_query(F.data == "settings:rates")
async def settings_rates(callback: CallbackQuery, rates: RateService) -> None:
    current = await rates.refresh()
    await callback.answer(
        f"TON ${current.ton_usd:.2f} | ⭐ ${current.stars_usd:.4f}",
        show_alert=True,
    )
