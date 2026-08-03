"""Telegram Market — fast newest Stars listings + owner resolve."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
)
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
    def __init__(self, client: TelegramClient, concurrency: int = 22) -> None:
        self.client = client
        self._gift_ids: list[int] = []
        self._sem = asyncio.Semaphore(concurrency)
        self._owner_sem = asyncio.Semaphore(12)
        self._cursor = 0
        self._owner_cache: dict[str, str] = {}
        self.last_stats: dict[str, int] = {
            "collections": 0,
            "ok": 0,
            "empty": 0,
            "errors": 0,
            "lots": 0,
        }

    def set_client(self, client: TelegramClient) -> None:
        self.client = client
        self._gift_ids.clear()
        self._owner_cache.clear()

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
            if resale is None:
                ids.append(int(gift_id))
                continue
            try:
                if int(resale) > 0:
                    ids.append(int(gift_id))
            except (TypeError, ValueError):
                ids.append(int(gift_id))

        if not ids:
            ids = [int(g.id) for g in gifts if getattr(g, "id", None) is not None]

        self._gift_ids = ids
        self.last_stats["collections"] = len(ids)
        logger.info("collections=%s", len(ids))
        return self._gift_ids

    async def fetch_newest(self, per_collection: int = 20) -> list[Lot]:
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
        unique = _dedupe(lots)
        stats["lots"] = len(unique)
        self.last_stats.update(stats)
        logger.info("scan lots=%s ok=%s err=%s", stats["lots"], stats["ok"], stats["errors"])
        return unique

    async def iter_wave(
        self,
        per_collection: int = 20,
        batch_size: int = 36,
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
                except Exception:  # noqa: BLE001
                    stats["errors"] += 1
            if chunk:
                yield _dedupe(chunk)
        self.last_stats.update(stats)

    async def resolve_owner(self, lot: Lot, timeout: float = 0.8) -> None:
        """Fill username even if gift is hidden on market."""
        if lot.seller:
            return
        if lot.slug and lot.slug in self._owner_cache:
            lot.seller = self._owner_cache[lot.slug]
            return

        # Try seller_id via get_entity
        if lot.seller_id:
            try:
                async with self._owner_sem:
                    ent = await asyncio.wait_for(
                        self.client.get_entity(lot.seller_id),
                        timeout=timeout,
                    )
                username = str(getattr(ent, "username", "") or "").lstrip("@")
                if username:
                    lot.seller = username
                    if lot.slug:
                        self._owner_cache[lot.slug] = username
                    return
            except Exception:  # noqa: BLE001
                pass

        if not lot.slug:
            return
        try:
            async with self._owner_sem:
                result = await asyncio.wait_for(
                    self.client(GetUniqueStarGiftRequest(slug=lot.slug)),
                    timeout=timeout,
                )
        except Exception:  # noqa: BLE001
            return

        gift = getattr(result, "gift", None)
        users = {
            int(u.id): u
            for u in (getattr(result, "users", None) or [])
            if getattr(u, "id", None) is not None
        }
        owner = getattr(gift, "owner_id", None) if gift else None
        seller_id = None
        if owner is not None:
            seller_id = getattr(owner, "user_id", None) or getattr(owner, "id", None)
            try:
                seller_id = int(seller_id) if seller_id is not None else None
            except (TypeError, ValueError):
                seller_id = None
        if seller_id and seller_id in users:
            username = str(getattr(users[seller_id], "username", "") or "").lstrip("@")
            if username:
                lot.seller = username
                lot.seller_id = seller_id
                self._owner_cache[lot.slug] = username
                return
        # owner_name fallback from unique gift
        if gift is not None:
            name = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
            if name and " " not in name and name.replace("_", "").isalnum():
                lot.seller = name
                self._owner_cache[lot.slug] = name

    async def _fetch_one(
        self,
        gift_id: int,
        limit: int,
        stats: dict[str, int] | None = None,
    ) -> list[Lot]:
        async with self._sem:
            result = await self._request(gift_id, limit, stars_only=True)
            lots = _parse_result(result) if result is not None else []
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
        for attempt in range(3):
            try:
                return await self.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset="",
                        limit=min(limit, 100),
                        stars_only=True if stars_only else None,
                    )
                )
            except FloodWaitError as exc:
                wait = min(float(exc.seconds) + 0.05, 2.5)
                await asyncio.sleep(wait)
            except RPCError:
                if attempt >= 2:
                    return None
                await asyncio.sleep(0.08 * (attempt + 1))
            except Exception:  # noqa: BLE001
                if attempt >= 2:
                    return None
                await asyncio.sleep(0.05 * (attempt + 1))
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
            if val > 0 and (name == "StarsAmount" or isinstance(item, StarsAmount)):
                return val
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
            seller = str(getattr(users[seller_id], "username", "") or "").lstrip("@")

    if not seller:
        raw_name = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
        # skip anonymized placeholders
        if raw_name and raw_name.lower() not in {"hidden", "anonymous", "telegram"}:
            if " " not in raw_name:
                seller = raw_name

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
