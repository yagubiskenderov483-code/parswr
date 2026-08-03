"""
Telegram Market parser — стабильный, без самоубийства FloodWait.

Крутит коллекции по кругу по 2–3 за раз, ловит свежие Stars-лоты.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

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


class TelegramMarket:
    def __init__(self, client: TelegramClient, parallel: int = 3) -> None:
        self.client = client
        self.parallel = max(1, parallel)
        self._gift_ids: list[int] = []
        self._cursor = 0
        self._flood_until = 0.0
        self._sem = asyncio.Semaphore(self.parallel)
        self._last_req = 0.0
        self._gap_lock = asyncio.Lock()
        self._owner_cache: dict[str, str] = {}
        self.last_stats = {
            "collections": 0,
            "ok": 0,
            "empty": 0,
            "errors": 0,
            "lots": 0,
            "floods": 0,
        }

    def set_client(self, client: TelegramClient) -> None:
        self.client = client
        self._gift_ids.clear()
        self._owner_cache.clear()
        self._cursor = 0
        self._flood_until = 0.0

    async def ensure_connected(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()

    async def load_collections(self, force: bool = False) -> list[int]:
        if self._gift_ids and not force:
            return self._gift_ids
        await self.ensure_connected()
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
        return ids

    async def seed_market(self, per_collection: int = 12) -> list[Lot]:
        """Один спокойный проход по всем коллекциям (для seen)."""
        gift_ids = await self.load_collections(force=True)
        lots: list[Lot] = []
        stats = {"ok": 0, "empty": 0, "errors": 0, "floods": 0}
        for i in range(0, len(gift_ids), self.parallel):
            batch = gift_ids[i : i + self.parallel]
            chunk = await asyncio.gather(
                *[self._fetch_one(gid, per_collection, stats) for gid in batch]
            )
            for part in chunk:
                lots.extend(part)
            await asyncio.sleep(0.05)
        unique = _dedupe(lots)
        self.last_stats.update(stats)
        self.last_stats["lots"] = len(unique)
        logger.info(
            "seed lots=%s ok=%s empty=%s err=%s flood=%s",
            len(unique),
            stats["ok"],
            stats["empty"],
            stats["errors"],
            stats["floods"],
        )
        return unique

    async def poll_batch(self, per_collection: int = 12) -> list[Lot]:
        """Следующие N коллекций по кругу — свежие лоты."""
        gift_ids = await self.load_collections()
        if not gift_ids:
            return []
        n = len(gift_ids)
        batch: list[int] = []
        for _ in range(min(self.parallel, n)):
            batch.append(gift_ids[self._cursor % n])
            self._cursor = (self._cursor + 1) % n

        stats = {"ok": 0, "empty": 0, "errors": 0, "floods": 0}
        parts = await asyncio.gather(
            *[self._fetch_one(gid, per_collection, stats) for gid in batch]
        )
        lots: list[Lot] = []
        for part in parts:
            lots.extend(part)
        self.last_stats["ok"] = self.last_stats.get("ok", 0) + stats["ok"]
        self.last_stats["empty"] = self.last_stats.get("empty", 0) + stats["empty"]
        self.last_stats["errors"] = self.last_stats.get("errors", 0) + stats["errors"]
        self.last_stats["floods"] = self.last_stats.get("floods", 0) + stats["floods"]
        self.last_stats["lots"] = len(lots)
        return _dedupe(lots)

    async def resolve_owner(self, lot: Lot, timeout: float = 1.0) -> None:
        if lot.seller:
            return
        if lot.slug and lot.slug in self._owner_cache:
            lot.seller = self._owner_cache[lot.slug]
            return

        if lot.seller_id:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_entity(lot.seller_id), timeout=timeout
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
            await self._wait_flood()
            result = await asyncio.wait_for(
                self.client(GetUniqueStarGiftRequest(slug=lot.slug)),
                timeout=timeout,
            )
        except FloodWaitError as exc:
            await self._note_flood(exc.seconds)
            return
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

    async def _fetch_one(
        self,
        gift_id: int,
        limit: int,
        stats: dict[str, int],
    ) -> list[Lot]:
        result = await self._request(gift_id, limit, stars_only=True, stats=stats)
        lots = _parse_result(result) if result is not None else []
        if not lots:
            result2 = await self._request(gift_id, limit, stars_only=False, stats=stats)
            if result2 is not None:
                lots = _parse_result(result2)
                result = result2
        if lots:
            stats["ok"] += 1
        elif result is not None:
            stats["empty"] += 1
        else:
            stats["errors"] += 1
        return lots

    async def _request(
        self,
        gift_id: int,
        limit: int,
        stars_only: bool,
        stats: dict[str, int],
    ) -> Any | None:
        from credentials import REQUEST_GAP

        for attempt in range(4):
            try:
                await self._wait_flood()
                await self.ensure_connected()
                async with self._sem:
                    async with self._gap_lock:
                        gap = REQUEST_GAP - (time.monotonic() - self._last_req)
                        if gap > 0:
                            await asyncio.sleep(gap)
                        self._last_req = time.monotonic()
                    return await self.client(
                        GetResaleStarGiftsRequest(
                            gift_id=gift_id,
                            offset="",
                            limit=min(limit, 50),
                            stars_only=True if stars_only else None,
                        )
                    )
            except FloodWaitError as exc:
                stats["floods"] += 1
                await self._note_flood(exc.seconds)
                logger.warning("FloodWait %ss (gift=%s)", exc.seconds, gift_id)
            except RPCError as exc:
                stats["errors"] += 1
                logger.debug("RPC gift=%s: %s", gift_id, exc)
                await asyncio.sleep(0.2 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                logger.debug("err gift=%s: %s", gift_id, exc)
                try:
                    await self.ensure_connected()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.25 * (attempt + 1))
        return None

    async def _wait_flood(self) -> None:
        delay = self._flood_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _note_flood(self, seconds: int | float) -> None:
        wait = min(float(seconds) + 0.3, 60.0)
        self._flood_until = max(self._flood_until, time.monotonic() + wait)
        await asyncio.sleep(min(wait, 5.0))


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
        raw = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
        if raw and raw.lower() not in {"hidden", "anonymous", "telegram"} and " " not in raw:
            seller = raw

    number_i = int(number) if number is not None else None
    return Lot(
        id=str(gift_id or slug or f"{title}-{number_i}"),
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
