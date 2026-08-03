"""Telegram Market — ultra-fast newest Stars listings."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
from telethon.tl.types import StarsAmount, StarsTonAmount

logger = logging.getLogger(__name__)

FRESH_WINDOW_SEC = 3600  # ~1 hour


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
    """
    Official Telegram Market (payments.getResaleStarGifts).
    Needs user login. No sort flags → newest by last resell change first.
    """

    def __init__(self, client: TelegramClient, concurrency: int = 24) -> None:
        self.client = client
        self._gift_ids: list[int] = []
        self._sem = asyncio.Semaphore(concurrency)
        self._cursor = 0

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
            resale = getattr(gift, "availability_resale", None)
            if resale is None or resale:
                ids.append(int(gift_id))
        if not ids:
            ids = [int(g.id) for g in gifts if getattr(g, "id", None) is not None]
        self._gift_ids = ids
        logger.info("collections=%s", len(self._gift_ids))
        return self._gift_ids

    async def fetch_newest(self, per_collection: int = 20) -> list[Lot]:
        """Full parallel scan of all collections."""
        gift_ids = await self.load_collections()
        if not gift_ids:
            return []
        results = await asyncio.gather(
            *[self._fetch_one(gid, per_collection) for gid in gift_ids],
            return_exceptions=True,
        )
        return _dedupe(_flatten(results))

    async def iter_wave(
        self,
        per_collection: int = 20,
        batch_size: int = 30,
    ) -> AsyncIterator[list[Lot]]:
        """
        Rotate through collections in hot waves — yields batches ASAP
        instead of waiting for the entire market.
        """
        gift_ids = await self.load_collections()
        if not gift_ids:
            return

        n = len(gift_ids)
        start = self._cursor % n
        batch = [gift_ids[(start + i) % n] for i in range(min(batch_size, n))]
        self._cursor = (start + len(batch)) % n

        tasks = [asyncio.create_task(self._fetch_one(gid, per_collection)) for gid in batch]
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            chunk: list[Lot] = []
            for task in done:
                try:
                    chunk.extend(task.result())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("wave task failed: %s", exc)
            if chunk:
                yield _dedupe(chunk)

    async def _fetch_one(self, gift_id: int, limit: int) -> list[Lot]:
        async with self._sem:
            for attempt in range(3):
                try:
                    result = await self.client(
                        GetResaleStarGiftsRequest(
                            gift_id=gift_id,
                            offset="",
                            limit=limit,
                            stars_only=True,
                        )
                    )
                    break
                except FloodWaitError as exc:
                    wait = min(int(exc.seconds) + 0.05, 3.0)
                    logger.warning("FloodWait %ss gift_id=%s", wait, gift_id)
                    await asyncio.sleep(wait)
                    continue
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        logger.debug("gift_id=%s: %s", gift_id, exc)
                        return []
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
            else:
                return []

        users = {
            int(u.id): u
            for u in (getattr(result, "users", None) or [])
            if getattr(u, "id", None) is not None
        }
        lots: list[Lot] = []
        now = time.time()
        for gift in getattr(result, "gifts", []) or []:
            lot = _parse(gift, users)
            if lot:
                lot.seen_at = now
                lots.append(lot)
        return lots


def _flatten(results: list) -> list[Lot]:
    lots: list[Lot] = []
    errors = 0
    for item in results:
        if isinstance(item, list):
            lots.extend(item)
        else:
            errors += 1
    if errors:
        logger.warning("collection errors=%s", errors)
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
    amounts = getattr(gift, "resell_amount", None)
    if isinstance(amounts, list):
        stars_val: float | None = None
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
            if val <= 0:
                continue
            if name == "StarsAmount" or isinstance(item, StarsAmount):
                return val
            if stars_val is None:
                stars_val = val
        return stars_val

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
