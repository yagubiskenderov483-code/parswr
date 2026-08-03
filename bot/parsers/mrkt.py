from __future__ import annotations

import asyncio
import logging

from curl_cffi import requests
from fake_useragent import UserAgent

from bot.models import Currency, MarketName, RawLot
from bot.parsers.base import BaseMarketParser
from bot.utils.links import nft_url

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
            raise RuntimeError("MRKT token missing — войди через /start")
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
            "ordering": None,  # newest first
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
            timeout=10,
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
            number_i = int(number) if number is not None else None
            title = str(
                item.get("collectionName")
                or item.get("name")
                or item.get("title")
                or "Gift"
            )
            seller, seller_id = _extract_seller(item)
            lots.append(
                RawLot(
                    market=self.name,
                    external_id=lot_id,
                    title=title,
                    price=price,
                    currency=Currency.TON,
                    url="https://t.me/mrkt/app",
                    model=str(item.get("modelName") or item.get("model") or ""),
                    backdrop=str(item.get("backdropName") or item.get("backdrop") or ""),
                    symbol=str(item.get("symbolName") or item.get("symbol") or ""),
                    number=number_i,
                    seller_username=seller,
                    seller_id=seller_id,
                    nft_url=nft_url(title, number_i),
                )
            )
        return lots

    async def fetch_latest(self, limit: int = 30) -> list[RawLot]:
        return await asyncio.to_thread(self._sync_fetch, limit)


def _extract_seller(item: dict) -> tuple[str, int | None]:
    candidates = [
        item.get("owner"),
        item.get("seller"),
        item.get("user"),
        item.get("saleOwner"),
        item.get("giftOwner"),
        item.get("telegramUser"),
    ]
    for owner in candidates:
        if isinstance(owner, dict):
            seller = (
                owner.get("username")
                or owner.get("userName")
                or owner.get("name")
                or owner.get("firstName")
                or ""
            )
            seller_id = (
                owner.get("telegramId")
                or owner.get("telegram_id")
                or owner.get("userId")
                or owner.get("id")
            )
            if seller or seller_id:
                try:
                    sid = int(seller_id) if seller_id is not None else None
                except (TypeError, ValueError):
                    sid = None
                return str(seller).lstrip("@"), sid
        elif isinstance(owner, str) and owner.strip():
            return owner.lstrip("@"), None

    seller = (
        item.get("ownerUsername")
        or item.get("sellerUsername")
        or item.get("username")
        or ""
    )
    seller_id = item.get("ownerId") or item.get("sellerId") or item.get("telegramId")
    try:
        sid = int(seller_id) if seller_id is not None else None
    except (TypeError, ValueError):
        sid = None
    return str(seller).lstrip("@") if seller else "", sid


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
