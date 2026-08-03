"""
Telegram Market scanner.

burst_search() — быстрый проход (пара секунд), как FreeGiftsParser.
run_check() — регулярный чек по кругу.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import StarsAmount, StarsTonAmount

logger = logging.getLogger(__name__)


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
    first_name: str = ""
    last_name: str = ""
    about: str = ""
    seen_at: float = field(default_factory=time.time)

    @property
    def model_key(self) -> str:
        title = (self.title or "").strip().lower()
        model = (self.model or "").strip().lower()
        return f"{title}|{model}" if model else (title or self.id)

    @property
    def owner_key(self) -> str:
        if self.seller:
            return self.seller.lower()
        if self.seller_id is not None:
            return f"id:{self.seller_id}"
        return f"lot:{self.id}"

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


@dataclass
class CheckResult:
    check_no: int
    scanned: int
    lots: list[Lot]
    collections_total: int
    ok: int = 0
    errors: int = 0
    floods: int = 0
    elapsed: float = 0.0
    error: str = ""
    all_lots: list[Lot] | None = None  # все найденные (для БД), не только matched


class TelegramMarket:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self._gift_ids: list[int] = []
        self._cursor = 0
        self._flood_until = 0.0
        self._gap_lock = asyncio.Lock()
        self._last_req = 0.0
        self._owner_cache: dict[str, str] = {}
        self._about_cache: dict[int, str] = {}
        self.check_no = 0
        self.last_error = ""

    def set_client(self, client: TelegramClient) -> None:
        self.client = client
        self._gift_ids.clear()
        self._owner_cache.clear()
        self._about_cache.clear()
        self._cursor = 0
        self._flood_until = 0.0
        self.check_no = 0
        self.last_error = ""

    async def ensure_connected(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()

    async def load_collections(self, force: bool = False) -> list[int]:
        if self._gift_ids and not force:
            return self._gift_ids
        await self.ensure_connected()
        result = await asyncio.wait_for(
            self.client(GetStarGiftsRequest(hash=0)),
            timeout=12.0,
        )
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
        logger.info("collections=%s", len(ids))
        return ids

    async def burst_search(
        self,
        min_stars: float,
        max_stars: float,
        *,
        parallel: int = 12,
        per_collection: int = 8,
        max_collections: int = 40,
        gap: float = 0.02,
        timeout: float = 8.0,
        limit_results: int = 25,
    ) -> CheckResult:
        """Быстрый поиск свежих лотов в диапазоне — цель ~2–4 сек."""
        started = time.monotonic()
        self.check_no += 1
        stats = {"ok": 0, "errors": 0, "floods": 0, "scanned": 0}

        try:
            gift_ids = await self.load_collections()
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return CheckResult(
                check_no=self.check_no,
                scanned=0,
                lots=[],
                collections_total=0,
                errors=1,
                elapsed=time.monotonic() - started,
                error=str(exc),
            )

        batch = gift_ids[:max_collections]
        sem = asyncio.Semaphore(parallel)
        lots: list[Lot] = []

        async def one(gid: int) -> list[Lot]:
            async with sem:
                return await self._fetch_one(
                    gid, per_collection, stats, gap=gap, timeout=timeout, sem=None
                )

        # кусками; стоп когда набрали preview или вышли по времени
        for i in range(0, len(batch), parallel):
            if time.monotonic() - started > 3.5:
                break
            group = batch[i : i + parallel]
            parts = await asyncio.gather(*[one(g) for g in group], return_exceptions=True)
            for part in parts:
                stats["scanned"] += 1
                if isinstance(part, list):
                    lots.extend(part)
                else:
                    stats["errors"] += 1
            matched_now = sum(
                1 for lot in _dedupe(lots) if min_stars <= lot.stars <= max_stars
            )
            if matched_now >= limit_results:
                break

        unique = _dedupe(lots)
        matched = [lot for lot in unique if min_stars <= lot.stars <= max_stars]
        matched = matched[:limit_results]

        return CheckResult(
            check_no=self.check_no,
            scanned=stats["scanned"],
            lots=matched,
            collections_total=len(gift_ids),
            ok=stats["ok"],
            errors=stats["errors"],
            floods=stats["floods"],
            elapsed=time.monotonic() - started,
            error=self.last_error,
            all_lots=unique,
        )

    async def run_check(
        self,
        *,
        parallel: int = 5,
        per_collection: int = 8,
        batch_size: int = 15,
        gap: float = 0.08,
        timeout: float = 8.0,
    ) -> CheckResult:
        started = time.monotonic()
        self.check_no += 1
        stats = {"ok": 0, "errors": 0, "floods": 0, "scanned": 0}

        try:
            gift_ids = await self.load_collections()
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                check_no=self.check_no,
                scanned=0,
                lots=[],
                collections_total=0,
                errors=1,
                elapsed=time.monotonic() - started,
                error=str(exc),
            )

        if not gift_ids:
            return CheckResult(
                check_no=self.check_no,
                scanned=0,
                lots=[],
                collections_total=0,
                errors=1,
                elapsed=time.monotonic() - started,
                error="Нет коллекций",
            )

        n = len(gift_ids)
        take = min(n, batch_size)
        batch = [gift_ids[(self._cursor + i) % n] for i in range(take)]
        self._cursor = (self._cursor + take) % n

        sem = asyncio.Semaphore(parallel)

        async def one(gid: int) -> list[Lot]:
            async with sem:
                return await self._fetch_one(
                    gid, per_collection, stats, gap=gap, timeout=timeout, sem=None
                )

        parts = await asyncio.gather(*[one(g) for g in batch], return_exceptions=True)
        lots: list[Lot] = []
        for part in parts:
            stats["scanned"] += 1
            if isinstance(part, list):
                lots.extend(part)
            else:
                stats["errors"] += 1

        unique = _dedupe(lots)
        return CheckResult(
            check_no=self.check_no,
            scanned=stats["scanned"],
            lots=unique,
            collections_total=n,
            ok=stats["ok"],
            errors=stats["errors"],
            floods=stats["floods"],
            elapsed=time.monotonic() - started,
            error=self.last_error,
            all_lots=unique,
        )

    async def resolve_owners(self, lots: list[Lot], timeout: float = 0.9) -> None:
        await asyncio.gather(*[self.resolve_owner(lot, timeout=timeout) for lot in lots])

    async def resolve_owner(self, lot: Lot, timeout: float = 0.9) -> None:
        if lot.seller and lot.seller_id is not None:
            return
        if lot.slug and lot.slug in self._owner_cache and not lot.seller:
            lot.seller = self._owner_cache[lot.slug]
            if lot.seller:
                return
        if lot.seller_id:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_entity(lot.seller_id), timeout=timeout
                )
                _fill_user(lot, ent)
                if lot.seller and lot.slug:
                    self._owner_cache[lot.slug] = lot.seller
                if lot.seller:
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
        if seller_id:
            lot.seller_id = seller_id
        if seller_id and seller_id in users:
            _fill_user(lot, users[seller_id])
            if lot.seller:
                self._owner_cache[lot.slug] = lot.seller

    async def load_abouts(
        self, lots: list[Lot], *, timeout: float = 0.7, parallel: int = 8
    ) -> None:
        """Bio для анти-рекламы. Поиск лотов не трогает."""
        sem = asyncio.Semaphore(parallel)

        async def one(lot: Lot) -> None:
            if not lot.seller_id:
                return
            if lot.seller_id in self._about_cache:
                lot.about = self._about_cache[lot.seller_id]
                return
            async with sem:
                try:
                    await self._wait_flood()
                    full = await asyncio.wait_for(
                        self.client(GetFullUserRequest(lot.seller_id)),
                        timeout=timeout,
                    )
                except Exception:  # noqa: BLE001
                    self._about_cache[lot.seller_id] = ""
                    return
                uf = getattr(full, "full_user", None)
                about = str(getattr(uf, "about", "") or "") if uf else ""
                lot.about = about
                self._about_cache[lot.seller_id] = about
                for u in getattr(full, "users", None) or []:
                    if getattr(u, "id", None) == lot.seller_id:
                        _fill_user(lot, u)
                        break

        await asyncio.gather(*[one(lot) for lot in lots])

    async def _fetch_one(
        self,
        gift_id: int,
        limit: int,
        stats: dict[str, int],
        *,
        gap: float,
        timeout: float,
        sem: asyncio.Semaphore | None,
    ) -> list[Lot]:
        async def _do() -> list[Lot]:
            result = await self._request(gift_id, limit, True, stats, gap, timeout)
            lots = _parse_result(result) if result is not None else []
            if not lots:
                result2 = await self._request(gift_id, limit, False, stats, gap, timeout)
                if result2 is not None:
                    lots = _parse_result(result2)
                    result = result2
            if lots:
                stats["ok"] += 1
            elif result is None:
                stats["errors"] += 1
            return lots

        if sem is None:
            return await _do()
        async with sem:
            return await _do()

    async def _request(
        self,
        gift_id: int,
        limit: int,
        stars_only: bool,
        stats: dict[str, int],
        gap: float,
        timeout: float,
    ) -> Any | None:
        for attempt in range(2):
            try:
                await self._wait_flood()
                await self.ensure_connected()
                async with self._gap_lock:
                    wait = gap - (time.monotonic() - self._last_req)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last_req = time.monotonic()
                return await asyncio.wait_for(
                    self.client(
                        GetResaleStarGiftsRequest(
                            gift_id=gift_id,
                            offset="",
                            limit=min(limit, 50),
                            stars_only=True if stars_only else None,
                        )
                    ),
                    timeout=timeout,
                )
            except FloodWaitError as exc:
                stats["floods"] += 1
                self._flood_until = time.monotonic() + min(float(exc.seconds) + 0.2, 20.0)
                self.last_error = f"FloodWait {exc.seconds}s"
                await asyncio.sleep(min(float(exc.seconds), 2.0))
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                self.last_error = str(exc)
                await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def _wait_flood(self) -> None:
        delay = self._flood_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


def _fill_user(lot: Lot, user: Any) -> None:
    username = str(getattr(user, "username", "") or "").lstrip("@").strip()
    if not username:
        for alt in getattr(user, "usernames", None) or []:
            name = str(getattr(alt, "username", "") or "").lstrip("@").strip()
            if name and getattr(alt, "active", True):
                username = name
                break
    if username:
        lot.seller = username
    sid = getattr(user, "id", None)
    if sid is not None:
        try:
            lot.seller_id = int(sid)
        except (TypeError, ValueError):
            pass
    fn = str(getattr(user, "first_name", "") or "")
    ln = str(getattr(user, "last_name", "") or "")
    if fn:
        lot.first_name = fn
    if ln:
        lot.last_name = ln


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
    first_name = ""
    last_name = ""
    owner = getattr(gift, "owner_id", None)
    if owner is not None:
        seller_id = getattr(owner, "user_id", None) or getattr(owner, "id", None)
        try:
            seller_id = int(seller_id) if seller_id is not None else None
        except (TypeError, ValueError):
            seller_id = None

    number_i = int(number) if number is not None else None
    lot = Lot(
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
        first_name=first_name,
        last_name=last_name,
    )
    if seller_id and users and seller_id in users:
        _fill_user(lot, users[seller_id])
    return lot
