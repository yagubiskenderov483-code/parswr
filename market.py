"""Внутренний маркет Telegram: payments.getResaleStarGifts + профиль продавца."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
)
from telethon.tl.functions.users import GetFullUserRequest, GetRequirementsToContactRequest
from telethon.tl.types import (
    StarsAmount,
    StarsTonAmount,
    RequirementToContactPaidMessages,
    RequirementToContactPremium,
)
from telethon.tl.types.payments import StarGiftsNotModified

try:
    from telethon.tl.functions.payments import GetSavedStarGiftsRequest
except ImportError:  # старый Telethon
    GetSavedStarGiftsRequest = None  # type: ignore[misc, assignment]

try:
    from telethon.tl.functions.stories import GetPeerStoriesRequest
except ImportError:
    GetPeerStoriesRequest = None  # type: ignore[misc, assignment]

logger = logging.getLogger("market")


@dataclass(slots=True)
class Lot:
    id: str
    title: str
    number: int | None
    stars: float
    slug: str
    model: str = ""
    seller: str = ""
    seller_id: int | None = None
    first_name: str = ""
    last_name: str = ""
    about: str = ""
    is_premium: bool | None = None
    account_level: int | None = None
    gifts_count: int | None = None
    free_dm: bool | None = None
    paid_dm_stars: int | None = None
    personal_channel: str = ""
    has_photo: bool = False
    emoji_status: str = ""
    stories_text: str = ""
    gifts_text: str = ""
    lang_code: str = ""
    collection_id: int | None = None
    discovered_at: float = field(default_factory=time.time)

    @property
    def nft_url(self) -> str:
        if self.slug:
            return f"https://t.me/nft/{self.slug}"
        if self.number is not None:
            clean = "".join(ch for ch in self.title if ch.isalnum())
            return f"https://t.me/nft/{clean}-{self.number}"
        return "https://t.me/nft/"

    @property
    def seller_key(self) -> str:
        if self.seller:
            return self.seller.lower().lstrip("@").strip()
        if self.seller_id is not None:
            return f"id:{int(self.seller_id)}"
        return ""


def format_level(lot: Lot) -> str:
    if lot.account_level is None or lot.account_level < 0:
        return "—"
    return str(lot.account_level)


def _normalize_level(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _username_of(user: Any) -> str:
    username = str(getattr(user, "username", "") or "").lstrip("@").strip()
    if username:
        return username
    for alt in getattr(user, "usernames", None) or []:
        name = str(getattr(alt, "username", "") or "").lstrip("@").strip()
        if name and getattr(alt, "active", True):
            return name
    return ""


def _photo_flag(user: Any) -> bool:
    photo = getattr(user, "photo", None)
    if photo is None:
        return False
    name = type(photo).__name__
    return "Empty" not in name


def _emoji_status_text(user: Any) -> str:
    st = getattr(user, "emoji_status", None)
    if st is None:
        return ""
    doc = getattr(st, "document_id", None)
    if doc is not None:
        return f"emoji:{doc}"
    return type(st).__name__


def fill_user(lot: Lot, user: Any) -> None:
    username = _username_of(user)
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
    if getattr(user, "premium", None) is not None:
        lot.is_premium = bool(user.premium)
    lc = str(getattr(user, "lang_code", "") or "").strip().lower()
    if lc:
        lot.lang_code = lc
    lot.has_photo = lot.has_photo or _photo_flag(user)
    emoji = _emoji_status_text(user)
    if emoji:
        lot.emoji_status = emoji
    rating = getattr(user, "stars_rating", None)
    if rating is not None and lot.account_level is None:
        lot.account_level = _normalize_level(getattr(rating, "level", None))
    if hasattr(user, "send_paid_messages_stars"):
        raw = getattr(user, "send_paid_messages_stars", None)
        if raw is not None:
            try:
                paid = int(raw)
            except (TypeError, ValueError):
                paid = None
            if paid is not None:
                lot.paid_dm_stars = paid
                lot.free_dm = paid <= 0


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


def parse_gift(gift: Any, users: dict[int, Any] | None = None) -> Lot | None:
    gift_id = getattr(gift, "id", None)
    slug = str(getattr(gift, "slug", None) or "")
    title = str(getattr(gift, "title", None) or "Gift")
    number = getattr(gift, "num", None)
    stars = _extract_stars(gift)
    if stars is None or stars <= 0:
        return None
    model = ""
    for attr in getattr(gift, "attributes", None) or []:
        cls = attr.__class__.__name__.lower()
        name = str(getattr(attr, "name", "") or getattr(attr, "text", "") or "")
        if "model" in cls:
            model = name
    seller_id: int | None = None
    owner = getattr(gift, "owner_id", None)
    if owner is not None:
        raw = getattr(owner, "user_id", None) or getattr(owner, "id", None)
        try:
            seller_id = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            seller_id = None
    number_i = int(number) if number is not None else None
    collection_id: int | None = None
    raw_coll = getattr(gift, "gift_id", None)
    if raw_coll is not None:
        try:
            collection_id = int(raw_coll)
        except (TypeError, ValueError):
            collection_id = None
    lot = Lot(
        id=str(gift_id or slug or f"{title}-{number_i}"),
        title=title,
        number=number_i,
        stars=float(stars),
        slug=slug,
        model=model,
        seller_id=seller_id,
        collection_id=collection_id,
    )
    if seller_id and users and seller_id in users:
        fill_user(lot, users[seller_id])
    return lot


def parse_result(result: Any) -> list[Lot]:
    users = {
        int(u.id): u
        for u in (getattr(result, "users", None) or [])
        if getattr(u, "id", None) is not None
    }
    lots: list[Lot] = []
    now = time.time()
    for gift in getattr(result, "gifts", []) or []:
        lot = parse_gift(gift, users)
        if lot:
            lot.discovered_at = now
            lots.append(lot)
    return lots


class TelegramMarket:
    def __init__(self, client: TelegramClient, catalog_file: Path | None = None) -> None:
        self.client = client
        self.catalog_file = catalog_file
        self.gift_ids: list[int] = []
        self._hash = 0
        self._cursor = 0
        self._flood_until = 0.0
        self._gap_lock = asyncio.Lock()
        self._last_req = 0.0
        self._rpc_sem = asyncio.Semaphore(2)
        self._profile_cache: dict[int, dict[str, Any]] = {}
        self.last_error = ""

    async def ensure_connected(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()

    async def _wait_flood(self) -> None:
        delay = self._flood_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _gap(self, gap: float) -> None:
        async with self._gap_lock:
            wait = gap - (time.monotonic() - self._last_req)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_req = time.monotonic()

    def _load_catalog(self) -> None:
        if self.gift_ids or not self.catalog_file or not self.catalog_file.exists():
            return
        try:
            data = json.loads(self.catalog_file.read_text(encoding="utf-8"))
            ids = [int(x) for x in data.get("gift_ids", [])]
            if ids:
                self.gift_ids = ids
                self._hash = int(data.get("hash", 0) or 0)
        except (OSError, ValueError, TypeError):
            pass

    def _save_catalog(self) -> None:
        if not self.catalog_file or not self.gift_ids:
            return
        path = self.catalog_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"gift_ids": self.gift_ids, "hash": self._hash}),
            encoding="utf-8",
        )
        tmp.replace(path)

    @staticmethod
    def _ids_from_gifts(gifts: Any) -> list[int]:
        ids: list[int] = []
        for gift in gifts or []:
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
        return ids

    async def load_collections(self, force: bool = False) -> list[int]:
        self._load_catalog()
        if self.gift_ids and not force:
            return self.gift_ids
        await self.ensure_connected()
        last: Exception | None = None
        for attempt in range(4):
            try:
                await self._wait_flood()
                result = await asyncio.wait_for(
                    self.client(GetStarGiftsRequest(hash=0 if attempt >= 2 else self._hash)),
                    timeout=20.0,
                )
                if isinstance(result, StarGiftsNotModified) or (
                    result.__class__.__name__ == "StarGiftsNotModified"
                ):
                    if self.gift_ids:
                        return self.gift_ids
                    result = await asyncio.wait_for(
                        self.client(GetStarGiftsRequest(hash=0)),
                        timeout=20.0,
                    )
                gifts = getattr(result, "gifts", []) or []
                ids = self._ids_from_gifts(gifts)
                if not ids:
                    ids = [
                        int(g.id)
                        for g in gifts
                        if getattr(g, "id", None) is not None
                    ]
                try:
                    self._hash = int(getattr(result, "hash", 0) or 0)
                except (TypeError, ValueError):
                    self._hash = 0
                if ids:
                    self.gift_ids = ids
                    self._save_catalog()
                    logger.info("Коллекций маркета: %s", len(ids))
                    return ids
            except Exception as exc:  # noqa: BLE001
                last = exc
                self.last_error = str(exc)
                await asyncio.sleep(1.5 * (attempt + 1))
        if last:
            logger.error("GetStarGifts: %s", last)
        return self.gift_ids

    def next_batch(self, n: int) -> list[int]:
        if not self.gift_ids:
            return []
        total = len(self.gift_ids)
        take = min(max(1, n), total)
        batch = [self.gift_ids[(self._cursor + i) % total] for i in range(take)]
        self._cursor = (self._cursor + take) % total
        return batch

    async def fetch_page(
        self,
        gift_id: int,
        *,
        limit: int = 12,
        timeout: float = 8.0,
        gap: float = 0.02,
        sort_by_price: bool = False,
    ) -> list[Lot]:
        stats = {"errors": 0, "floods": 0}
        result = await self._request(
            gift_id, limit, True, stats, gap, timeout, sort_by_price=sort_by_price
        )
        if result is None:
            result = await self._request(
                gift_id, limit, False, stats, gap, timeout, sort_by_price=sort_by_price
            )
        if result is None:
            return []
        lots = parse_result(result)
        for lot in lots:
            lot.collection_id = int(gift_id)
        return lots

    async def _request(
        self,
        gift_id: int,
        limit: int,
        stars_only: bool,
        stats: dict[str, int],
        gap: float,
        timeout: float,
        *,
        offset: str = "",
        sort_by_price: bool = False,
    ) -> Any | None:
        for attempt in range(2):
            try:
                await self._wait_flood()
                await self.ensure_connected()
                async with self._rpc_sem:
                    await self._gap(gap)
                    return await asyncio.wait_for(
                        self.client(
                            GetResaleStarGiftsRequest(
                                gift_id=gift_id,
                                offset=offset or "",
                                limit=min(limit, 50),
                                stars_only=True if stars_only else None,
                                sort_by_price=True if sort_by_price else None,
                            )
                        ),
                        timeout=timeout,
                    )
            except FloodWaitError as exc:
                stats["floods"] += 1
                wait_s = float(exc.seconds) + 1.5
                self._flood_until = time.monotonic() + min(wait_s, 300.0)
                self.last_error = f"FloodWait {exc.seconds}s"
                await asyncio.sleep(min(wait_s, 120.0))
            except asyncio.TimeoutError:
                stats["errors"] += 1
                self.last_error = f"таймаут {timeout:g}s"
                await asyncio.sleep(0.2 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.2 * (attempt + 1))
        return None

    async def resolve_owner(self, lot: Lot, timeout: float = 4.0) -> None:
        if lot.seller and lot.seller_id is not None:
            return
        if lot.seller_id:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_entity(lot.seller_id), timeout=timeout
                )
                fill_user(lot, ent)
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
            raw = getattr(owner, "user_id", None) or getattr(owner, "id", None)
            try:
                seller_id = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                seller_id = None
        if seller_id:
            lot.seller_id = seller_id
            if seller_id in users:
                fill_user(lot, users[seller_id])

    async def enrich_profile(self, lot: Lot, timeout: float = 5.0) -> None:
        if not lot.seller_id:
            return
        cached = self._profile_cache.get(int(lot.seller_id))
        if cached:
            _apply_cache(lot, cached)
            return
        try:
            await self._wait_flood()
            full = await asyncio.wait_for(
                self.client(GetFullUserRequest(lot.seller_id)),
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            return
        for u in getattr(full, "users", None) or []:
            if getattr(u, "id", None) == lot.seller_id:
                fill_user(lot, u)
                break
        uf = getattr(full, "full_user", None)
        if uf is not None:
            about = str(getattr(uf, "about", "") or "")
            if about:
                lot.about = about
            raw_ch = getattr(uf, "personal_channel_id", None)
            if raw_ch:
                lot.personal_channel = str(raw_ch)
            raw_gifts = getattr(uf, "stargifts_count", None)
            if raw_gifts is not None:
                try:
                    lot.gifts_count = int(raw_gifts)
                except (TypeError, ValueError):
                    pass
            rating = getattr(uf, "stars_rating", None)
            if rating is not None:
                level = _normalize_level(getattr(rating, "level", None))
                if level is not None:
                    lot.account_level = level
            if hasattr(uf, "send_paid_messages_stars"):
                raw_paid = getattr(uf, "send_paid_messages_stars", None)
                if raw_paid is not None:
                    try:
                        paid = int(raw_paid)
                    except (TypeError, ValueError):
                        paid = None
                    if paid is not None and paid > 0:
                        lot.free_dm = False
                        lot.paid_dm_stars = paid
        self._profile_cache[int(lot.seller_id)] = _cache_from(lot)
        await self._extra_signals(lot, timeout=timeout)

    async def _extra_signals(self, lot: Lot, timeout: float = 4.0) -> None:
        """Сторис + названия подарков — для женского фильтра."""
        if not lot.seller_id:
            return
        if GetPeerStoriesRequest is not None and not lot.stories_text:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_input_entity(lot.seller_id), timeout=timeout
                )
                stories = await asyncio.wait_for(
                    self.client(GetPeerStoriesRequest(peer=ent)),
                    timeout=timeout,
                )
                texts: list[str] = []
                peer_stories = getattr(stories, "stories", None)
                items = getattr(peer_stories, "stories", None) or []
                for item in items:
                    cap = str(getattr(item, "caption", "") or "")
                    if cap:
                        texts.append(cap)
                if texts:
                    lot.stories_text = " ".join(texts)[:400]
            except Exception:  # noqa: BLE001
                pass
        if GetSavedStarGiftsRequest is not None and not lot.gifts_text:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_input_entity(lot.seller_id), timeout=timeout
                )
                saved = await asyncio.wait_for(
                    self.client(
                        GetSavedStarGiftsRequest(
                            peer=ent,
                            offset="",
                            limit=20,
                            exclude_unlimited=True,
                        )
                    ),
                    timeout=timeout,
                )
                titles: list[str] = []
                unique = 0
                for item in getattr(saved, "gifts", None) or []:
                    gift = getattr(item, "gift", None) or item
                    title = str(
                        getattr(gift, "title", "") or getattr(gift, "slug", "") or ""
                    )
                    if title:
                        titles.append(title)
                    if getattr(gift, "slug", None) or "Unique" in type(gift).__name__:
                        unique += 1
                if titles:
                    lot.gifts_text = " ".join(titles)[:400]
                if unique and (lot.gifts_count is None or unique < lot.gifts_count):
                    lot.gifts_count = unique
            except Exception:  # noqa: BLE001
                pass
        cached = self._profile_cache.get(int(lot.seller_id))
        if cached is not None:
            cached.update(_cache_from(lot))

    async def check_free_dm(self, lot: Lot, timeout: float = 4.0) -> None:
        if lot.seller_id is None:
            return
        try:
            ent = await asyncio.wait_for(
                self.client.get_input_entity(int(lot.seller_id)), timeout=timeout
            )
            await self._wait_flood()
            result = await asyncio.wait_for(
                self.client(GetRequirementsToContactRequest(id=[ent])),
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            return
        reqs = list(result or [])
        if not reqs:
            lot.free_dm = True
            return
        req = reqs[0]
        name = req.__class__.__name__
        if isinstance(req, RequirementToContactPaidMessages) or name == (
            "RequirementToContactPaidMessages"
        ):
            try:
                paid = int(getattr(req, "stars_amount", 0) or 0)
            except (TypeError, ValueError):
                paid = 1
            lot.paid_dm_stars = paid
            lot.free_dm = paid <= 0
            return
        if isinstance(req, RequirementToContactPremium) or name == (
            "RequirementToContactPremium"
        ):
            lot.free_dm = False
            return
        lot.free_dm = True

    async def enrich_lot(self, lot: Lot, timeout: float = 5.0) -> None:
        await self.resolve_owner(lot, timeout=timeout)
        if lot.seller_id is None:
            return
        await self.enrich_profile(lot, timeout=timeout)
        if lot.free_dm is None:
            await self.check_free_dm(lot, timeout=timeout)


def _cache_from(lot: Lot) -> dict[str, Any]:
    return {
        "username": lot.seller,
        "first_name": lot.first_name,
        "last_name": lot.last_name,
        "about": lot.about,
        "is_premium": lot.is_premium,
        "account_level": lot.account_level,
        "gifts_count": lot.gifts_count,
        "free_dm": lot.free_dm,
        "paid_dm_stars": lot.paid_dm_stars,
        "personal_channel": lot.personal_channel,
        "has_photo": lot.has_photo,
        "emoji_status": lot.emoji_status,
        "stories_text": lot.stories_text,
        "gifts_text": lot.gifts_text,
    }


def _apply_cache(lot: Lot, info: dict[str, Any]) -> None:
    if info.get("username") and not lot.seller:
        lot.seller = str(info["username"])
    if info.get("first_name") and not lot.first_name:
        lot.first_name = str(info["first_name"])
    if info.get("last_name") and not lot.last_name:
        lot.last_name = str(info["last_name"])
    if info.get("about"):
        lot.about = str(info["about"])
    if info.get("is_premium") is not None:
        lot.is_premium = bool(info["is_premium"])
    if info.get("account_level") is not None:
        lot.account_level = _normalize_level(info["account_level"])
    if info.get("gifts_count") is not None:
        lot.gifts_count = int(info["gifts_count"])
    if info.get("free_dm") is not None:
        lot.free_dm = bool(info["free_dm"])
    if info.get("paid_dm_stars") is not None:
        try:
            lot.paid_dm_stars = int(info["paid_dm_stars"])
        except (TypeError, ValueError):
            pass
    if info.get("personal_channel"):
        lot.personal_channel = str(info["personal_channel"])
    if info.get("has_photo"):
        lot.has_photo = True
    if info.get("emoji_status"):
        lot.emoji_status = str(info["emoji_status"])
    if info.get("stories_text"):
        lot.stories_text = str(info["stories_text"])
    if info.get("gifts_text"):
        lot.gifts_text = str(info["gifts_text"])
