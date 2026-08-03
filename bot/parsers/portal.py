from __future__ import annotations

import asyncio
import logging

from curl_cffi import requests
from fake_useragent import UserAgent

from bot.models import Currency, MarketName, RawLot
from bot.parsers.base import BaseMarketParser
from bot.utils.links import nft_url

logger = logging.getLogger(__name__)


class PortalParser(BaseMarketParser):
    name = MarketName.PORTAL
    title = "Portal"
    API = "https://portal-market.com/api"

    def __init__(self, auth: str = "") -> None:
        super().__init__()
        self.auth = auth  # optional

    def _sync_fetch(self, limit: int) -> list[RawLot]:
        try:
            ua = UserAgent().random
        except Exception:  # noqa: BLE001
            ua = "Mozilla/5.0"
        headers = {"User-Agent": ua, "Accept": "application/json"}
        if self.auth:
            headers["Authorization"] = self.auth

        response = requests.get(
            f"{self.API}/nfts/search",
            params={
                "offset": 0,
                "limit": min(limit, 50),
                "sort_by": "listed_at desc",
                "status": "listed",
            },
            headers=headers,
            impersonate="chrome",
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        lots: list[RawLot] = []
        for item in results:
            lot_id = str(item.get("id") or "")
            try:
                price = float(item.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if not lot_id or price <= 0:
                continue

            name = str(item.get("name") or "Gift")
            number = item.get("external_collection_number")
            number_i = int(number) if number is not None else None
            tg_id = str(item.get("tg_id") or "")
            explicit_nft = f"https://t.me/nft/{tg_id}" if tg_id else nft_url(name, number_i)

            attrs = {a.get("type"): a.get("value") for a in (item.get("attributes") or []) if isinstance(a, dict)}
            owner = item.get("owner") or item.get("seller") or {}
            if isinstance(owner, dict):
                seller = owner.get("username") or owner.get("name") or ""
                seller_id = owner.get("telegram_id") or owner.get("id")
            else:
                seller = item.get("owner_username") or item.get("seller_username") or ""
                seller_id = None
            try:
                sid = int(seller_id) if seller_id is not None else None
            except (TypeError, ValueError):
                sid = None

            lots.append(
                RawLot(
                    market=self.name,
                    external_id=lot_id,
                    title=name,
                    price=price,
                    currency=Currency.TON,
                    url="https://t.me/portals/market",
                    model=str(attrs.get("model") or ""),
                    backdrop=str(attrs.get("backdrop") or ""),
                    symbol=str(attrs.get("symbol") or ""),
                    number=number_i,
                    seller_username=str(seller).lstrip("@") if seller else "",
                    seller_id=sid,
                    nft_url=explicit_nft,
                )
            )
        return lots

    async def fetch_latest(self, limit: int = 30) -> list[RawLot]:
        return await asyncio.to_thread(self._sync_fetch, limit)
