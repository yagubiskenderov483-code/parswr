"""
Telegram Market scanner.

burst_search() — быстрый проход (пара секунд), как FreeGiftsParser.
run_check() — регулярный чек по кругу.

Фильтр: не отдаём @username продавцов, у кого ЛС только за Stars
(send_paid_messages_stars / requirementToContactPaidMessages).
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
from telethon.tl.functions.users import GetRequirementsToContactRequest
from telethon.tl.types import (
    RequirementToContactPaidMessages,
    StarsAmount,
    StarsTonAmount,
)

import credentials as creds

logger = logging.getLogger(__name__)

NANOTON = 1_000_000_000.0


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
    paid_dm: bool = False
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
    def writable(self) -> bool:
        """Можно писать в ЛС без Stars."""
        return bool(self.seller) and not self.paid_dm


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
    price_samples: list[str] = field(default_factory=list)


class TelegramMarket:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self._gift_ids: list[int] = []
        self._cursor = 0
        self._flood_until = 0.0
        self._gap_lock = asyncio.Lock()
        self._last_req = 0.0
        self._owner_cache: dict[str, str] = {}
        self._paid_cache: dict[int | str, bool] = {}
        self.check_no = 0
        self.last_error = ""

    def set_client(self, client: TelegramClient) -> None:
        self.client = client
        self._gift_ids.clear()
        self._owner_cache.clear()
        self._paid_cache.clear()
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
        limit_results: int = 100,
        time_budget: float = 6.0,
    ) -> CheckResult:
        """Быстрый поиск свежих лотов в диапазоне — цель ~2–4 сек."""
        started = time.monotonic()
        self.check_no += 1
        stats = {"ok": 0, "errors": 0, "floods": 0, "scanned": 0}
        samples: list[str] = []

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

        for i in range(0, len(batch), parallel):
            if time.monotonic() - started > time_budget:
                break
            group = batch[i : i + parallel]
            parts = await asyncio.gather(*[one(g) for g in group], return_exceptions=True)
            for part in parts:
                stats["scanned"] += 1
                if isinstance(part, list):
                    lots.extend(part)
                    for lot in part[:2]:
                        if len(samples) < 8:
                            samples.append(f"{lot.stars:.0f}⭐ {lot.title[:24]}")
                else:
                    stats["errors"] += 1
            # рано выходим, если уже набрали с запасом под фильтр платных ЛС
            matched_so_far = [
                lot
                for lot in _dedupe(lots)
                if min_stars <= lot.stars <= max_stars
            ]
            if len(matched_so_far) >= limit_results * 2:
                break

        unique = _dedupe(lots)
        matched = [lot for lot in unique if min_stars <= lot.stars <= max_stars]
        # берём с запасом — после filter_paid_dms обрежем до limit
        matched = matched[: max(limit_results * 3, limit_results)]

        err = self.last_error
        if not matched and unique:
            vals = sorted({round(l.stars) for l in unique})[:12]
            err = (err + " | " if err else "") + f"цены вне диапазона, примеры: {vals}"
        elif not matched and not unique:
            err = (err + " | " if err else "") + "0 лотов (проверь Stars/TON parse)"

        return CheckResult(
            check_no=self.check_no,
            scanned=stats["scanned"],
            lots=matched,
            collections_total=len(gift_ids),
            ok=stats["ok"],
            errors=stats["errors"],
            floods=stats["floods"],
            elapsed=time.monotonic() - started,
            error=err,
            price_samples=samples,
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

        return CheckResult(
            check_no=self.check_no,
            scanned=stats["scanned"],
            lots=_dedupe(lots),
            collections_total=n,
            ok=stats["ok"],
            errors=stats["errors"],
            floods=stats["floods"],
            elapsed=time.monotonic() - started,
            error=self.last_error,
        )

    async def resolve_owners(self, lots: list[Lot], timeout: float = 0.9) -> None:
        await asyncio.gather(*[self.resolve_owner(lot, timeout=timeout) for lot in lots])

    async def filter_paid_dms(self, lots: list[Lot], timeout: float = 2.5) -> list[Lot]:
        """Убирает @username / лоты, где ЛС только за Stars."""
        if not lots:
            return []

        # 1) уже помеченные из User.send_paid_messages_stars
        for lot in lots:
            if lot.paid_dm and lot.seller:
                self._paid_cache[lot.seller.lower()] = True
                if lot.seller_id:
                    self._paid_cache[lot.seller_id] = True
                lot.seller = ""

        # 2) bulk check через GetRequirementsToContact
        need_check: list[Lot] = []
        for lot in lots:
            if lot.paid_dm:
                continue
            if not lot.seller and not lot.seller_id:
                continue
            key = lot.seller_id if lot.seller_id else lot.seller.lower()
            cached = self._paid_cache.get(key)
            if cached is True:
                lot.paid_dm = True
                lot.seller = ""
                continue
            if cached is False:
                continue
            need_check.append(lot)

        if need_check:
            await self._check_paid_batch(need_check, timeout=timeout)

        # Отдаём только тех, кому можно писать бесплатно (с юзом),
        # плюс лоты без юза не показываем в выдаче юзов.
        writable: list[Lot] = []
        for lot in lots:
            if lot.paid_dm:
                continue
            if not lot.seller:
                continue
            writable.append(lot)
        return writable

    async def _check_paid_batch(self, lots: list[Lot], timeout: float) -> None:
        # уникальные peer-ключи
        peers: list[Any] = []
        index: list[Lot] = []
        seen: set[int | str] = set()
        for lot in lots:
            key: int | str
            peer: Any
            if lot.seller_id:
                key = lot.seller_id
                peer = lot.seller_id
            elif lot.seller:
                key = lot.seller.lower()
                peer = lot.seller
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            peers.append(peer)
            index.append(lot)

        if not peers:
            return

        # чанками по 20
        for i in range(0, len(peers), 20):
            chunk_peers = peers[i : i + 20]
            chunk_lots = index[i : i + 20]
            try:
                await self._wait_flood()
                result = await asyncio.wait_for(
                    self.client(GetRequirementsToContactRequest(id=chunk_peers)),
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("paid-dm check failed: %s", exc)
                continue

            reqs = list(result) if result is not None else []
            for lot, req in zip(chunk_lots, reqs):
                is_paid = isinstance(req, RequirementToContactPaidMessages) or (
                    req is not None
                    and req.__class__.__name__ == "RequirementToContactPaidMessages"
                )
                stars_amt = getattr(req, "stars_amount", None) if req else None
                try:
                    stars_amt_i = int(stars_amt) if stars_amt is not None else 0
                except (TypeError, ValueError):
                    stars_amt_i = 0
                if is_paid or stars_amt_i > 0:
                    lot.paid_dm = True
                    if lot.seller_id:
                        self._paid_cache[lot.seller_id] = True
                    if lot.seller:
                        self._paid_cache[lot.seller.lower()] = True
                    lot.seller = ""
                else:
                    if lot.seller_id:
                        self._paid_cache[lot.seller_id] = False
                    if lot.seller:
                        self._paid_cache[lot.seller.lower()] = False

            for lot in lots:
                if lot.paid_dm:
                    continue
                if lot.seller_id and self._paid_cache.get(lot.seller_id) is True:
                    lot.paid_dm = True
                    lot.seller = ""
                elif lot.seller and self._paid_cache.get(lot.seller.lower()) is True:
                    lot.paid_dm = True
                    lot.seller = ""

    async def resolve_owner(self, lot: Lot, timeout: float = 0.9) -> None:
        if lot.paid_dm:
            lot.seller = ""
            return
        if lot.seller:
            if self._paid_cache.get(lot.seller.lower()) is True:
                lot.paid_dm = True
                lot.seller = ""
            return
        if lot.slug and lot.slug in self._owner_cache:
            cached = self._owner_cache[lot.slug]
            if self._paid_cache.get(cached.lower()) is True:
                lot.paid_dm = True
                lot.seller = ""
            else:
                lot.seller = cached
            return
        if lot.seller_id and self._paid_cache.get(lot.seller_id) is True:
            lot.paid_dm = True
            return

        if lot.seller_id:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_entity(lot.seller_id), timeout=timeout
                )
                if _user_paid_dm(ent):
                    lot.paid_dm = True
                    self._paid_cache[lot.seller_id] = True
                    return
                username = _best_username(ent)
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
            user = users[seller_id]
            if _user_paid_dm(user):
                lot.paid_dm = True
                self._paid_cache[seller_id] = True
                return
            username = _best_username(user)
            if username:
                lot.seller = username
                self._owner_cache[lot.slug] = username
                return
        # скрытый owner_name иногда всё же юзернейм
        if gift and not lot.seller:
            raw = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
            if raw and " " not in raw and raw.lower() not in {
                "hidden",
                "anonymous",
                "telegram",
            }:
                if self._paid_cache.get(raw.lower()) is True:
                    lot.paid_dm = True
                else:
                    lot.seller = raw
                    self._owner_cache[lot.slug] = raw

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
            # один запрос: Stars + TON (TON конвертим в Stars)
            result = await self._request(gift_id, limit, stats, gap, timeout)
            lots = _parse_result(result) if result is not None else []
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
                            # None = все валюты (Stars + TON→Stars)
                            stars_only=None,
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


def _user_paid_dm(user: Any) -> bool:
    stars = getattr(user, "send_paid_messages_stars", None)
    if stars is None:
        return False
    try:
        return int(stars) > 0
    except (TypeError, ValueError):
        return False


def _best_username(user: Any) -> str:
    username = str(getattr(user, "username", "") or "").lstrip("@")
    if username:
        return username
    for item in getattr(user, "usernames", None) or []:
        name = str(getattr(item, "username", "") or "").lstrip("@")
        if not name:
            continue
        if getattr(item, "active", True) and not getattr(item, "edited", False):
            return name
        if not username:
            username = name
    return username


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
        keys = [lot.id]
        if lot.slug:
            keys.append(f"slug:{lot.slug}")
        if any(k in seen for k in keys):
            continue
        for k in keys:
            seen.add(k)
        out.append(lot)
    return out


def _ton_nano_to_stars(nano: float) -> float:
    ton = float(nano) / NANOTON
    return ton * float(creds.STARS_PER_TON)


def _extract_stars(gift: Any) -> float | None:
    """Stars напрямую; если только TON — конвертим nanotons→TON→Stars."""
    amounts = getattr(gift, "resell_amount", None)
    stars_val: float | None = None
    ton_val: float | None = None

    if isinstance(amounts, list):
        for item in amounts:
            name = item.__class__.__name__
            amount = getattr(item, "amount", None)
            if amount is None:
                continue
            try:
                val = float(amount)
            except (TypeError, ValueError):
                continue
            if val <= 0:
                continue
            if name == "StarsTonAmount" or isinstance(item, StarsTonAmount):
                ton_val = val
            elif name == "StarsAmount" or isinstance(item, StarsAmount):
                stars_val = val
            elif stars_val is None:
                # неизвестный тип с amount — пробуем как Stars
                stars_val = val
        if stars_val is not None:
            return stars_val
        if ton_val is not None:
            return _ton_nano_to_stars(ton_val)
        return None

    if getattr(gift, "resale_ton_only", False):
        # всё равно пробуем атрибуты ниже
        pass

    for attr in ("resell_stars", "stars", "price"):
        val = getattr(gift, attr, None)
        if val is None:
            continue
        if hasattr(val, "amount"):
            try:
                raw = float(val.amount)
            except (TypeError, ValueError):
                continue
            if val.__class__.__name__ == "StarsTonAmount" or isinstance(val, StarsTonAmount):
                return _ton_nano_to_stars(raw)
            return raw
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
    paid_dm = False
    owner = getattr(gift, "owner_id", None)
    if owner is not None:
        seller_id = getattr(owner, "user_id", None) or getattr(owner, "id", None)
        try:
            seller_id = int(seller_id) if seller_id is not None else None
        except (TypeError, ValueError):
            seller_id = None
        if seller_id and users and seller_id in users:
            user = users[seller_id]
            if _user_paid_dm(user):
                paid_dm = True
            else:
                seller = _best_username(user)

    if not seller and not paid_dm:
        raw = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
        if raw and raw.lower() not in {"hidden", "anonymous", "telegram"} and " " not in raw:
            seller = raw

    number_i = int(number) if number is not None else None
    lot_id = str(slug or gift_id or f"{title}-{number_i}")
    return Lot(
        id=lot_id,
        title=title,
        number=number_i,
        stars=float(stars),
        slug=slug,
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        seller=seller if not paid_dm else "",
        seller_id=seller_id,
        paid_dm=paid_dm,
    )
