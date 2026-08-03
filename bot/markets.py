from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from curl_cffi import requests
from fake_useragent import UserAgent

from bot.models import Lot

logger = logging.getLogger(__name__)


class MarketClient(Protocol):
    name: str

    async def fetch_newest(self, limit: int = 30) -> list[Lot]:
        ...


def _ua() -> str:
    try:
        return UserAgent().random
    except Exception:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )


class TonnelClient:
    name = "tonnel"
    URL = "https://gifts2.tonnel.network/api/pageGifts"

    def __init__(self, stars_per_ton: float, auth: str = ""):
        self.stars_per_ton = stars_per_ton
        self.auth = auth

    def _headers(self) -> dict[str, str]:
        return {
            "authority": "gifts2.tonnel.network",
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://market.tonnel.network",
            "referer": "https://market.tonnel.network/",
            "user-agent": _ua(),
        }

    def _sync_fetch(self, limit: int) -> list[Lot]:
        payload = {
            "page": 1,
            "limit": min(limit, 30),
            "sort": json.dumps({"message_post_time": -1, "gift_id": -1}),
            "filter": json.dumps(
                {
                    "price": {"$exists": True},
                    "refunded": {"$ne": True},
                    "buyer": {"$exists": False},
                    "export_at": {"$exists": True},
                    "asset": "TON",
                }
            ),
            "price_range": None,
            "user_auth": self.auth or "",
        }
        r = requests.post(
            self.URL,
            json=payload,
            headers=self._headers(),
            impersonate="chrome",
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("gifts") or []
        lots: list[Lot] = []
        for item in items:
            price = float(item.get("price") or 0)
            gift_id = str(item.get("gift_id") or item.get("_id") or "")
            if not gift_id or price <= 0:
                continue
            name = str(item.get("name") or item.get("gift_name") or "Gift")
            number = item.get("gift_num")
            lots.append(
                Lot(
                    market=self.name,
                    lot_id=gift_id,
                    name=name,
                    model=str(item.get("model") or ""),
                    backdrop=str(item.get("backdrop") or ""),
                    symbol=str(item.get("symbol") or ""),
                    number=int(number) if number is not None else None,
                    price_ton=price,
                    stars=price * self.stars_per_ton,
                    url=f"https://t.me/tonnel_network_bot/gifts",
                    raw_status=str(item.get("status") or ""),
                )
            )
        return lots

    async def fetch_newest(self, limit: int = 30) -> list[Lot]:
        return await asyncio.to_thread(self._sync_fetch, limit)


class MrktClient:
    name = "mrkt"
    API = "https://api.tgmrkt.io/api/v1"

    def __init__(self, stars_per_ton: float, token: str):
        self.stars_per_ton = stars_per_ton
        self.token = token

    def _sync_fetch(self, limit: int) -> list[Lot]:
        if not self.token:
            return []
        headers = {
            "Authorization": self.token,
            "Referer": "https://cdn.tgmrkt.io/",
            "User-Agent": _ua(),
            "Content-Type": "application/json",
        }
        # ordering=None → по времени выставления
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
        r = requests.post(
            f"{self.API}/gifts/saling",
            headers=headers,
            json=payload,
            impersonate="chrome",
            timeout=30,
        )
        r.raise_for_status()
        gifts = r.json().get("gifts") or []
        lots: list[Lot] = []
        for item in gifts:
            price = _extract_ton_price(item)
            lot_id = str(
                item.get("id")
                or item.get("giftId")
                or item.get("saleId")
                or item.get("number")
                or ""
            )
            if not lot_id or price <= 0:
                continue
            name = str(
                item.get("collectionName")
                or item.get("name")
                or item.get("title")
                or "Gift"
            )
            model = str(item.get("modelName") or item.get("model") or "")
            backdrop = str(item.get("backdropName") or item.get("backdrop") or "")
            symbol = str(item.get("symbolName") or item.get("symbol") or "")
            number = item.get("number") or item.get("externalCollectionNumber")
            lots.append(
                Lot(
                    market=self.name,
                    lot_id=lot_id,
                    name=name,
                    model=model,
                    backdrop=backdrop,
                    symbol=symbol,
                    number=int(number) if number is not None else None,
                    price_ton=price,
                    stars=price * self.stars_per_ton,
                    url="https://t.me/mrkt/app",
                )
            )
        return lots

    async def fetch_newest(self, limit: int = 30) -> list[Lot]:
        return await asyncio.to_thread(self._sync_fetch, limit)


class PortalsClient:
    name = "portals"
    API = "https://portal-market.com/api"

    def __init__(self, stars_per_ton: float, auth: str):
        self.stars_per_ton = stars_per_ton
        self.auth = auth

    def _sync_fetch(self, limit: int) -> list[Lot]:
        if not self.auth:
            return []
        headers = {"Authorization": self.auth, "User-Agent": _ua()}
        r = requests.get(
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
        r.raise_for_status()
        results = r.json().get("results") or []
        lots: list[Lot] = []
        for item in results:
            price = _extract_ton_price(item)
            lot_id = str(item.get("id") or item.get("nft_id") or "")
            if not lot_id or price <= 0:
                continue
            name = str(
                item.get("name")
                or (item.get("collection") or {}).get("name")
                or item.get("collection_name")
                or "Gift"
            )
            attrs = item.get("attributes") or {}
            model = str(item.get("model") or attrs.get("model") or "")
            backdrop = str(item.get("backdrop") or attrs.get("backdrop") or "")
            symbol = str(item.get("symbol") or attrs.get("symbol") or "")
            number = item.get("external_collection_number") or item.get("number")
            lots.append(
                Lot(
                    market=self.name,
                    lot_id=lot_id,
                    name=name,
                    model=model,
                    backdrop=backdrop,
                    symbol=symbol,
                    number=int(number) if number is not None else None,
                    price_ton=price,
                    stars=price * self.stars_per_ton,
                    url="https://t.me/portals/market",
                )
            )
        return lots

    async def fetch_newest(self, limit: int = 30) -> list[Lot]:
        return await asyncio.to_thread(self._sync_fetch, limit)


def _extract_ton_price(item: dict) -> float:
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
        # nanoTON → TON
        if price > 10000:
            price = price / 1_000_000_000
        return price
    return 0.0
