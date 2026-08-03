from __future__ import annotations

from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from bot.database.models import AppSettings


def main_menu() -> ReplyKeyboardRemove:
    """No reply-keyboard buttons — only the blue Menu (BotCommands)."""
    return ReplyKeyboardRemove()


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command="start", description="Старт")]


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
        ]
    )


def price_presets(kind: str) -> InlineKeyboardMarkup:
    presets = {
        "min": [500, 1000, 2000, 5000, 10000],
        "max": [5000, 10000, 30000, 65000, 100000],
        "interval": [0.2, 0.3, 0.5, 1],
    }
    rows: list[list[InlineKeyboardButton]] = []
    for value in presets[kind]:
        suffix = "⭐" if kind != "interval" else "с"
        rows.append(
            [InlineKeyboardButton(text=f"{value} {suffix}", callback_data=f"set:{kind}:{value}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
