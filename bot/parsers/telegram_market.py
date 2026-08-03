from __future__ import annotations

import logging
from typing import Any

from bot.models import Currency, MarketName, RawLot
from bot.parsers.base import BaseMarketParser

logger = logging.getLogger(__name__)


class TelegramMarketParser(BaseMarketParser):
    """Official Telegram gift resale market via MTProto payments.getResaleStarGifts."""

    name = MarketName.TELEGRAM
    title = "Telegram Market"

    def __init__(self, client: Any | None = None) -> None:
        super().__init__()
        self.client = client
        self._gift_ids: list[int] = []

    async def _ensure_gift_ids(self) -> list[int]:
        if self._gift_ids:
            return self._gift_ids
        if self.client is None:
            raise RuntimeError("Telethon client is not connected for Telegram Market")

        from telethon.tl.functions.payments import GetStarGiftsRequest

        result = await self.client(GetStarGiftsRequest(hash=0))
        gifts = getattr(result, "gifts", []) or []
        ids: list[int] = []
        for gift in gifts:
            gift_id = getattr(gift, "id", None)
            # Prefer gifts that have resale availability
            availability_resale = getattr(gift, "availability_resale", None)
            if gift_id is None:
                continue
            if availability_resale is None or availability_resale:
                ids.append(int(gift_id))
        if not ids:
            # Fallback: take all known gift ids
            ids = [int(g.id) for g in gifts if getattr(g, "id", None) is not None]
        self._gift_ids = ids[:40]
        logger.info("Telegram Market: tracking %s gift collections", len(self._gift_ids))
        return self._gift_ids

    async def fetch_latest(self, limit: int = 30) -> list[RawLot]:
        if self.client is None:
            raise RuntimeError(
                "Telegram Market requires Telethon session. Run: python -m bot.login"
            )

        from telethon.tl.functions.payments import GetResaleStarGiftsRequest

        gift_ids = await self._ensure_gift_ids()
        lots: list[RawLot] = []
        per_collection = max(3, limit // max(len(gift_ids), 1))

        for gift_id in gift_ids:
            try:
                result = await self.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset="",
                        limit=min(per_collection, 20),
                        sort_by_price=False,
                        stars_only=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Resale fetch gift_id=%s failed: %s", gift_id, exc)
                continue

            gifts = getattr(result, "gifts", []) or []
            for gift in gifts:
                raw = _parse_resale_gift(gift)
                if raw:
                    lots.append(raw)
            if len(lots) >= limit:
                break

        # Newest first when possible
        lots.sort(key=lambda x: x.listed_at or x.external_id, reverse=True)
        return lots[:limit]


def _parse_resale_gift(gift: Any) -> RawLot | None:
    """Normalize Telethon star gift objects into RawLot."""
    # Unique gifts expose slug / title / num / resell_stars / resell_amount
    gift_id = (
        getattr(gift, "id", None)
        or getattr(gift, "gift_id", None)
        or getattr(getattr(gift, "gift", None), "id", None)
    )
    slug = getattr(gift, "slug", None) or getattr(gift, "title", None)
    title = (
        getattr(gift, "title", None)
        or getattr(getattr(gift, "gift", None), "title", None)
        or "Telegram Gift"
    )
    number = getattr(gift, "num", None) or getattr(gift, "number", None)

    price = None
    for attr in ("resell_stars", "resell_amount", "stars", "price"):
        val = getattr(gift, attr, None)
        if val is None:
            continue
        if hasattr(val, "amount"):
            price = float(val.amount)
            break
        try:
            price = float(val)
            break
        except (TypeError, ValueError):
            continue

    if price is None or price <= 0:
        return None

    external = str(gift_id or slug or f"{title}-{number}")
    url = f"https://t.me/nft/{slug}" if slug else "https://t.me/gift"

    model = backdrop = symbol = ""
    attributes = getattr(gift, "attributes", None) or []
    for attr in attributes:
        cls = attr.__class__.__name__.lower()
        name = getattr(attr, "name", "") or getattr(attr, "text", "") or ""
        if "model" in cls:
            model = str(name)
        elif "backdrop" in cls:
            backdrop = str(name)
        elif "pattern" in cls or "symbol" in cls:
            symbol = str(name)

    return RawLot(
        market=MarketName.TELEGRAM,
        external_id=external,
        title=str(title),
        price=float(price),
        currency=Currency.STARS,
        url=url,
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        number=int(number) if number is not None else None,
    )
