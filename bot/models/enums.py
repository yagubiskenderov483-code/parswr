from __future__ import annotations

from enum import StrEnum


class MarketName(StrEnum):
    TELEGRAM = "telegram_market"
    PORTAL = "portal"
    MRKT = "mrkt"
    TONNEL = "tonnel"


class Currency(StrEnum):
    STARS = "STARS"
    TON = "TON"
    USD = "USD"


class Difficulty(StrEnum):
    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"
    IMPOSSIBLE = "Impossible"
    UNREAL = "Unreal"
    CUSTOM = "Custom"


MARKET_TITLES: dict[MarketName, str] = {
    MarketName.TELEGRAM: "Telegram Market",
    MarketName.PORTAL: "Portal",
    MarketName.MRKT: "MRKT",
    MarketName.TONNEL: "Tonnel",
}


def difficulty_for_stars(stars: float) -> Difficulty:
    """Map Stars price to difficulty. Gaps (e.g. 10k–15k) → Custom."""
    if 2000 <= stars < 5000:
        return Difficulty.EASY
    if 5000 <= stars <= 10000:
        return Difficulty.MEDIUM
    if 15000 <= stars < 30000:
        return Difficulty.HARD
    if 30000 <= stars < 65000:
        return Difficulty.IMPOSSIBLE
    if 65000 <= stars <= 100000:
        return Difficulty.UNREAL
    return Difficulty.CUSTOM
