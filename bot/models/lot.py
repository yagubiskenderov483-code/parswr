from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot.models.enums import Currency, Difficulty, MarketName, difficulty_for_stars
from bot.utils.links import nft_url, write_url


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
    seller_username: str = ""
    seller_id: int | None = None
    nft_url: str = ""
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
    seller_username: str = ""
    seller_id: int | None = None
    nft_url: str = ""
    write_url: str = ""
    listed_at: datetime | None = None
    found_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fingerprint(self) -> str:
        return f"{self.market.value}:{self.external_id}"

    @classmethod
    def from_raw(cls, raw: RawLot, price_stars: float) -> UnifiedLot:
        resolved_nft = raw.nft_url or nft_url(raw.title, raw.number)
        resolved_write = write_url(
            seller_username=raw.seller_username or None,
            seller_id=raw.seller_id,
            market_url=raw.url,
        )
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
            seller_username=raw.seller_username,
            seller_id=raw.seller_id,
            nft_url=resolved_nft,
            write_url=resolved_write,
            listed_at=raw.listed_at,
        )

    def display_title(self) -> str:
        name = self.title
        if self.number is not None:
            name = f"{self.title} #{self.number}"
        attrs = ", ".join(x for x in (self.model, self.backdrop, self.symbol) if x)
        return f"{name} · {attrs}" if attrs else name

    @property
    def seller_display(self) -> str:
        if self.seller_username:
            uname = self.seller_username if self.seller_username.startswith("@") else f"@{self.seller_username}"
            return uname
        if self.seller_id:
            return f"id:{self.seller_id}"
        return "—"
