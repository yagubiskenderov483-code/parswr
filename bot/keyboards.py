from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.categories import CATEGORIES


def categories_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for cat in CATEGORIES:
        mark = "✅" if cat.key in selected else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {cat.label}",
                    callback_data=f"cat:{cat.key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔕 Выключить все", callback_data="cat:off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Категории"), KeyboardButton(text="📊 Мои подписки")],
            [KeyboardButton(text="⚡ Статус"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )
