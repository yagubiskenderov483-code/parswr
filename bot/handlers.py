from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from bot.categories import CATEGORIES, CATEGORY_BY_KEY
from bot.keyboards import categories_keyboard, main_menu
from bot.storage import SubscriptionStore

router = Router()


def _subs_text(store: SubscriptionStore, user_id: int) -> str:
    keys = store.get(user_id)
    if not keys:
        return "Подписок нет. Нажми «📂 Категории» и выбери диапазон."
    lines = ["Активные категории:"]
    for cat in CATEGORIES:
        if cat.key in keys:
            lines.append(f"• {cat.label}")
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message, subs: SubscriptionStore) -> None:
    text = (
        "⚡ <b>Новые лоты Gift-маркетов</b>\n\n"
        "Слушаю <b>Tonnel / MRKT / Portals</b> и сразу кидаю свежие лоты по цене:\n"
        "🟢 Лёгкий — 2 000–5 000 ⭐\n"
        "🟡 Средний — 5 000–10 000 ⭐\n"
        "🟠 Сложный — 10 000–20 000 ⭐\n"
        "🔴 Топ — 20 000–60 000 ⭐\n\n"
        "Выбери категории — и лоты будут прилетать моментально."
    )
    await message.answer(text, reply_markup=main_menu())
    await message.answer(
        "Отметь ценовые категории:",
        reply_markup=categories_keyboard(subs.get(message.from_user.id)),
    )


@router.message(Command("categories"))
@router.message(F.text == "📂 Категории")
async def show_categories(message: Message, subs: SubscriptionStore) -> None:
    await message.answer(
        "Отметь категории:",
        reply_markup=categories_keyboard(subs.get(message.from_user.id)),
    )


@router.message(Command("subs"))
@router.message(F.text == "📊 Мои подписки")
async def show_subs(message: Message, subs: SubscriptionStore) -> None:
    await message.answer(_subs_text(subs, message.from_user.id), reply_markup=main_menu())


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def show_help(message: Message) -> None:
    await message.answer(
        "1. «📂 Категории» → включи диапазон Stars\n"
        "2. Бот опрашивает Tonnel / MRKT / Portals каждые пару секунд\n"
        "3. Новый лот в твоём диапазоне → сразу сообщение\n\n"
        "Команды: /start /categories /subs /status",
        reply_markup=main_menu(),
    )


@router.message(Command("status"))
@router.message(F.text == "⚡ Статус")
async def show_status(message: Message, monitor_status: dict) -> None:
    per = monitor_status.get("per_market") or {}
    per_line = ", ".join(f"{k}:{v}" for k, v in per.items()) or "—"
    err = monitor_status.get("last_error")
    text = (
        f"Маркеты: {', '.join(monitor_status.get('markets') or [])}\n"
        f"Интервал: {monitor_status.get('poll_interval')} сек\n"
        f"Курс: 1 TON ≈ {monitor_status.get('stars_per_ton')} ⭐\n"
        f"Выборка: {monitor_status.get('last_fetch_count', 0)} ({per_line})\n"
        f"Новых с запуска: {monitor_status.get('new_lots_total', 0)}\n"
        f"Ошибка: {err if err else 'нет'}"
    )
    await message.answer(text, reply_markup=main_menu())


@router.callback_query(F.data.startswith("cat:"))
async def toggle_category(callback: CallbackQuery, subs: SubscriptionStore) -> None:
    key = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if key == "off":
        subs.set(user_id, set())
        await callback.answer("Все выключены")
        await callback.message.edit_reply_markup(reply_markup=categories_keyboard(set()))
        return

    if key not in CATEGORY_BY_KEY:
        await callback.answer("Неизвестная категория", show_alert=True)
        return

    enabled = subs.toggle(user_id, key)
    cat = CATEGORY_BY_KEY[key]
    await callback.answer(f"{'Вкл' if enabled else 'Выкл'}: {cat.title}")
    await callback.message.edit_reply_markup(
        reply_markup=categories_keyboard(subs.get(user_id))
    )
