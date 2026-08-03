from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot.models.enums import Currency, Difficulty, MarketName, difficulty_for_stars


@dataclass(slots=True)
class RawLot:
    market: MarketName
    external_id: str
    title: str
    price: float
    currency: Currency
    url: str
    model: str = ""
    backdrop: str = ""
    symbol: str = ""
    number: int | None = None
    listed_at: datetime | None = None
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return f"{self.market.value}:{self.external_id}"


@dataclass(slots=True)
class UnifiedLot:
    market: MarketName
    external_id: str
    title: str
    price_stars: float
    original_price: float
    original_currency: Currency
    url: str
    difficulty: Difficulty
    model: str = ""
    backdrop: str = ""
    symbol: str = ""
    number: int | None = None
    listed_at: datetime | None = None
    found_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fingerprint(self) -> str:
        return f"{self.market.value}:{self.external_id}"

    @classmethod
    def from_raw(cls, raw: RawLot, price_stars: float) -> UnifiedLot:
        return cls(
            market=raw.market,
            external_id=raw.external_id,
            title=raw.title,
            price_stars=price_stars,
            original_price=raw.price,
            original_currency=raw.currency,
            url=raw.url,
            difficulty=difficulty_for_stars(price_stars),
            model=raw.model,
            backdrop=raw.backdrop,
            symbol=raw.symbol,
            number=raw.number,
            listed_at=raw.listed_at,
        )

    def display_title(self) -> str:
        name = self.title
        if self.number is not None:
            name = f"{self.title} #{self.number}"
        attrs = ", ".join(x for x in (self.model, self.backdrop, self.symbol) if x)
        return f"{name} · {attrs}" if attrs else name
