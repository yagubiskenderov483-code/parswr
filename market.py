"""
Telegram Market scanner.

- быстрый burst + чеки
- резолв скрытых гифтов → @username
- без юзов с ЛС за Stars
- только RU-признаки (био/ник/канал/подарки)
- Stars Rating level ≤ MAX_ACCOUNT_LEVEL (режем 5/6/8+)
- режем китов с кучей гифтов на профиле
- выдача вразнобой (не пачками одной коллекции)
- burst-парсинг маркета ≤ ~3с
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
    market_rank: int = 0  # 0 = самый свежий в ответе коллекции
    listing_age: float = 0.0
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
        return bool(self.seller) and not self.paid_dm and USERNAME_RE.fullmatch(self.seller)


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
        self.check_no = 0
        self.last_error = ""
        # runtime filters (Settings может менять)
        self.max_level = creds.MAX_ACCOUNT_LEVEL
        self.max_gifts = creds.MAX_PROFILE_GIFTS
        self.min_ru = creds.MIN_RU_SCORE
        self.fresh_age = float(creds.FRESH_MAX_AGE_SEC)
        self.fresh_rank = int(creds.FRESH_MAX_RANK)
        self._hydrate_profiles()

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
        self._cursor = 0
        self._flood_until = 0.0
        self.check_no = 0
        self.last_error = ""
        self._hydrate_profiles()

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
        random.shuffle(ids)
        self._gift_ids = ids
        self._cursor = 0
        logger.info("collections=%s", len(ids))
        return ids

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

        # каждый burst — свежий срез коллекций
        batch = list(gift_ids)
        random.shuffle(batch)
        batch = batch[:max_collections]
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
        take = min(n, batch_size)
        batch = [gift_ids[(self._cursor + i) % n] for i in range(take)]
        self._cursor = (self._cursor + take) % n

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
        while done < total and emitted < cap:
            item = await queue.get()
            done += 1
            if item is None:
                continue
            emitted += 1
            stats.kept = emitted
            yield item

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
        # свежесть по позиции в маркете + возраст наблюдения
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

        if lot.paid_dm or not lot.seller:
            stats.paid_skip += 1
            lot.skip_reason = "paid_or_no_user"
            return False

        stats.with_user += 1

        if store.is_blocked(lot.seller, lot.seller_id):
            stats.black_skip += 1
            lot.skip_reason = "blacklist"
            return False

        await self.enrich_profiles([lot], timeout=profile_timeout)

        if lot.paid_dm or not lot.seller:
            stats.paid_skip += 1
            lot.skip_reason = "paid_dm"
            return False
        if store.is_blocked(lot.seller, lot.seller_id):
            stats.black_skip += 1
            lot.skip_reason = "blacklist"
            return False

        level = lot.level if lot.level is not None else 0
        if level > self.max_level:
            stats.level_skip += 1
            lot.skip_reason = f"lvl{level}"
            return False
        gifts_n = lot.gifts_count if lot.gifts_count is not None else 0
        if gifts_n > self.max_gifts:
            stats.gifts_skip += 1
            lot.skip_reason = f"gifts>{self.max_gifts}"
            return False
        # ru_ok пересчитаем относительно текущего порога
        lot.ru_ok = lot.ru_score >= self.min_ru
        if not lot.ru_ok:
            stats.ru_skip += 1
            lot.skip_reason = "no_ru"
            return False
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
            if cached.get("paid_dm"):
                lot.paid_dm = True
                lot.seller = ""
            if cached.get("username") and not lot.seller and not lot.paid_dm:
                lot.seller = cached["username"]

    async def _enrich_one(self, lot: Lot, timeout: float) -> None:
        key: int | str = lot.seller_id if lot.seller_id else lot.seller.lower()
        if key in self._profile_cache:
            cached = self._profile_cache[key]
            lot.level = cached.get("level")
            lot.gifts_count = cached.get("gifts_count")
            lot.ru_score = int(cached.get("ru_score", 0))
            lot.ru_ok = lot.ru_score >= self.min_ru
            if cached.get("paid_dm"):
                lot.paid_dm = True
                lot.seller = ""
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
                if _user_paid_dm(ent):
                    paid = True
            except Exception:  # noqa: BLE001
                pass
            self._store_profile(
                key, lot.seller_id, username, level, gifts_count, score, paid
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

        self._store_profile(
            key, lot.seller_id, username, level, gifts_count, score, paid
        )
        if lot.seller_id and lot.seller_id != key:
            self._store_profile(
                lot.seller_id, lot.seller_id, username, level, gifts_count, score, paid
            )
        if username:
            self._store_profile(
                username.lower(), lot.seller_id, username, level, gifts_count, score, paid
            )

        lot.level = level if level is not None else 0
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
    ) -> None:
        payload = {
            "level": 0 if level is None else level,
            "gifts_count": gifts_count,
            "ru_score": score,
            "ru_ok": score >= self.min_ru,
            "paid_dm": paid,
            "username": username,
            "seller_id": seller_id,
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

    async def resolve_owner(self, lot: Lot, timeout: float = 0.8) -> None:
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
            # юзера есть, юза нет — всё равно сохраним id для GetFullUser
            return

        if gift and not lot.seller:
            raw = str(getattr(gift, "owner_name", "") or "").strip().lstrip("@")
            if (
                raw
                and " " not in raw
                and raw.lower() not in {"hidden", "anonymous", "telegram"}
                and USERNAME_RE.fullmatch(raw)
            ):
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
    ) -> list[Lot]:
        result = await self._request(gift_id, limit, stats, gap, timeout)
        lots = _parse_result(result) if result is not None else []
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
                            stars_only=None,
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
    """Не выдавать подряд одну коллекцию / одного продавца."""
    by_title: dict[str, deque[Lot]] = defaultdict(deque)
    for lot in lots:
        by_title[lot.title or "?"].append(lot)

    mixed: list[Lot] = []
    keys = list(by_title.keys())
    random.shuffle(keys)
    while by_title:
        progress = False
        for key in list(keys):
            bucket = by_title.get(key)
            if not bucket:
                by_title.pop(key, None)
                continue
            mixed.append(bucket.popleft())
            progress = True
            if not bucket:
                by_title.pop(key, None)
        keys = list(by_title.keys())
        if not progress:
            break

    # второй проход: развести одинаковых продавцов
    out: list[Lot] = []
    pending = deque(mixed)
    last_seller = ""
    guard = 0
    while pending and guard < len(mixed) * 3:
        guard += 1
        lot = pending.popleft()
        if lot.seller and lot.seller == last_seller and pending:
            pending.append(lot)
            continue
        out.append(lot)
        last_seller = lot.seller
    while pending:
        out.append(pending.popleft())
    return out


def _has_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text or ""))


def _ru_points_from_user(user: Any) -> int:
    score = 0
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    if _has_cyrillic(first) or _has_cyrillic(last):
        score += 2
    username = _best_username(user)
    # транслит-ники не считаем, только явная кириллица в имени
    lang = str(getattr(user, "lang_code", "") or "").lower()
    if lang.startswith("ru") or lang.startswith("uk") or lang.startswith("be"):
        score += 2
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
                stars_val = val
        if stars_val is not None:
            return stars_val
        if ton_val is not None:
            return _ton_nano_to_stars(ton_val)
        return None

    for attr in ("resell_stars", "stars", "price"):
        val = getattr(gift, attr, None)
        if val is None:
            continue
        if hasattr(val, "amount"):
            try:
                raw = float(val.amount)
            except (TypeError, ValueError):
                continue
            if val.__class__.__name__ == "StarsTonAmount" or isinstance(
                val, StarsTonAmount
            ):
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
        if (
            raw
            and raw.lower() not in {"hidden", "anonymous", "telegram"}
            and " " not in raw
            and USERNAME_RE.fullmatch(raw)
        ):
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
