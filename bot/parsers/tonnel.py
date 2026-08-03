from __future__ import annotations

import asyncio
import json
import logging

from curl_cffi import requests
from fake_useragent import UserAgent

from bot.models import Currency, MarketName, RawLot
from bot.parsers.base import BaseMarketParser

logger = logging.getLogger(__name__)


class TonnelParser(BaseMarketParser):
    name = MarketName.TONNEL
    title = "Tonnel"
    URL = "https://gifts2.tonnel.network/api/pageGifts"

    def __init__(self, auth: str = "") -> None:
        super().__init__()
        self.auth = auth

    def _headers(self) -> dict[str, str]:
        try:
            ua = UserAgent().random
        except Exception:  # noqa: BLE001
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        return {
            "authority": "gifts2.tonnel.network",
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://market.tonnel.network",
            "referer": "https://market.tonnel.network/",
            "user-agent": ua,
        }

    def _sync_fetch(self, limit: int) -> list[RawLot]:
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
        response = requests.post(
            self.URL,
            json=payload,
            headers=self._headers(),
            impersonate="chrome",
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        items = data if isinstance(data, list) else data.get("gifts") or []
        lots: list[RawLot] = []
        for item in items:
            gift_id = str(item.get("gift_id") or item.get("_id") or "")
            price = float(item.get("price") or 0)
            if not gift_id or price <= 0:
                continue
            number = item.get("gift_num")
            lots.append(
                RawLot(
                    market=self.name,
                    external_id=gift_id,
                    title=str(item.get("name") or item.get("gift_name") or "Gift"),
                    price=price,
                    currency=Currency.TON,
                    url="https://t.me/tonnel_network_bot/gifts",
                    model=str(item.get("model") or ""),
                    backdrop=str(item.get("backdrop") or ""),
                    symbol=str(item.get("symbol") or ""),
                    number=int(number) if number is not None else None,
                    extra={"status": item.get("status")},
                )
            )
        return lots

    async def fetch_latest(self, limit: int = 30) -> list[RawLot]:
        return await asyncio.to_thread(self._sync_fetch, limit)
