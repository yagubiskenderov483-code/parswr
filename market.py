"""Telegram Market — newest Stars gift listings."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
from telethon.tl.types import StarsAmount, StarsTonAmount

logger = logging.getLogger(__name__)

FRESH_WINDOW_SEC = 3600


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
    seen_at: float = field(default_factory=time.time)

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

    @property
    def category(self) -> str:
        s = self.stars
        if 2000 <= s < 5000:
            return "Easy"
        if 5000 <= s <= 10000:
            return "Medium"
        if 15000 <= s < 30000:
            return "Hard"
        if 30000 <= s < 65000:
            return "Impossible"
        if 65000 <= s <= 100000:
            return "Unreal"
        return "Custom"


class TelegramMarket:
    def __init__(self, client: TelegramClient, concurrency: int = 8) -> None:
        self.client = client
        self._gift_ids: list[int] = []
        self._sem = asyncio.Semaphore(concurrency)
        self._cursor = 0
        self.last_stats: dict[str, int] = {
            "collections": 0,
            "ok": 0,
            "empty": 0,
            "errors": 0,
            "lots": 0,
        }

    def set_client(self, client: TelegramClient) -> None:
        self.client = client

    async def load_collections(self, force: bool = False) -> list[int]:
        if self._gift_ids and not force:
            return self._gift_ids

        result = await self.client(GetStarGiftsRequest(hash=0))
        gifts = getattr(result, "gifts", []) or []
        ids: list[int] = []
        for gift in gifts:
            gift_id = getattr(gift, "id", None)
            if gift_id is None:
                continue
            # availability_resale > 0 means collectibles are on market
            resale = getattr(gift, "availability_resale", None)
            if resale is None:
                ids.append(int(gift_id))
            else:
                try:
                    if int(resale) > 0:
                        ids.append(int(gift_id))
                except (TypeError, ValueError):
                    ids.append(int(gift_id))

        if not ids:
            ids = [int(g.id) for g in gifts if getattr(g, "id", None) is not None]

        self._gift_ids = ids
        self.last_stats["collections"] = len(ids)
        logger.info("collections with resale=%s / total_gifts=%s", len(ids), len(gifts))
        return self._gift_ids

    async def fetch_newest(self, per_collection: int = 15) -> list[Lot]:
        gift_ids = await self.load_collections()
        if not gift_ids:
            return []

        stats = {"ok": 0, "empty": 0, "errors": 0, "lots": 0}
        results = await asyncio.gather(
            *[self._fetch_one(gid, per_collection, stats) for gid in gift_ids],
            return_exceptions=True,
        )
        lots: list[Lot] = []
        for item in results:
            if isinstance(item, list):
                lots.extend(item)
            else:
                stats["errors"] += 1
                logger.warning("gather error: %s", item)

        unique = _dedupe(lots)
        stats["lots"] = len(unique)
        self.last_stats.update(stats)
        logger.info(
            "scan done lots=%s ok=%s empty=%s errors=%s",
            stats["lots"],
            stats["ok"],
            stats["empty"],
            stats["errors"],
        )
        return unique

    async def iter_wave(
        self,
        per_collection: int = 15,
        batch_size: int = 12,
    ) -> AsyncIterator[list[Lot]]:
        gift_ids = await self.load_collections()
        if not gift_ids:
            return

        n = len(gift_ids)
        start = self._cursor % n
        batch = [gift_ids[(start + i) % n] for i in range(min(batch_size, n))]
        self._cursor = (start + len(batch)) % n

        stats = {"ok": 0, "empty": 0, "errors": 0, "lots": 0}
        tasks = [
            asyncio.create_task(self._fetch_one(gid, per_collection, stats))
            for gid in batch
        ]
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            chunk: list[Lot] = []
            for task in done:
                try:
                    chunk.extend(task.result())
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    logger.debug("wave fail: %s", exc)
            if chunk:
                yield _dedupe(chunk)
        self.last_stats.update(stats)

    async def _fetch_one(
        self,
        gift_id: int,
        limit: int,
        stats: dict[str, int] | None = None,
    ) -> list[Lot]:
        async with self._sem:
            result = await self._request(gift_id, limit, stars_only=True)
            lots = _parse_result(result) if result is not None else []

            # Fallback: without stars_only, keep only StarsAmount prices
            if not lots:
                result2 = await self._request(gift_id, limit, stars_only=False)
                if result2 is not None:
                    lots = _parse_result(result2)

            if stats is not None:
                if result is None and not lots:
                    stats["errors"] += 1
                elif not lots:
                    stats["empty"] += 1
                else:
                    stats["ok"] += 1
            return lots

    async def _request(
        self,
        gift_id: int,
        limit: int,
        stars_only: bool,
    ) -> Any | None:
        for attempt in range(4):
            try:
                return await self.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset="",
                        limit=min(limit, 100),
                        stars_only=stars_only if stars_only else None,
                    )
                )
            except FloodWaitError as exc:
                wait = min(float(exc.seconds) + 0.1, 5.0)
                logger.warning("FloodWait %.1fs gift_id=%s", wait, gift_id)
                await asyncio.sleep(wait)
            except RPCError as exc:
                logger.debug("RPC gift_id=%s: %s", gift_id, exc)
                if attempt >= 2:
                    return None
                await asyncio.sleep(0.15 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                logger.debug("gift_id=%s: %s", gift_id, exc)
                if attempt >= 2:
                    return None
                await asyncio.sleep(0.1 * (attempt + 1))
        return None


def _parse_result(result: Any) -> list[Lot]:
    users = {
        int(u.id): u
        for u in (getattr(result, "users", None) or [])
        if getattr(u, "id", None) is not None
    }
    now = time.time()
    lots: list[Lot] = []
    for gift in getattr(result, "gifts", []) or []:
        lot = _parse(gift, users)
        if lot:
            lot.seen_at = now
            lots.append(lot)
    return lots


def _dedupe(lots: list[Lot]) -> list[Lot]:
    seen: set[str] = set()
    out: list[Lot] = []
    for lot in lots:
        if lot.id in seen:
            continue
        seen.add(lot.id)
        out.append(lot)
    return out


def _extract_stars(gift: Any) -> float | None:
    """Only Stars prices (skip TON). resell_amount is a LIST."""
    amounts = getattr(gift, "resell_amount", None)
    if isinstance(amounts, list):
        for item in amounts:
            name = item.__class__.__name__
            if name == "StarsTonAmount" or isinstance(item, StarsTonAmount):
                continue
            amount = getattr(item, "amount", None)
            if amount is None:
                continue
            try:
                val = float(amount)
            except (TypeError, ValueError):
                continue
            if val > 0 and (
                name == "StarsAmount"
                or isinstance(item, StarsAmount)
                or name != "StarsTonAmount"
            ):
                # Prefer explicit StarsAmount
                if name == "StarsAmount" or isinstance(item, StarsAmount):
                    return val
        # second pass: any non-TON
        for item in amounts:
            name = item.__class__.__name__
            if name == "StarsTonAmount" or isinstance(item, StarsTonAmount):
                continue
            amount = getattr(item, "amount", None)
            if amount is None:
                continue
            try:
                val = float(amount)
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
        return None

    # If only TON resale — skip (we want Stars)
    if getattr(gift, "resale_ton_only", False):
        return None

    for attr in ("resell_stars", "stars", "price"):
        val = getattr(gift, attr, None)
        if val is None:
            continue
        if hasattr(val, "amount"):
            try:
                return float(val.amount)
            except (TypeError, ValueError):
                continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _parse(gift: Any, users: dict[int, Any] | None = None) -> Lot | None:
    gift_id = getattr(gift, "id", None)
    slug = str(getattr(gift, "slug", None) or "")
    title = str(getattr(gift, "title", None) or "Gift")
    number = getattr(gift, "num", None)

    stars = _extract_stars(gift)
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

    seller = ""
    seller_id: int | None = None
    owner = getattr(gift, "owner_id", None)
    if owner is not None:
        seller_id = getattr(owner, "user_id", None) or getattr(owner, "id", None)
        try:
            seller_id = int(seller_id) if seller_id is not None else None
        except (TypeError, ValueError):
            seller_id = None
        if seller_id and users and seller_id in users:
            user = users[seller_id]
            seller = str(getattr(user, "username", "") or "").lstrip("@")

    if not seller:
        seller = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")

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
