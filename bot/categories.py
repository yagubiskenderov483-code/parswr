from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceCategory:
    key: str
    title: str
    emoji: str
    min_stars: float
    max_stars: float

    def matches(self, stars: float) -> bool:
        return self.min_stars <= stars <= self.max_stars

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.title} · {_fmt(self.min_stars)}–{_fmt(self.max_stars)} ⭐"


def _fmt(value: float) -> str:
    v = int(value)
    if v >= 1000:
        return f"{v // 1000}к" if v % 1000 == 0 else f"{v:,}".replace(",", " ")
    return str(v)


CATEGORIES: tuple[PriceCategory, ...] = (
    PriceCategory("easy", "Лёгкий", "🟢", 2000, 5000),
    PriceCategory("medium", "Средний", "🟡", 5000, 10000),
    PriceCategory("hard", "Сложный", "🟠", 10000, 20000),
    PriceCategory("elite", "Топ", "🔴", 20000, 60000),
)

CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}


def category_for_stars(stars: float) -> PriceCategory | None:
    """Границы: 5000 → Средний, 10000 → Сложный, 20000 → Топ."""
    if 2000 <= stars < 5000:
        return CATEGORY_BY_KEY["easy"]
    if 5000 <= stars < 10000:
        return CATEGORY_BY_KEY["medium"]
    if 10000 <= stars < 20000:
        return CATEGORY_BY_KEY["hard"]
    if 20000 <= stars <= 60000:
        return CATEGORY_BY_KEY["elite"]
    return None
