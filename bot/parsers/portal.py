from __future__ import annotations

import asyncio
import logging

from curl_cffi import requests
from fake_useragent import UserAgent

from bot.models import Currency, MarketName, RawLot
from bot.parsers.base import BaseMarketParser

logger = logging.getLogger(__name__)


class PortalParser(BaseMarketParser):
    name = MarketName.PORTAL
    title = "Portal"
    API = "https://portal-market.com/api"

    def __init__(self, auth: str = "") -> None:
        super().__init__()
        self.auth = auth

    def _sync_fetch(self, limit: int) -> list[RawLot]:
        if not self.auth:
            raise RuntimeError(
                "Portal auth missing. Set PORTALS_AUTH or run Telethon login."
            )
        try:
            ua = UserAgent().random
        except Exception:  # noqa: BLE001
            ua = "Mozilla/5.0"
        headers = {"Authorization": self.auth, "User-Agent": ua}
        response = requests.get(
            f"{self.API}/nfts/search",
            params={
                "offset": 0,
                "limit": min(limit, 100),
                "sort_by": "listed_at desc",
                "status": "listed",
            },
            headers=headers,
            impersonate="chrome",
            timeout=30,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        lots: list[RawLot] = []
        for item in results:
            lot_id = str(item.get("id") or item.get("nft_id") or "")
            price = _extract_price(item)
            if not lot_id or price <= 0:
                continue
            collection = item.get("collection") or {}
            name = str(
                item.get("name")
                or collection.get("name")
                or item.get("collection_name")
                or "Gift"
            )
            attrs = item.get("attributes") or {}
            number = item.get("external_collection_number") or item.get("number")
            lots.append(
                RawLot(
                    market=self.name,
                    external_id=lot_id,
                    title=name,
                    price=price,
                    currency=Currency.TON,
                    url="https://t.me/portals/market",
                    model=str(item.get("model") or attrs.get("model") or ""),
                    backdrop=str(item.get("backdrop") or attrs.get("backdrop") or ""),
                    symbol=str(item.get("symbol") or attrs.get("symbol") or ""),
                    number=int(number) if number is not None else None,
                )
            )
        return lots

    async def fetch_latest(self, limit: int = 30) -> list[RawLot]:
        return await asyncio.to_thread(self._sync_fetch, limit)


def _extract_price(item: dict) -> float:
    for key in ("price", "amount", "salePrice", "sale_price", "priceTon", "price_ton"):
        val = item.get(key)
        if val is None:
            continue
        if isinstance(val, dict):
            for nested in ("amount", "value", "ton", "nano"):
                if nested in val:
                    val = val[nested]
                    break
        try:
            price = float(val)
        except (TypeError, ValueError):
            continue
        if price > 10_000:
            price /= 1_000_000_000
        return price
    return 0.0
