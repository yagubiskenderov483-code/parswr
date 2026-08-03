from __future__ import annotations

from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.database.models import AppSettings
from bot.models import MarketName


def main_menu() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Старт"),
        BotCommand(command="stop", description="Стоп"),
    ]


def markets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Все маркеты", callback_data="marketpick:all")],
            [
                InlineKeyboardButton(text="Tonnel", callback_data="marketpick:tonnel"),
                InlineKeyboardButton(text="MRKT", callback_data="marketpick:mrkt"),
            ],
            [
                InlineKeyboardButton(text="Portal", callback_data="marketpick:portal"),
                InlineKeyboardButton(text="Telegram", callback_data="marketpick:telegram_market"),
            ],
        ]
    )


def settings_keyboard(cfg: AppSettings) -> InlineKeyboardMarkup:
    def mark(enabled: bool) -> str:
        return "✅" if enabled else "❌"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"Min: {int(cfg.min_stars)} ⭐", callback_data="settings:min"),
                InlineKeyboardButton(text=f"Max: {int(cfg.max_stars)} ⭐", callback_data="settings:max"),
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
        ]
    )


def price_presets(kind: str) -> InlineKeyboardMarkup:
    presets = {
        "min": [500, 1000, 2000, 5000, 10000],
        "max": [5000, 10000, 30000, 65000, 100000],
        "interval": [0.15, 0.2, 0.3, 0.5],
    }
    rows = [
        [InlineKeyboardButton(text=f"{v} {'⭐' if kind != 'interval' else 'с'}", callback_data=f"set:{kind}:{v}")]
        for v in presets[kind]
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


MARKET_PICK_TITLES = {
    "all": "Все маркеты",
    MarketName.TONNEL.value: "Tonnel",
    MarketName.MRKT.value: "MRKT",
    MarketName.PORTAL.value: "Portal",
    MarketName.TELEGRAM.value: "Telegram Market",
}
