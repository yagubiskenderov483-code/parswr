from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.database.models import AppSettings


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Запустить парсинг"), KeyboardButton(text="⏹ Остановить парсинг")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🔄 Обновить курсы валют"), KeyboardButton(text="🔐 Войти")],
            [KeyboardButton(text="🚀 Старт")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard(cfg: AppSettings) -> InlineKeyboardMarkup:
    def mark(enabled: bool) -> str:
        return "✅" if enabled else "❌"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Min: {int(cfg.min_stars)} ⭐",
                    callback_data="settings:min",
                ),
                InlineKeyboardButton(
                    text=f"Max: {int(cfg.max_stars)} ⭐",
                    callback_data="settings:max",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"Интервал: {cfg.poll_interval:g}с",
                    callback_data="settings:interval",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{mark(cfg.notifications_enabled)} Уведомления",
                    callback_data="settings:notify",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{mark(cfg.market_tonnel)} Tonnel",
                    callback_data="settings:market:tonnel",
                ),
                InlineKeyboardButton(
                    text=f"{mark(cfg.market_mrkt)} MRKT",
                    callback_data="settings:market:mrkt",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{mark(cfg.market_portal)} Portal",
                    callback_data="settings:market:portal",
                ),
                InlineKeyboardButton(
                    text=f"{mark(cfg.market_telegram)} Telegram",
                    callback_data="settings:market:telegram",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить курсы",
                    callback_data="settings:rates",
                )
            ],
        ]
    )


def price_presets(kind: str) -> InlineKeyboardMarkup:
    presets = {
        "min": [2000, 5000, 10000, 15000, 30000],
        "max": [5000, 10000, 30000, 65000, 100000],
        "interval": [1, 2, 3, 5, 10],
    }
    rows: list[list[InlineKeyboardButton]] = []
    for value in presets[kind]:
        suffix = "⭐" if kind != "interval" else "с"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{value} {suffix}",
                    callback_data=f"set:{kind}:{value}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
