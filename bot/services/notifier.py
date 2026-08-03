from __future__ import annotations

from bot.models import MARKET_TITLES, Currency, UnifiedLot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def format_lot_message(lot: UnifiedLot) -> str:
    market = MARKET_TITLES.get(lot.market, lot.market.value)
    stars = _fmt_num(lot.price_stars)
    original = _fmt_original(lot.original_price, lot.original_currency)
    when = lot.found_at.strftime("%H:%M:%S UTC") if lot.found_at else "—"
    title = _esc(lot.display_title())
    seller = lot.seller_display
    if seller != "—" and not seller.startswith("@"):
        seller = f"@{seller}" if seller.replace(" ", "").isalnum() is False and " " not in seller else seller
    # normalize @
    if lot.seller_username:
        seller = f"@{lot.seller_username.lstrip('@')}"
    nft = lot.nft_url or lot.url

    return (
        "🆕 <b>Новый лот</b>\n\n"
        f"🎁 <b>Название:</b>\n{title}\n\n"
        f"👤 <b>Юз продавца:</b>\n{seller}\n\n"
        f"💰 <b>Цена:</b>\n{stars} ⭐\n\n"
        f"💵 <b>Исходная:</b>\n{original}\n\n"
        f"🌐 <b>Маркет:</b>\n{market}\n\n"
        f"📈 <b>Категория:</b>\n{lot.difficulty.value}\n\n"
        f"🕒 <b>Время:</b>\n{when}\n\n"
        f'🖼 <b>NFT:</b>\n<a href="{nft}">{nft}</a>'
    )


def lot_keyboard(lot: UnifiedLot) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nft = lot.nft_url or lot.url
    rows.append([InlineKeyboardButton(text="🖼 NFT", url=nft)])
    if lot.write_url and lot.seller_username:
        rows.append([InlineKeyboardButton(text="✍️ Написать продавцу", url=lot.write_url)])
    if lot.url and lot.url != nft:
        rows.append([InlineKeyboardButton(text="🌐 Маркет", url=lot.url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value)):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def _fmt_original(amount: float, currency: Currency) -> str:
    if currency == Currency.TON:
        text = f"{amount:.4f}".rstrip("0").rstrip(".")
        return f"{text} TON"
    if currency == Currency.USD:
        return f"${amount:.4f}"
    return f"{_fmt_num(amount)} ⭐"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
