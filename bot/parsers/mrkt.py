from __future__ import annotations

import asyncio
import logging

from curl_cffi import requests
from fake_useragent import UserAgent

from bot.models import Currency, MarketName, RawLot
from bot.parsers.base import BaseMarketParser

logger = logging.getLogger(__name__)


class MrktParser(BaseMarketParser):
    name = MarketName.MRKT
    title = "MRKT"
    API = "https://api.tgmrkt.io/api/v1"

    def __init__(self, token: str = "") -> None:
        super().__init__()
        self.token = token

    def _sync_fetch(self, limit: int) -> list[RawLot]:
        if not self.token:
            raise RuntimeError("MRKT token missing. Set MRKT_TOKEN or run Telethon login.")
        try:
            ua = UserAgent().random
        except Exception:  # noqa: BLE001
            ua = "Mozilla/5.0"
        headers = {
            "Authorization": self.token,
            "Referer": "https://cdn.tgmrkt.io/",
            "User-Agent": ua,
            "Content-Type": "application/json",
        }
        payload = {
            "collectionNames": [],
            "modelNames": [],
            "backdropNames": [],
            "symbolNames": [],
            "ordering": None,
            "lowToHigh": False,
            "maxPrice": None,
            "minPrice": None,
            "mintable": None,
            "number": None,
            "count": min(limit, 20),
            "cursor": "",
            "query": None,
            "promotedFirst": False,
        }
        response = requests.post(
            f"{self.API}/gifts/saling",
            headers=headers,
            json=payload,
            impersonate="chrome",
            timeout=30,
        )
        response.raise_for_status()
        gifts = response.json().get("gifts") or []
        lots: list[RawLot] = []
        for item in gifts:
            lot_id = str(
                item.get("id")
                or item.get("giftId")
                or item.get("saleId")
                or item.get("number")
                or ""
            )
            price = _extract_price(item)
            if not lot_id or price <= 0:
                continue
            number = item.get("number") or item.get("externalCollectionNumber")
            lots.append(
                RawLot(
                    market=self.name,
                    external_id=lot_id,
                    title=str(
                        item.get("collectionName")
                        or item.get("name")
                        or item.get("title")
                        or "Gift"
                    ),
                    price=price,
                    currency=Currency.TON,
                    url="https://t.me/mrkt/app",
                    model=str(item.get("modelName") or item.get("model") or ""),
                    backdrop=str(item.get("backdropName") or item.get("backdrop") or ""),
                    symbol=str(item.get("symbolName") or item.get("symbol") or ""),
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
