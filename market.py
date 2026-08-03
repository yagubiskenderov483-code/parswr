"""
Telegram Market scanner.

- быстрый burst + чеки
- резолв скрытых гифтов → @username
- без юзов с ЛС за Stars
- только RU-признаки (био/ник/канал/подарки)
- Stars Rating level ≤ MAX_ACCOUNT_LEVEL (режем 5/6/8+)
- режем китов с кучей гифтов на профиле
- выдача вразнобой (не пачками одной коллекции)
- burst-парсинг маркета по многим коллекциям (live emit)
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetSavedStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
)
from telethon.tl.functions.users import (
    GetFullUserRequest,
    GetRequirementsToContactRequest,
)
from telethon.tl.types import (
    RequirementToContactPaidMessages,
    StarsAmount,
    StarsTonAmount,
    UserStatusOnline,
    UserStatusRecently,
)

import credentials as creds
from store import store

logger = logging.getLogger(__name__)

NANOTON = 1_000_000_000.0
CYRILLIC_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")


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
    level: int | None = None
    gifts_count: int | None = None
    ru_score: int = 0
    ru_ok: bool = False
    skip_reason: str = ""
    market_rank: int = 0
    listing_age: float = 0.0
    collection_id: int | None = None
    floor_stars: float | None = None
    online: bool = False
    recently: bool = False
    owner_hidden: bool = False  # профиль скрыт, но owner найден
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
    def seller_label(self) -> str:
        if self.seller and USERNAME_RE.fullmatch(self.seller):
            return f"@{self.seller}"
        return ""

    @property
    def write_url(self) -> str | None:
        if self.seller and USERNAME_RE.fullmatch(self.seller):
            return f"https://t.me/{self.seller}"
        return None

    @property
    def writable(self) -> bool:
        """Только реальный @username. Без юза — не выдаём (и не пишем «скрыт»)."""
        if self.paid_dm:
            return False
        return bool(self.seller and USERNAME_RE.fullmatch(self.seller))


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


@dataclass
class PrepareStats:
    input: int = 0
    with_user: int = 0
    paid_skip: int = 0
    level_skip: int = 0
    gifts_skip: int = 0
    ru_skip: int = 0
    fresh_skip: int = 0
    black_skip: int = 0
    price_skip: int = 0
    online_skip: int = 0
    kept: int = 0


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
        self._profile_cache: dict[int | str, dict[str, Any]] = {}
        self._floor_by_id: dict[int, float] = {}
        self._obs_prices: dict[str, list[float]] = {}
        self.check_no = 0
        self.last_error = ""
        # runtime filters
        self.max_level = creds.MAX_ACCOUNT_LEVEL
        self.max_gifts = creds.MAX_PROFILE_GIFTS
        self.min_ru = creds.MIN_RU_SCORE
        self.fresh_age = float(creds.FRESH_MAX_AGE_SEC)
        self.fresh_rank = int(creds.FRESH_MAX_RANK)
        self.online_mode = str(creds.ONLINE_MODE)
        self.max_price_mult = 2.5  # legacy unused
        self.search_min = float(creds.MIN_STARS)
        self.search_max = float(creds.MAX_STARS)
        self._hydrate_profiles()
        self._hydrate_floors()

    def _hydrate_floors(self) -> None:
        for key, row in store.floors.items():
            try:
                floor = float(row.get("floor", 0))
            except (TypeError, ValueError):
                continue
            if floor <= 0:
                continue
            if key.isdigit():
                self._floor_by_id[int(key)] = floor
            else:
                self._obs_prices.setdefault(key, [])
                # title-key floors live via store.get_floor
                pass

    def floor_delta(self) -> float | None:
        """Макс. надбавка над floor для текущего режима поиска."""
        key = (int(self.search_min), int(self.search_max))
        table = getattr(creds, "FLOOR_DELTA_BY_RANGE", {})
        if key in table:
            return table[key]
        # fallback: ближайший диапазон по mid
        mid = (self.search_min + self.search_max) / 2
        for (a, b), delta in table.items():
            if a <= mid <= b:
                return delta
        return None

    def pick_collections(
        self,
        min_stars: float,
        max_stars: float,
        *,
        max_collections: int,
    ) -> list[int]:
        """Коллекции, у которых floor реально может дать лоты в диапазоне.

        Дешёвые (floor+Δ < min) и заведомо дорогие (floor > max) отсекаем —
        иначе в 60–100k сыпятся 3k-коллекции.
        """
        ids = list(self._gift_ids)
        if not ids:
            return []
        delta = self.floor_delta()
        matched: list[int] = []
        unknown: list[int] = []
        for gid in ids:
            floor = self._floor_by_id.get(gid)
            if floor is None or floor <= 0:
                unknown.append(gid)
                continue
            if floor > max_stars:
                continue
            if delta is not None and floor + float(delta) < min_stars:
                continue
            matched.append(gid)
        random.shuffle(matched)
        random.shuffle(unknown)
        # при активном floor-фильтре почти не берём unknown — иначе 3k-коллекции
        if delta is not None:
            extra = max(20, max_collections // 10) if max_collections > 0 else 40
            ordered = matched + unknown[:extra]
        else:
            ordered = matched + unknown
        if max_collections > 0:
            ordered = ordered[:max_collections]
        logger.info(
            "pick collections: match=%s unknown=%s take=%s range=%.0f-%.0f Δ=%s",
            len(matched),
            len(unknown),
            len(ordered),
            min_stars,
            max_stars,
            delta,
        )
        return ordered

    def _hydrate_profiles(self) -> None:
        for key, val in store.profiles.items():
            try:
                ikey: int | str = int(key) if key.isdigit() else key
            except ValueError:
                ikey = key
            self._profile_cache[ikey] = val
            if val.get("paid_dm") and isinstance(ikey, int):
                self._paid_cache[ikey] = True
            user = val.get("username")
            if val.get("paid_dm") and user:
                self._paid_cache[str(user).lower()] = True

    def set_client(self, client: TelegramClient) -> None:
        store.flush()
        self.client = client
        self._gift_ids.clear()
        self._owner_cache.clear()
        self._paid_cache.clear()
        self._profile_cache.clear()
        self._floor_by_id.clear()
        self._obs_prices.clear()
        self._gift_ids.clear()
        self._cursor = 0
        self._flood_until = 0.0
        self.check_no = 0
        self.last_error = ""
        self._hydrate_profiles()
        self._hydrate_floors()

    async def ensure_connected(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()

    async def load_collections(self, force: bool = False) -> list[int]:
        if self._gift_ids and not force:
            return self._gift_ids
        await self.ensure_connected()
        result = await asyncio.wait_for(
            self.client(GetStarGiftsRequest(hash=0)),
            timeout=10.0,
        )
        gifts = getattr(result, "gifts", []) or []
        ids: list[int] = []
        for gift in gifts:
            gift_id = getattr(gift, "id", None)
            if gift_id is None:
                continue
            gid = int(gift_id)
            # рыночный минимум коллекции
            floor = getattr(gift, "resell_min_stars", None)
            if floor is not None:
                try:
                    fval = float(floor)
                    if fval > 0:
                        self._floor_by_id[gid] = fval
                        title = str(getattr(gift, "title", "") or "")
                        store.note_floor(str(gid), fval, title=title)
                        if title:
                            store.note_floor(f"t:{title.strip().lower()}", fval, title=title)
                except (TypeError, ValueError):
                    pass
            resale = getattr(gift, "availability_resale", None)
            if resale is None:
                continue
            try:
                if int(resale) > 0:
                    ids.append(gid)
            except (TypeError, ValueError):
                continue
        if not ids:
            # fallback: всё, у чего есть floor/resell_min
            ids = [
                int(g.id)
                for g in gifts
                if getattr(g, "id", None) is not None
                and getattr(g, "resell_min_stars", None)
            ]
        random.shuffle(ids)
        self._gift_ids = ids
        self._cursor = 0
        logger.info("collections=%s floors=%s", len(ids), len(self._floor_by_id))
        return ids

    async def live_parse(
        self,
        min_stars: float,
        max_stars: float,
        *,
        parallel: int = 24,
        per_collection: int = 10,
        max_collections: int = 80,
        gap: float = 0.0,
        timeout: float = 2.2,
        limit_results: int = 100,
        time_budget: float = 3.0,
        require_fresh: bool = True,
        check_rank: bool = True,
        stats: PrepareStats | None = None,
    ):
        """Скан + квалификация параллельно: отдаём лоты сразу, без ожидания конца скана."""
        stats = stats or PrepareStats()
        started = time.monotonic()
        self.check_no += 1
        scan_stats = {"ok": 0, "errors": 0, "floods": 0, "scanned": 0}
        hard_deadline = started + max(0.6, time_budget)

        try:
            gift_ids = await self.load_collections()
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return

        batch = self.pick_collections(
            min_stars, max_stars, max_collections=max_collections
        )
        if not batch:
            batch = list(gift_ids)[:max_collections]
            random.shuffle(batch)

        raw: asyncio.Queue[Lot | None] = asyncio.Queue()
        ready: asyncio.Queue[Lot | None] = asyncio.Queue()
        seen_ids: set[str] = set()
        workers_n = max(4, min(creds.PROFILE_PARALLEL, 12))

        async def scan() -> None:
            sem = asyncio.Semaphore(parallel)

            async def one(gid: int) -> list[Lot]:
                left = hard_deadline - time.monotonic()
                if left <= 0.05:
                    return []
                req_timeout = min(timeout, max(0.3, left - 0.05))
                async with sem:
                    return await self._fetch_one(
                        gid, per_collection, scan_stats, gap=gap, timeout=req_timeout
                    )

            # волны: не стопаемся после 1-й пачки из ~15
            for i in range(0, len(batch), parallel):
                if time.monotonic() >= hard_deadline:
                    break
                group = batch[i : i + parallel]
                parts = await asyncio.gather(
                    *[one(g) for g in group], return_exceptions=True
                )
                for part in parts:
                    scan_stats["scanned"] += 1
                    if not isinstance(part, list):
                        scan_stats["errors"] += 1
                        continue
                    for lot in part:
                        if lot.id in seen_ids:
                            continue
                        seen_ids.add(lot.id)
                        stats.input += 1
                        if min_stars <= lot.stars <= max_stars:
                            await raw.put(lot)
            logger.info(
                "live scan done scanned=%s/%s matched_raw=%s t=%.1fs",
                scan_stats["scanned"],
                len(batch),
                stats.input,
                time.monotonic() - started,
            )
            for _ in range(workers_n):
                await raw.put(None)

        async def worker() -> None:
            while True:
                lot = await raw.get()
                if lot is None:
                    await ready.put(None)
                    return
                ok = await self.qualify_one(
                    lot,
                    stats,
                    owner_timeout=creds.OWNER_TIMEOUT,
                    paid_timeout=creds.PAID_DM_TIMEOUT,
                    profile_timeout=creds.PROFILE_TIMEOUT,
                    require_fresh=require_fresh,
                    check_rank=check_rank,
                )
                if ok:
                    await ready.put(lot)

        scan_task = asyncio.create_task(scan())
        worker_tasks = [asyncio.create_task(worker()) for _ in range(workers_n)]

        mixer = EmitMixer(gap=int(getattr(creds, "EMIT_GAP", 5)))  # title+seller gap
        finished = 0
        emitted = 0

        try:
            while finished < workers_n and emitted < limit_results:
                item = await ready.get()
                if item is None:
                    finished += 1
                    # при каждом завершённом воркере пробуем вытолкнуть из буфера
                    while emitted < limit_results:
                        nxt = mixer.pop()
                        if nxt is None:
                            break
                        emitted += 1
                        stats.kept = emitted
                        yield nxt
                    continue
                out = mixer.push(item)
                for lot in out:
                    if emitted >= limit_results:
                        break
                    emitted += 1
                    stats.kept = emitted
                    yield lot
            # финальный слив без подряд одинаковых
            while emitted < limit_results:
                nxt = mixer.pop()
                if nxt is None:
                    break
                emitted += 1
                stats.kept = emitted
                yield nxt
            # остаток одной коллекции — НЕ кидаем подряд, дропаем
            dropped = mixer.drop_rest()
            if dropped:
                logger.info("dropped consecutive same-title leftovers=%s", dropped)
        finally:
            if not scan_task.done():
                scan_task.cancel()
            for t in worker_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(scan_task, *worker_tasks, return_exceptions=True)
            store.flush()

    async def burst_search(
        self,
        min_stars: float,
        max_stars: float,
        *,
        parallel: int = 16,
        per_collection: int = 12,
        max_collections: int = 100,
        gap: float = 0.01,
        timeout: float = 6.0,
        limit_results: int = 100,
        time_budget: float = 3.0,
    ) -> CheckResult:
        started = time.monotonic()
        self.check_no += 1
        stats = {"ok": 0, "errors": 0, "floods": 0, "scanned": 0}
        samples: list[str] = []
        # жёсткий потолок: запросы не длиннее остатка бюджета
        hard_deadline = started + max(0.8, time_budget)

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

        # каждый burst — коллекции, у которых floor в зоне диапазона
        batch = self.pick_collections(
            min_stars, max_stars, max_collections=max_collections
        )
        if not batch:
            batch = list(gift_ids)[:max_collections]
            random.shuffle(batch)
        sem = asyncio.Semaphore(parallel)
        lots: list[Lot] = []

        async def one(gid: int) -> list[Lot]:
            left = hard_deadline - time.monotonic()
            if left <= 0.05:
                return []
            req_timeout = min(timeout, max(0.35, left - 0.05))
            async with sem:
                return await self._fetch_one(
                    gid, per_collection, stats, gap=gap, timeout=req_timeout
                )

        for i in range(0, len(batch), parallel):
            if time.monotonic() >= hard_deadline:
                break
            group = batch[i : i + parallel]
            parts = await asyncio.gather(*[one(g) for g in group], return_exceptions=True)
            for part in parts:
                stats["scanned"] += 1
                if isinstance(part, list):
                    lots.extend(part)
                    for lot in part[:1]:
                        if len(samples) < 8:
                            samples.append(f"{lot.stars:.0f}⭐ {lot.title[:24]}")
                else:
                    stats["errors"] += 1
            matched_so_far = [
                lot for lot in _dedupe(lots) if min_stars <= lot.stars <= max_stars
            ]
            if len(matched_so_far) >= limit_results * 3:
                break

        unique = _dedupe(lots)
        matched = [lot for lot in unique if min_stars <= lot.stars <= max_stars]
        matched = matched[: max(limit_results * 4, limit_results)]

        err = self.last_error
        if not matched and unique:
            vals = sorted({round(l.stars) for l in unique})[:12]
            err = (err + " | " if err else "") + f"цены вне диапазона, примеры: {vals}"
        elif not matched and not unique:
            err = (err + " | " if err else "") + "0 лотов"

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
        parallel: int = 8,
        per_collection: int = 10,
        batch_size: int = 20,
        gap: float = 0.04,
        timeout: float = 6.0,
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
        # чек тоже по диапазону: не крутим 3k-коллекции в режиме 60–100k
        pool = self.pick_collections(
            self.search_min,
            self.search_max,
            max_collections=0,  # все подходящие
        )
        if not pool:
            pool = list(gift_ids)
        pn = len(pool)
        take = min(pn, max(1, batch_size))
        batch = [pool[(self._cursor + i) % pn] for i in range(take)]
        self._cursor = (self._cursor + take) % max(pn, 1)

        sem = asyncio.Semaphore(parallel)

        async def one(gid: int) -> list[Lot]:
            async with sem:
                return await self._fetch_one(
                    gid, per_collection, stats, gap=gap, timeout=timeout
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

    async def prepare_lots(
        self,
        lots: list[Lot],
        *,
        limit: int | None = None,
        owner_timeout: float | None = None,
        paid_timeout: float | None = None,
        profile_timeout: float | None = None,
        require_fresh: bool = True,
    ) -> tuple[list[Lot], PrepareStats]:
        kept: list[Lot] = []
        stats = PrepareStats(input=len(lots))
        async for lot in self.stream_prepared(
            lots,
            limit=limit,
            owner_timeout=owner_timeout,
            paid_timeout=paid_timeout,
            profile_timeout=profile_timeout,
            require_fresh=require_fresh,
            stats=stats,
        ):
            kept.append(lot)
        store.flush()
        return kept, stats

    async def stream_prepared(
        self,
        lots: list[Lot],
        *,
        limit: int | None = None,
        owner_timeout: float | None = None,
        paid_timeout: float | None = None,
        profile_timeout: float | None = None,
        require_fresh: bool = True,
        check_rank: bool = True,
        stats: PrepareStats | None = None,
    ):
        """Квалифицирует лоты параллельно и отдаёт готовые сразу (не ждёт всех)."""
        stats = stats or PrepareStats(input=len(lots))
        if not lots:
            return

        ordered = diversify_lots(_dedupe(lots))
        stats.input = len(ordered)
        queue: asyncio.Queue[Lot | None] = asyncio.Queue()
        sem = asyncio.Semaphore(creds.PROFILE_PARALLEL)
        cap = limit if limit is not None else len(ordered)

        async def worker(lot: Lot) -> None:
            async with sem:
                ok = await self.qualify_one(
                    lot,
                    stats,
                    owner_timeout=owner_timeout or creds.OWNER_TIMEOUT,
                    paid_timeout=paid_timeout or creds.PAID_DM_TIMEOUT,
                    profile_timeout=profile_timeout or creds.PROFILE_TIMEOUT,
                    require_fresh=require_fresh,
                    check_rank=check_rank,
                )
            await queue.put(lot if ok else None)

        tasks = [asyncio.create_task(worker(lot)) for lot in ordered]
        done = 0
        emitted = 0
        total = len(tasks)
        mixer = EmitMixer(gap=int(getattr(creds, "EMIT_GAP", 5)))

        while done < total and emitted < cap:
            item = await queue.get()
            done += 1
            if item is None:
                while emitted < cap:
                    nxt = mixer.pop()
                    if nxt is None:
                        break
                    emitted += 1
                    stats.kept = emitted
                    yield nxt
                continue
            for lot in mixer.push(item):
                if emitted >= cap:
                    break
                emitted += 1
                stats.kept = emitted
                yield lot

        while emitted < cap:
            nxt = mixer.pop()
            if nxt is None:
                break
            emitted += 1
            stats.kept = emitted
            yield nxt
        dropped = mixer.drop_rest()
        if dropped:
            logger.info("stream dropped consecutive leftovers=%s", dropped)

        while done < total:
            await queue.get()
            done += 1
        await asyncio.gather(*tasks, return_exceptions=True)
        store.flush()

    async def qualify_one(
        self,
        lot: Lot,
        stats: PrepareStats,
        *,
        owner_timeout: float,
        paid_timeout: float,
        profile_timeout: float,
        require_fresh: bool,
        check_rank: bool = True,
    ) -> bool:
        # жёсткий диапазон режима поиска
        if not (self.search_min <= lot.stars <= self.search_max):
            stats.price_skip += 1
            lot.skip_reason = (
                f"price {lot.stars:.0f}∉[{self.search_min:.0f},{self.search_max:.0f}]"
            )
            return False

        # адекватная цена vs floor коллекции
        floor = self._fair_floor(lot)
        lot.floor_stars = floor
        delta = self.floor_delta()
        if delta is not None:
            # без известного floor не выдаём — иначе в 60–100k лезет мусор
            if floor is None or floor <= 0:
                stats.price_skip += 1
                lot.skip_reason = "no_floor"
                return False
            # коллекция слишком дешёвая для этого режима
            if floor + float(delta) < self.search_min:
                stats.price_skip += 1
                lot.skip_reason = f"cheap_col fl{floor:.0f}+{delta}<{self.search_min:.0f}"
                return False
            if lot.stars - floor > float(delta) + 1e-6:
                stats.price_skip += 1
                lot.skip_reason = f"floor+{lot.stars - floor:.0f}>{delta}"
                return False
        # ниже флора — ок (редкий дамп), но вдруг floor устарел — подтянем
        if floor is not None and lot.stars > 0 and lot.stars < floor:
            self._note_price(lot)

        if require_fresh and check_rank and lot.market_rank > self.fresh_rank:
            stats.fresh_skip += 1
            lot.skip_reason = f"rank>{self.fresh_rank}"
            return False

        age = store.touch_listing(lot.slug or lot.id, lot.stars)
        lot.listing_age = age
        if require_fresh and age > self.fresh_age:
            stats.fresh_skip += 1
            lot.skip_reason = f"age>{self.fresh_age:.0f}s"
            return False

        await self.resolve_owner(lot, timeout=owner_timeout)
        if lot.seller or lot.seller_id:
            await self.filter_paid_dms([lot], timeout=paid_timeout)

        if lot.paid_dm or not lot.writable:
            stats.paid_skip += 1
            lot.skip_reason = "no_username" if not lot.seller else "paid_dm"
            return False

        stats.with_user += 1

        if store.is_blocked(lot.seller, lot.seller_id):
            stats.black_skip += 1
            lot.skip_reason = "blacklist"
            return False

        await self.enrich_profiles([lot], timeout=profile_timeout)

        if lot.paid_dm or not lot.writable:
            stats.paid_skip += 1
            lot.skip_reason = "paid_dm" if lot.paid_dm else "no_username"
            return False
        if store.is_blocked(lot.seller, lot.seller_id):
            stats.black_skip += 1
            lot.skip_reason = "blacklist"
            return False

        if not self._online_ok(lot):
            stats.online_skip += 1
            lot.skip_reason = "offline"
            return False

        # fail-closed: без lvl / gifts не выдаём
        if lot.level is None:
            stats.level_skip += 1
            lot.skip_reason = "no_lvl"
            return False
        if lot.level > self.max_level:
            stats.level_skip += 1
            lot.skip_reason = f"lvl{lot.level}"
            return False
        if lot.gifts_count is None:
            stats.gifts_skip += 1
            lot.skip_reason = "no_gifts"
            return False
        if lot.gifts_count > self.max_gifts:
            stats.gifts_skip += 1
            lot.skip_reason = f"gifts>{self.max_gifts}"
            return False
        lot.ru_ok = lot.ru_score >= self.min_ru
        if not lot.ru_ok:
            stats.ru_skip += 1
            lot.skip_reason = "no_ru"
            return False
        return True

    def _fair_floor(self, lot: Lot) -> float | None:
        floors: list[float] = []
        if lot.collection_id and lot.collection_id in self._floor_by_id:
            floors.append(self._floor_by_id[lot.collection_id])
        if lot.collection_id:
            stored = store.get_floor(str(lot.collection_id))
            if stored:
                floors.append(stored)
        key = (lot.title or "").strip().lower()
        if key:
            stored_t = store.get_floor(f"t:{key}")
            if stored_t:
                floors.append(stored_t)
            obs = self._obs_prices.get(key, [])
            if obs:
                floors.append(min(obs))
        if not floors:
            return None
        return min(floors)

    def _note_price(self, lot: Lot) -> None:
        key = (lot.title or "").strip().lower()
        price = float(lot.stars)
        if price <= 0:
            return
        if key:
            bucket = self._obs_prices.setdefault(key, [])
            bucket.append(price)
            if len(bucket) > 100:
                del bucket[:-70]
            store.note_floor(f"t:{key}", price, title=lot.title)
        if lot.collection_id:
            cur = self._floor_by_id.get(lot.collection_id)
            if cur is None or price < cur:
                self._floor_by_id[lot.collection_id] = price
            store.note_floor(str(lot.collection_id), price, title=lot.title)

    def _online_ok(self, lot: Lot) -> bool:
        mode = (self.online_mode or "any").lower()
        if mode in {"any", "off", "all", ""}:
            return True
        if mode == "online":
            return bool(lot.online)
        if mode == "recent":
            return bool(lot.online or lot.recently)
        return True

    async def resolve_owners(self, lots: list[Lot], timeout: float = 0.8) -> None:
        sem = asyncio.Semaphore(creds.OWNER_PARALLEL)

        async def one(lot: Lot) -> None:
            async with sem:
                await self.resolve_owner(lot, timeout=timeout)

        await asyncio.gather(*[one(lot) for lot in lots])

    async def filter_paid_dms(self, lots: list[Lot], timeout: float = 2.0) -> list[Lot]:
        if not lots:
            return []

        for lot in lots:
            if lot.paid_dm and lot.seller:
                self._paid_cache[lot.seller.lower()] = True
                if lot.seller_id:
                    self._paid_cache[lot.seller_id] = True
                lot.seller = ""

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

        return [lot for lot in lots if lot.writable]

    async def enrich_profiles(self, lots: list[Lot], timeout: float = 2.2) -> None:
        """Stars level + RU-признаки по био/нику/каналу/подаркам."""
        sem = asyncio.Semaphore(creds.PROFILE_PARALLEL)

        async def one(lot: Lot) -> None:
            async with sem:
                await self._enrich_one(lot, timeout=timeout)

        # уникальные продавцы
        seen: set[int | str] = set()
        unique: list[Lot] = []
        for lot in lots:
            key: int | str = lot.seller_id if lot.seller_id else lot.seller.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(lot)

        await asyncio.gather(*[one(lot) for lot in unique])

        # размазать кэш на остальные лоты того же продавца
        for lot in lots:
            key = lot.seller_id if lot.seller_id else (
                lot.seller.lower() if lot.seller else None
            )
            if key is None:
                continue
            cached = self._profile_cache.get(key)
            if not cached:
                continue
            lot.level = cached.get("level")
            lot.gifts_count = cached.get("gifts_count")
            lot.ru_score = int(cached.get("ru_score", 0))
            lot.ru_ok = lot.ru_score >= self.min_ru
            lot.online = bool(cached.get("online", False))
            lot.recently = bool(cached.get("recently", False))
            if cached.get("paid_dm"):
                lot.paid_dm = True
                lot.seller = ""
            if cached.get("username") and not lot.seller and not lot.paid_dm:
                lot.seller = cached["username"]

    async def _enrich_one(self, lot: Lot, timeout: float) -> None:
        key: int | str = lot.seller_id if lot.seller_id else lot.seller.lower()
        if key in self._profile_cache:
            cached = self._profile_cache[key]
            # неполный кэш не используем как финал — перезапросим
            if cached.get("complete") or (
                cached.get("level") is not None and cached.get("gifts_count") is not None
            ):
                lot.level = cached.get("level")
                lot.gifts_count = cached.get("gifts_count")
                lot.ru_score = int(cached.get("ru_score", 0))
                lot.ru_ok = lot.ru_score >= self.min_ru
                lot.online = bool(cached.get("online", False))
                lot.recently = bool(cached.get("recently", False))
                if cached.get("paid_dm"):
                    lot.paid_dm = True
                    lot.seller = ""
                elif cached.get("username") and not lot.seller:
                    lot.seller = cached["username"]
                return

        peer: Any = lot.seller_id if lot.seller_id else lot.seller
        score = 0
        level: int | None = None
        gifts_count: int | None = None
        paid = False
        username = lot.seller

        try:
            await self._wait_flood()
            full = await asyncio.wait_for(
                self.client(GetFullUserRequest(peer)),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("full user fail %s: %s", peer, exc)
            try:
                ent = await asyncio.wait_for(
                    self.client.get_entity(peer), timeout=min(timeout, 1.0)
                )
                username = _best_username(ent) or username
                score += _ru_points_from_user(ent)
                _apply_status(lot, ent)
                if _user_paid_dm(ent):
                    paid = True
            except Exception:  # noqa: BLE001
                pass
            self._store_profile(
                key,
                lot.seller_id,
                username,
                level,
                gifts_count,
                score,
                paid,
                lot.online,
                lot.recently,
            )
            lot.level = level
            lot.gifts_count = gifts_count
            lot.ru_score = score
            lot.ru_ok = score >= self.min_ru
            if paid:
                lot.paid_dm = True
                lot.seller = ""
            elif username and not lot.seller:
                lot.seller = username
            return

        users = {
            int(u.id): u
            for u in (getattr(full, "users", None) or [])
            if getattr(u, "id", None) is not None
        }
        chats = list(getattr(full, "chats", None) or [])
        full_user = getattr(full, "full_user", None)

        user = None
        if lot.seller_id and lot.seller_id in users:
            user = users[lot.seller_id]
        elif users:
            user = next(iter(users.values()))

        if user is not None:
            username = _best_username(user) or username
            _apply_status(lot, user)
            if lot.seller_id is None:
                try:
                    lot.seller_id = int(user.id)
                    key = lot.seller_id
                except (TypeError, ValueError):
                    pass
            score += _ru_points_from_user(user)
            if _user_paid_dm(user):
                paid = True
            paid_full = getattr(full_user, "send_paid_messages_stars", None)
            if paid_full is not None:
                try:
                    if int(paid_full) > 0:
                        paid = True
                except (TypeError, ValueError):
                    pass

        if full_user is not None:
            about = str(getattr(full_user, "about", "") or "")
            if _has_cyrillic(about):
                score += 3
            rating = getattr(full_user, "stars_rating", None)
            if rating is not None and getattr(rating, "level", None) is not None:
                try:
                    level = int(rating.level)
                except (TypeError, ValueError):
                    level = None
            raw_gifts = getattr(full_user, "stargifts_count", None)
            if raw_gifts is not None:
                try:
                    gifts_count = int(raw_gifts)
                except (TypeError, ValueError):
                    gifts_count = None

            channel_id = getattr(full_user, "personal_channel_id", None)
            if channel_id is not None:
                for chat in chats:
                    try:
                        if int(getattr(chat, "id", 0)) == int(channel_id):
                            score += _ru_points_from_chat(chat)
                            break
                    except (TypeError, ValueError):
                        continue

        # подарки на профиле: ники/тексты отправителей
        # для китов с кучей гифтов RU-probe не нужен — всё равно отсечём
        whale = gifts_count is not None and gifts_count > creds.MAX_PROFILE_GIFTS
        if not paid and not whale and (lot.seller_id or username):
            try:
                score += await self._ru_from_gifts(peer, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                logger.debug("gifts ru fail: %s", exc)

        # после успешного FullUser: нет rating/gifts → 0, не «unknown»
        if level is None:
            level = 0
        if gifts_count is None:
            gifts_count = 0

        self._store_profile(
            key,
            lot.seller_id,
            username,
            level,
            gifts_count,
            score,
            paid,
            lot.online,
            lot.recently,
        )
        if lot.seller_id and lot.seller_id != key:
            self._store_profile(
                lot.seller_id,
                lot.seller_id,
                username,
                level,
                gifts_count,
                score,
                paid,
                lot.online,
                lot.recently,
            )
        if username:
            self._store_profile(
                username.lower(),
                lot.seller_id,
                username,
                level,
                gifts_count,
                score,
                paid,
                lot.online,
                lot.recently,
            )

        lot.level = level
        lot.gifts_count = gifts_count
        lot.ru_score = score
        lot.ru_ok = score >= self.min_ru
        if paid:
            lot.paid_dm = True
            lot.seller = ""
            self._paid_cache[key] = True
            if username:
                self._paid_cache[username.lower()] = True
        elif username:
            lot.seller = username
            if lot.slug:
                self._owner_cache[lot.slug] = username

    def _store_profile(
        self,
        key: int | str,
        seller_id: int | None,
        username: str,
        level: int | None,
        gifts_count: int | None,
        score: int,
        paid: bool,
        online: bool = False,
        recently: bool = False,
    ) -> None:
        payload = {
            "level": level,
            "gifts_count": gifts_count,
            "complete": level is not None and gifts_count is not None,
            "ru_score": score,
            "ru_ok": score >= self.min_ru,
            "paid_dm": paid,
            "username": username,
            "seller_id": seller_id,
            "online": online,
            "recently": recently,
            "ts": time.time(),
        }
        self._profile_cache[key] = payload
        store.set_profile(key, payload)

    async def _ru_from_gifts(self, peer: Any, timeout: float) -> int:
        await self._wait_flood()
        result = await asyncio.wait_for(
            self.client(
                GetSavedStarGiftsRequest(
                    peer=peer,
                    offset="",
                    limit=min(20, creds.GIFTS_PROBE_LIMIT),
                )
            ),
            timeout=timeout,
        )
        score = 0
        users = {
            int(u.id): u
            for u in (getattr(result, "users", None) or [])
            if getattr(u, "id", None) is not None
        }
        chats = {
            int(c.id): c
            for c in (getattr(result, "chats", None) or [])
            if getattr(c, "id", None) is not None
        }
        for gift in getattr(result, "gifts", None) or []:
            msg = getattr(gift, "message", None)
            text = ""
            if msg is not None:
                text = str(getattr(msg, "text", "") or "")
            if _has_cyrillic(text):
                score += 2
            from_id = getattr(gift, "from_id", None)
            if from_id is not None:
                uid = getattr(from_id, "user_id", None)
                cid = getattr(from_id, "channel_id", None) or getattr(
                    from_id, "chat_id", None
                )
                if uid and int(uid) in users:
                    score += _ru_points_from_user(users[int(uid)])
                elif cid and int(cid) in chats:
                    score += _ru_points_from_chat(chats[int(cid)])
            # original details на unique
            inner = getattr(gift, "gift", None)
            for attr in getattr(inner, "attributes", None) or []:
                if "original" not in attr.__class__.__name__.lower():
                    continue
                om = getattr(attr, "message", None)
                otext = str(getattr(om, "text", "") or "") if om else ""
                if _has_cyrillic(otext):
                    score += 2
                sender = getattr(attr, "sender_id", None)
                if sender is not None:
                    sid = getattr(sender, "user_id", None)
                    if sid and int(sid) in users:
                        score += _ru_points_from_user(users[int(sid)])
            if score >= 10:
                break
        return min(score, 10)

    async def _check_paid_batch(self, lots: list[Lot], timeout: float) -> None:
        peers: list[Any] = []
        index: list[Lot] = []
        seen: set[int | str] = set()
        for lot in lots:
            if lot.seller_id:
                key: int | str = lot.seller_id
                peer: Any = lot.seller_id
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
        """Достаёт @username. Если гифт открыт (есть owner_id) — дожимаем FullUser.

        Без реального @username лот не writable → не выдаём. «Скрыт» больше не пишем.
        """
        if lot.paid_dm:
            lot.seller = ""
            return

        # уже есть валидный юз
        if lot.seller and USERNAME_RE.fullmatch(lot.seller):
            lot.owner_hidden = False
            if self._paid_cache.get(lot.seller.lower()) is True:
                lot.paid_dm = True
                lot.seller = ""
            return

        # кэш юзов (не кэшируем id-only как финал — всегда пробуем ещё раз)
        if lot.slug and lot.slug in self._owner_cache:
            cached = self._owner_cache[lot.slug]
            if not cached.startswith("id:"):
                if self._paid_cache.get(cached.lower()) is True:
                    lot.paid_dm = True
                    lot.seller = ""
                    return
                if USERNAME_RE.fullmatch(cached):
                    lot.seller = cached
                    lot.owner_hidden = False
                    return

        if lot.seller_id and self._paid_cache.get(lot.seller_id) is True:
            lot.paid_dm = True
            lot.seller = ""
            return

        # 1) Unique gift по slug — owner есть даже при Hidden в ленте
        if lot.slug:
            try:
                await self._wait_flood()
                result = await asyncio.wait_for(
                    self.client(GetUniqueStarGiftRequest(slug=lot.slug)),
                    timeout=max(timeout, 1.6),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("unique gift %s: %s", lot.slug, exc)
                result = None

            if result is not None:
                gift = getattr(result, "gift", None)
                users = {
                    int(u.id): u
                    for u in (getattr(result, "users", None) or [])
                    if getattr(u, "id", None) is not None
                }
                seller_id = _peer_user_id(getattr(gift, "owner_id", None) if gift else None)
                if seller_id is None and gift is not None:
                    seller_id = _peer_user_id(getattr(gift, "host_id", None))
                if seller_id:
                    lot.seller_id = seller_id
                    user = users.get(seller_id)
                    if user is None and len(users) == 1:
                        user = next(iter(users.values()))
                    if user is not None:
                        _apply_status(lot, user)
                        if _user_paid_dm(user):
                            lot.paid_dm = True
                            self._paid_cache[seller_id] = True
                            lot.seller = ""
                            return
                        username = _best_username(user)
                        if username and USERNAME_RE.fullmatch(username):
                            lot.seller = username
                            lot.owner_hidden = False
                            self._owner_cache[lot.slug] = username
                            return
                    raw = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
                    if (
                        raw
                        and " " not in raw
                        and raw.lower() not in {"hidden", "anonymous", "telegram", ""}
                        and USERNAME_RE.fullmatch(raw)
                    ):
                        lot.seller = raw
                        lot.owner_hidden = False
                        self._owner_cache[lot.slug] = raw
                        return

        # 2) По user_id: FullUser / entity (открытый гифт почти всегда здесь)
        if lot.seller_id and not lot.seller:
            username = await self._username_from_peer(
                lot.seller_id, max(timeout, 1.4)
            )
            if username and USERNAME_RE.fullmatch(username):
                if self._paid_cache.get(username.lower()) is True:
                    lot.paid_dm = True
                    lot.seller = ""
                    return
                lot.seller = username
                lot.owner_hidden = False
                if lot.slug:
                    self._owner_cache[lot.slug] = username
                return
            # юза нет — реально скрыт / без username. Не выдаём.
            lot.owner_hidden = True
            lot.seller = ""
            return

        if not lot.seller:
            lot.owner_hidden = True
            lot.seller = ""

    async def _username_from_peer(self, peer: Any, timeout: float) -> str:
        """Достаёт @username по peer/id."""
        try:
            await self._wait_flood()
            full = await asyncio.wait_for(
                self.client(GetFullUserRequest(peer)),
                timeout=timeout,
            )
            for u in getattr(full, "users", None) or []:
                username = _best_username(u)
                if username and USERNAME_RE.fullmatch(username):
                    return username
        except Exception as exc:  # noqa: BLE001
            logger.debug("fulluser %s: %s", peer, exc)

        try:
            await self._wait_flood()
            ent = await asyncio.wait_for(
                self.client.get_entity(peer), timeout=timeout
            )
            if _user_paid_dm(ent):
                if isinstance(peer, int):
                    self._paid_cache[peer] = True
                return ""
            username = _best_username(ent)
            if username and USERNAME_RE.fullmatch(username):
                return username
        except Exception as exc:  # noqa: BLE001
            logger.debug("entity %s: %s", peer, exc)
        return ""

    async def _fetch_one(
        self,
        gift_id: int,
        limit: int,
        stats: dict[str, int],
        *,
        gap: float,
        timeout: float,
    ) -> list[Lot]:
        result = await self._request(gift_id, limit, stats, gap, timeout)
        lots = _parse_result(result) if result is not None else []
        for lot in lots:
            self._note_price(lot)
        if lots:
            stats["ok"] += 1
        elif result is None:
            stats["errors"] += 1
        return lots

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
                            sort_by_price=True,
                            stars_only=True,
                        )
                    ),
                    timeout=timeout,
                )
            except FloodWaitError as exc:
                stats["floods"] += 1
                self._flood_until = time.monotonic() + min(float(exc.seconds) + 0.2, 15.0)
                self.last_error = f"FloodWait {exc.seconds}s"
                await asyncio.sleep(min(float(exc.seconds), 1.5))
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                self.last_error = str(exc)
                await asyncio.sleep(0.08 * (attempt + 1))
        return None

    async def _wait_flood(self) -> None:
        delay = self._flood_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


def diversify_lots(lots: list[Lot]) -> list[Lot]:
    """Жёстко перемешать: round-robin по коллекциям и владельцам, без подряд."""
    gap = int(getattr(creds, "EMIT_GAP", 5))
    mixer = EmitMixer(gap=gap)
    shuffled = list(lots)
    random.shuffle(shuffled)
    out: list[Lot] = []
    for lot in shuffled:
        out.extend(mixer.push(lot))
    while True:
        nxt = mixer.pop()
        if nxt is None:
            break
        out.append(nxt)
    mixer.drop_rest()
    return out


class EmitMixer:
    """Не выдаёт подряд одинаковый title ИЛИ одинакового владельца."""

    def __init__(self, gap: int = 5) -> None:
        self.gap = max(3, int(gap))
        self.buckets: dict[str, deque[Lot]] = defaultdict(deque)
        self.recent_titles: deque[str] = deque(maxlen=self.gap)
        self.recent_sellers: deque[str] = deque(maxlen=self.gap)
        self.last_title = ""
        self.last_seller = ""

    def _key(self, lot: Lot) -> str:
        return (lot.title or "?").strip().lower() or "?"

    def _seller_key(self, lot: Lot) -> str:
        return (lot.seller or "").strip().lower()

    def _allowed(self, lot: Lot) -> bool:
        title = self._key(lot)
        seller = self._seller_key(lot)
        if title == self.last_title or title in self.recent_titles:
            return False
        if seller and (seller == self.last_seller or seller in self.recent_sellers):
            return False
        return True

    def _emit(self, lot: Lot) -> Lot:
        title = self._key(lot)
        seller = self._seller_key(lot)
        self.recent_titles.append(title)
        self.last_title = title
        if seller:
            self.recent_sellers.append(seller)
            self.last_seller = seller
        return lot

    def push(self, lot: Lot) -> list[Lot]:
        self.buckets[self._key(lot)].append(lot)
        out: list[Lot] = []
        for _ in range(len(self.buckets) + 4):
            nxt = self.pop()
            if nxt is None:
                break
            out.append(nxt)
        return out

    def pop(self) -> Lot | None:
        if not self.buckets:
            return None
        keys = list(self.buckets.keys())
        random.shuffle(keys)
        # сначала те, кого точно можно
        for key in keys:
            bucket = self.buckets.get(key)
            if not bucket:
                self.buckets.pop(key, None)
                continue
            cand = bucket[0]
            if not self._allowed(cand):
                continue
            bucket.popleft()
            if not bucket:
                self.buckets.pop(key, None)
            return self._emit(cand)
        return None

    def drop_rest(self) -> int:
        n = sum(len(b) for b in self.buckets.values())
        self.buckets.clear()
        return n


def _peer_user_id(peer: Any) -> int | None:
    if peer is None:
        return None
    uid = getattr(peer, "user_id", None)
    if uid is None:
        uid = getattr(peer, "id", None)
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def _has_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text or ""))


def _ru_points_from_user(user: Any) -> int:
    score = 0
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    if _has_cyrillic(first) or _has_cyrillic(last):
        score += 2
    # lang сам по себе не хватает на MIN_RU_SCORE=2 — нужна кириллица
    lang = str(getattr(user, "lang_code", "") or "").lower()
    if lang.startswith("ru") or lang.startswith("uk") or lang.startswith("be"):
        score += 1
    return score


def _ru_points_from_chat(chat: Any) -> int:
    score = 0
    title = str(getattr(chat, "title", "") or "")
    username = str(getattr(chat, "username", "") or "")
    about = str(getattr(chat, "about", "") or "")
    if _has_cyrillic(title) or _has_cyrillic(about):
        score += 2
    if _has_cyrillic(username):
        score += 1
    return score


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
    for rank, gift in enumerate(getattr(result, "gifts", []) or []):
        lot = _parse(gift, users)
        if lot:
            lot.seen_at = now
            lot.market_rank = rank
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
    """Только StarsAmount. TON-only лоты не берём (рынок Stars)."""
    amounts = getattr(gift, "resell_amount", None)
    stars_val: float | None = None

    if isinstance(amounts, list):
        for item in amounts:
            amount = getattr(item, "amount", None)
            if amount is None:
                continue
            try:
                val = float(amount)
            except (TypeError, ValueError):
                continue
            if val <= 0:
                continue
            # TON / nanotons — пропускаем
            if isinstance(item, StarsTonAmount) or item.__class__.__name__ == "StarsTonAmount":
                continue
            if isinstance(item, StarsAmount) or item.__class__.__name__ == "StarsAmount":
                # amount = целые Stars, nanos — дробная часть
                nanos = getattr(item, "nanos", 0) or 0
                try:
                    stars_val = val + float(nanos) / NANOTON
                except (TypeError, ValueError):
                    stars_val = val
                break
            if stars_val is None:
                stars_val = val
        return stars_val

    # одиночное поле
    if isinstance(amounts, StarsTonAmount) or (
        amounts is not None and amounts.__class__.__name__ == "StarsTonAmount"
    ):
        return None
    if isinstance(amounts, StarsAmount) or (
        amounts is not None and amounts.__class__.__name__ == "StarsAmount"
    ):
        try:
            base = float(amounts.amount)
            nanos = float(getattr(amounts, "nanos", 0) or 0)
            return base + nanos / NANOTON
        except (TypeError, ValueError):
            return None

    for attr in ("resell_stars", "stars", "price"):
        val = getattr(gift, attr, None)
        if val is None:
            continue
        if isinstance(val, StarsTonAmount) or (
            hasattr(val, "__class__") and val.__class__.__name__ == "StarsTonAmount"
        ):
            continue
        if hasattr(val, "amount"):
            try:
                raw = float(val.amount)
            except (TypeError, ValueError):
                continue
            if val.__class__.__name__ == "StarsTonAmount" or isinstance(
                val, StarsTonAmount
            ):
                continue
            nanos = getattr(val, "nanos", 0) or 0
            try:
                return raw + float(nanos) / NANOTON
            except (TypeError, ValueError):
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
    seller_id = _peer_user_id(getattr(gift, "owner_id", None))
    if seller_id is None:
        seller_id = _peer_user_id(getattr(gift, "host_id", None))
    if seller_id and users:
        user = users.get(seller_id)
        if user is None and len(users) == 1:
            user = next(iter(users.values()))
        if user is not None:
            if _user_paid_dm(user):
                paid_dm = True
            else:
                seller = _best_username(user)

    if not seller and not paid_dm:
        raw = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
        if (
            raw
            and raw.lower() not in {"hidden", "anonymous", "telegram"}
            and " " not in raw
            and USERNAME_RE.fullmatch(raw)
        ):
            seller = raw

    collection_id = None
    raw_cid = getattr(gift, "gift_id", None)
    if raw_cid is not None:
        try:
            collection_id = int(raw_cid)
        except (TypeError, ValueError):
            collection_id = None

    number_i = int(number) if number is not None else None
    lot_id = str(slug or gift_id or f"{title}-{number_i}")
    lot = Lot(
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
        collection_id=collection_id,
    )
    if seller_id and users and seller_id in users:
        _apply_status(lot, users[seller_id])
    return lot


def _apply_status(lot: Lot, user: Any) -> None:
    status = getattr(user, "status", None)
    if status is None:
        return
    name = status.__class__.__name__
    if isinstance(status, UserStatusOnline) or name == "UserStatusOnline":
        lot.online = True
        lot.recently = True
    elif isinstance(status, UserStatusRecently) or name == "UserStatusRecently":
        lot.recently = True
        lot.online = False
    elif name in {"UserStatusLastWeek", "UserStatusLastMonth"}:
        lot.recently = False
        lot.online = False
