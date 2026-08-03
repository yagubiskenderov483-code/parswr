from __future__ import annotations

from bot.models import Currency, RawLot, UnifiedLot
from bot.services.rates import RateService


class PriceConverter:
    def __init__(self, rates: RateService) -> None:
        self.rates = rates

    def to_stars(self, amount: float, currency: Currency) -> float:
        if amount < 0:
            return 0.0
        if currency == Currency.STARS:
            return float(amount)
        if currency == Currency.USD:
            stars_usd = self.rates.current.stars_usd
            return float(amount / stars_usd) if stars_usd > 0 else 0.0
        if currency == Currency.TON:
            return float(amount * self.rates.current.ton_to_stars)
        return 0.0

    def unify(self, raw: RawLot) -> UnifiedLot:
        stars = self.to_stars(raw.price, raw.currency)
        return UnifiedLot.from_raw(raw, price_stars=stars)
