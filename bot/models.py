from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Lot:
    market: str
    lot_id: str
    name: str
    model: str
    backdrop: str
    symbol: str
    number: int | None
    price_ton: float
    stars: float
    url: str
    raw_status: str = ""

    @property
    def unique_id(self) -> str:
        return f"{self.market}:{self.lot_id}"

    @property
    def title(self) -> str:
        parts = [self.name]
        if self.number is not None:
            parts[0] = f"{self.name} #{self.number}"
        extra = ", ".join(p for p in (self.model, self.backdrop, self.symbol) if p)
        if extra:
            return f"{parts[0]} · {extra}"
        return parts[0]

    @property
    def price_label(self) -> str:
        stars = int(round(self.stars))
        ton = f"{self.price_ton:.2f}".rstrip("0").rstrip(".")
        return f"{stars:,} ⭐ (~{ton} TON)".replace(",", " ")
