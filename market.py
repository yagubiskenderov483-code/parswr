"""Telegram Market parser — only gifts listed for Stars."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Lot:
    id: str
    title: str
    number: int | None
    stars: float
    slug: str
    model: str = ""
    backdrop: str = ""
    symbol: str = ""
    seller: str = ""
    seller_id: int | None = None

    @property
    def nft_url(self) -> str:
        if self.slug:
            return f"https://t.me/nft/{self.slug}"
        if self.number is not None:
            clean = "".join(ch for ch in self.title if ch.isalnum())
            return f"https://t.me/nft/{clean}-{self.number}"
        return "https://t.me/nft/"

    @property
    def display(self) -> str:
        parts = [self.title]
        if self.number is not None:
            parts.append(f"#{self.number}")
        extra = " · ".join(x for x in (self.model, self.backdrop, self.symbol) if x)
        if extra:
            parts.append(f"({extra})")
        return " ".join(parts)


class TelegramMarket:
    """Fetches newest resale gifts sold for Stars."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self._gift_ids: list[int] = []

    async def load_collections(self) -> list[int]:
        if self._gift_ids:
            return self._gift_ids
        result = await self.client(GetStarGiftsRequest(hash=0))
        gifts = getattr(result, "gifts", []) or []
        ids: list[int] = []
        for gift in gifts:
            gift_id = getattr(gift, "id", None)
            if gift_id is None:
                continue
            resale = getattr(gift, "availability_resale", None)
            if resale is None or resale:
                ids.append(int(gift_id))
        if not ids:
            ids = [int(g.id) for g in gifts if getattr(g, "id", None) is not None]
        self._gift_ids = ids
        logger.info("Telegram Market collections: %s", len(self._gift_ids))
        return self._gift_ids

    async def fetch_latest(self, limit: int = 40) -> list[Lot]:
        gift_ids = await self.load_collections()
        lots: list[Lot] = []
        # Round-robin a bit of each collection for freshest Stars listings
        per = max(2, min(10, limit // max(len(gift_ids), 1) + 2))

        for gift_id in gift_ids:
            try:
                result = await self.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset="",
                        limit=per,
                        sort_by_price=False,  # newest first
                        stars_only=True,  # only Stars listings
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("gift_id=%s: %s", gift_id, exc)
                continue

            for gift in getattr(result, "gifts", []) or []:
                lot = _parse(gift)
                if lot:
                    lots.append(lot)

        # Deduplicate by id, keep order
        seen: set[str] = set()
        unique: list[Lot] = []
        for lot in lots:
            if lot.id in seen:
                continue
            seen.add(lot.id)
            unique.append(lot)
        return unique[:limit]


def _parse(gift: Any) -> Lot | None:
    gift_id = (
        getattr(gift, "id", None)
        or getattr(gift, "gift_id", None)
        or getattr(getattr(gift, "gift", None), "id", None)
    )
    slug = str(getattr(gift, "slug", None) or "")
    title = str(
        getattr(gift, "title", None)
        or getattr(getattr(gift, "gift", None), "title", None)
        or "Gift"
    )
    number = getattr(gift, "num", None) or getattr(gift, "number", None)

    stars = None
    for attr in ("resell_stars", "resell_amount", "stars", "price"):
        val = getattr(gift, attr, None)
        if val is None:
            continue
        if hasattr(val, "amount"):
            # StarsAmount-like
            try:
                stars = float(val.amount)
            except (TypeError, ValueError):
                continue
            break
        try:
            stars = float(val)
            break
        except (TypeError, ValueError):
            continue

    if stars is None or stars <= 0:
        return None

    model = backdrop = symbol = ""
    for attr in getattr(gift, "attributes", None) or []:
        cls = attr.__class__.__name__.lower()
        name = str(getattr(attr, "name", "") or getattr(attr, "text", "") or "")
        if "model" in cls:
            model = name
        elif "backdrop" in cls:
            backdrop = name
        elif "pattern" in cls or "symbol" in cls:
            symbol = name

    owner = getattr(gift, "owner", None) or getattr(gift, "from_id", None)
    seller = ""
    seller_id = None
    if owner is not None:
        seller = str(getattr(owner, "username", "") or "").lstrip("@")
        seller_id = getattr(owner, "user_id", None) or getattr(owner, "id", None)
        try:
            seller_id = int(seller_id) if seller_id is not None else None
        except (TypeError, ValueError):
            seller_id = None

    number_i = int(number) if number is not None else None
    external = str(gift_id or slug or f"{title}-{number_i}")

    return Lot(
        id=external,
        title=title,
        number=number_i,
        stars=float(stars),
        slug=slug,
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        seller=seller,
        seller_id=seller_id,
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
