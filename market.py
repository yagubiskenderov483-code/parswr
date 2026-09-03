"""Внутренний маркет Telegram: payments.getResaleStarGifts + профиль продавца."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import struct
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config

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

STAR_GIFT_CTOR = 0x313A9547
_PUBLIC_CATALOG_URLS = (
    "https://api.changes.tg/ids",
    "https://cdn.changes.tg/gifts/id-to-name.json",
)
_BUNDLED_CATALOG = Path(__file__).resolve().parent / "gift_catalog.json"


def merge_ids(*groups: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for group in groups:
        for gid in group or []:
            try:
                n = int(gid)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def ids_from_json_payload(data: Any) -> list[int]:
    found: list[int] = []

    def add(raw: Any) -> None:
        if raw is None or isinstance(raw, bool):
            return
        if isinstance(raw, float) and not raw.is_integer():
            return
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            return
        if gid > 10**12:
            found.append(gid)

    if isinstance(data, dict):
        for item in data.get("gift_ids") or []:
            add(item)
        for key, val in data.items():
            if key == "gift_ids":
                continue
            add(key)
            if isinstance(val, dict):
                add(val.get("telegram_id") or val.get("gift_id") or val.get("id"))
            else:
                add(val)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                add(item.get("telegram_id") or item.get("gift_id") or item.get("id"))
            else:
                add(item)
    return merge_ids(found)


def extract_star_gift_ids(data: bytes) -> list[int]:
    """Достаёт gift id из сырого TL, даже если StarGift не парсится целиком."""
    needle = struct.pack("<I", STAR_GIFT_CTOR)
    found: list[int] = []
    start = 0
    while True:
        pos = data.find(needle, start)
        if pos < 0:
            break
        if pos + 16 > len(data):
            break
        gid = struct.unpack_from("<q", data, pos + 8)[0]
        if gid > 10**12:
            found.append(gid)
        start = pos + 4
    return merge_ids(found)


class GetStarGiftsIdsRequest(GetStarGiftsRequest):
    """getStarGifts: распарсенный список или id из сырых байт."""

    @staticmethod
    def read_result(reader):  # noqa: ANN001
        start = reader.tell_position()
        raw = bytes(reader.get_bytes()[start:])
        parsed: list[int] = []
        try:
            obj = reader.tgread_object()
            parsed = TelegramMarket._collect_gift_ids(getattr(obj, "gifts", None) or [])
        except Exception:  # noqa: BLE001
            try:
                reader.set_position(len(reader.get_bytes()))
            except Exception:  # noqa: BLE001
                pass
        return merge_ids(parsed, extract_star_gift_ids(raw))


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
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _stars_level(rating: Any) -> int | None:
    """Telegram StarsRating.level / current_level — не путать с None."""
    if rating is None:
        return None
    direct = _normalize_level(rating)
    if direct is not None:
        return direct
    for attr in ("level", "current_level"):
        got = _normalize_level(getattr(rating, attr, None))
        if got is not None:
            return got
    if isinstance(rating, dict):
        for key in ("level", "current_level"):
            got = _normalize_level(rating.get(key))
            if got is not None:
                return got
    return None


def _apply_level(lot: Lot, raw: Any) -> None:
    got = _stars_level(raw)
    if got is None:
        return
    if lot.account_level is None or got > lot.account_level:
        lot.account_level = got


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
    if rating is not None:
        _apply_level(lot, rating)
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


def count_unique_star_gifts(saved: Any) -> int:
    """Сколько unique NFT у продавца. Безлимитные дешёвые гифты не считаем."""
    unique = 0
    for item in getattr(saved, "gifts", None) or []:
        gift = getattr(item, "gift", None) or item
        name = type(gift).__name__
        if getattr(gift, "slug", None) or "Unique" in name:
            unique += 1
    return unique


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
            ids = ids_from_json_payload(data)
            if len(ids) >= config.MIN_COLLECTIONS:
                self.gift_ids = ids
                try:
                    self._hash = int(data.get("hash", 0) or 0)
                except (TypeError, ValueError, AttributeError):
                    self._hash = 0
            else:
                logger.warning(
                    "Кэш каталога слишком маленький (%s) — грузим заново",
                    len(ids),
                )
        except (OSError, ValueError, TypeError):
            pass

    def _save_catalog(self) -> None:
        if not self.catalog_file or len(self.gift_ids) < config.MIN_COLLECTIONS:
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
    def _collect_gift_ids(gifts: Any) -> list[int]:
        """Все id из каталога — и обычные, и unique, без фильтра resale."""
        ids: list[int] = []
        seen: set[int] = set()

        def add(raw: Any) -> None:
            if raw is None:
                return
            try:
                gid = int(raw)
            except (TypeError, ValueError):
                return
            if gid and gid not in seen:
                seen.add(gid)
                ids.append(gid)

        for gift in gifts or []:
            add(getattr(gift, "id", None))
            add(getattr(gift, "gift_id", None))
            inner = getattr(gift, "gift", None)
            if inner is not None and inner is not gift:
                add(getattr(inner, "id", None))
                add(getattr(inner, "gift_id", None))
        return ids

    async def load_from_bot_api(self, bot: Any) -> list[int]:
        """Каталог через Bot API — не зависит от слоя Telethon юзера."""
        if bot is None:
            return []
        try:
            result = await asyncio.wait_for(bot.get_available_gifts(), timeout=25.0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"getAvailableGifts: {exc}"
            logger.warning("Bot API каталог: %s", exc)
            return []
        gifts = getattr(result, "gifts", None) or []
        ids: list[int] = []
        seen: set[int] = set()
        for gift in gifts:
            raw = getattr(gift, "id", None)
            try:
                gid = int(raw)
            except (TypeError, ValueError):
                continue
            if gid and gid not in seen:
                seen.add(gid)
                ids.append(gid)
        if ids:
            logger.info("Каталог Bot API: %s коллекций", len(ids))
        elif gifts:
            self.last_error = f"getAvailableGifts пустой (gifts={len(gifts)})"
        else:
            self.last_error = "getAvailableGifts пустой"
        return ids

    async def _load_user_gifts(self) -> list[int]:
        last: Exception | None = None
        for attempt in range(2):
            try:
                await self._wait_flood()
                result = await asyncio.wait_for(
                    self.client(GetStarGiftsIdsRequest(hash=0)),
                    timeout=15.0,
                )
                if isinstance(result, list) and result:
                    logger.info("Коллекций маркета (user API): %s", len(result))
                    return merge_ids(result)
                if isinstance(result, StarGiftsNotModified) or (
                    result.__class__.__name__ == "StarGiftsNotModified"
                ):
                    continue
                gifts = getattr(result, "gifts", []) or []
                ids = self._collect_gift_ids(gifts)
                if ids:
                    logger.info("Коллекций маркета (user API): %s", len(ids))
                    return ids
                self.last_error = f"GetStarGifts пустой (gifts={len(gifts)})"
            except Exception as exc:  # noqa: BLE001
                last = exc
                self.last_error = f"GetStarGifts: {type(exc).__name__}: {exc}"
                logger.warning("GetStarGifts attempt %s: %s", attempt + 1, exc)
                await asyncio.sleep(1.2 * (attempt + 1))
        if last:
            logger.error("GetStarGifts: %s", last)
        return []

    def load_from_bundled(self) -> list[int]:
        if not _BUNDLED_CATALOG.exists():
            return []
        try:
            data = json.loads(_BUNDLED_CATALOG.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Встроенный каталог: %s", exc)
            return []
        ids = ids_from_json_payload(data)
        if ids:
            logger.info("Каталог из репо: %s коллекций", len(ids))
        return ids

    def _fetch_public_sync(self) -> list[int]:
        last_err = ""
        for url in _PUBLIC_CATALOG_URLS:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "gift-tracker/4.3"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                ids = ids_from_json_payload(data)
                if ids:
                    logger.info("Каталог %s: %s коллекций", url, len(ids))
                    return ids
                last_err = f"{url} без id"
            except Exception as exc:  # noqa: BLE001
                last_err = f"{url}: {exc}"
                logger.warning("Публичный каталог %s", last_err)
        if last_err:
            self.last_error = last_err
        return []

    async def load_from_public(self) -> list[int]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_public_sync), timeout=20.0
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"public catalog: {exc}"
            logger.warning("Публичный каталог: %s", exc)
            return []

    async def load_collections(
        self, force: bool = False, *, bot: Any | None = None
    ) -> list[int]:
        self._load_catalog()
        if self.gift_ids and not force and len(self.gift_ids) >= config.MIN_COLLECTIONS:
            return self.gift_ids
        try:
            await self.ensure_connected()
        except Exception as exc:  # noqa: BLE001
            logger.warning("connect перед каталогом: %s", exc)

        via_bundled = self.load_from_bundled()
        via_public: list[int] = []
        via_user: list[int] = []
        via_bot: list[int] = []

        public_task = asyncio.create_task(self.load_from_public())
        user_task = asyncio.create_task(self._load_user_gifts())
        bot_task = (
            asyncio.create_task(self.load_from_bot_api(bot))
            if bot is not None
            else None
        )
        try:
            via_public = await asyncio.wait_for(asyncio.shield(public_task), timeout=12.0)
        except Exception:  # noqa: BLE001
            public_task.cancel()
        try:
            via_user = await asyncio.wait_for(asyncio.shield(user_task), timeout=18.0)
        except Exception:  # noqa: BLE001
            user_task.cancel()
        if bot_task is not None:
            try:
                via_bot = await asyncio.wait_for(asyncio.shield(bot_task), timeout=12.0)
            except Exception:  # noqa: BLE001
                bot_task.cancel()
        if not isinstance(via_public, list):
            via_public = []
        if not isinstance(via_user, list):
            via_user = []
        if not isinstance(via_bot, list):
            via_bot = []

        merged = merge_ids(via_user, via_public, via_bundled, via_bot)
        if merged:
            self.gift_ids = merged
            self._save_catalog()
            logger.info(
                "Каталог суммарно %s · user=%s public=%s bundled=%s bot=%s",
                len(merged),
                len(via_user),
                len(via_public),
                len(via_bundled),
                len(via_bot),
            )
            if len(merged) < config.MIN_COLLECTIONS:
                self.last_error = f"мало коллекций: {len(merged)}"
            return merged
        return self.gift_ids

    def next_batch(self, n: int) -> list[int]:
        if not self.gift_ids:
            return []
        total = len(self.gift_ids)
        if n <= 0 or n >= total:
            self._cursor = 0
            ids = list(self.gift_ids)
            random.shuffle(ids)
            return ids
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
        offset: str = "",
    ) -> list[Lot]:
        stats = {"errors": 0, "floods": 0}
        result = await self._request(
            gift_id,
            limit,
            True,
            stats,
            gap,
            timeout,
            offset=offset,
            sort_by_price=sort_by_price,
        )
        if result is None:
            result = await self._request(
                gift_id,
                limit,
                False,
                stats,
                gap,
                timeout,
                offset=offset,
                sort_by_price=sort_by_price,
            )
        if result is None:
            return []
        lots = parse_result(result)
        for lot in lots:
            lot.collection_id = int(gift_id)
        return lots

    async def fetch_in_range(
        self,
        gift_id: int,
        min_stars: float,
        max_stars: float,
        *,
        timeout: float = 10.0,
        gap: float = 0.02,
        max_pages: int = 8,
    ) -> list[Lot]:
        """Лоты коллекции в диапазоне цены (сортировка по цене, пагинация)."""
        offset = ""
        collected: list[Lot] = []
        stats = {"errors": 0, "floods": 0}
        for _ in range(max(1, int(max_pages))):
            result = await self._request(
                gift_id,
                50,
                True,
                stats,
                gap,
                timeout,
                offset=offset,
                sort_by_price=True,
            )
            if result is None:
                result = await self._request(
                    gift_id,
                    50,
                    False,
                    stats,
                    gap,
                    timeout,
                    offset=offset,
                    sort_by_price=True,
                )
            if result is None:
                break
            lots = parse_result(result)
            if not lots:
                break
            for lot in lots:
                lot.collection_id = int(gift_id)
                if min_stars <= lot.stars <= max_stars:
                    collected.append(lot)
            prices = [lot.stars for lot in lots]
            ascending = prices[0] <= prices[-1]
            if ascending:
                if prices[0] > max_stars:
                    break
                if prices[-1] > max_stars:
                    break
            else:
                if prices[0] < min_stars:
                    break
                if prices[-1] < min_stars:
                    break
            next_off = str(getattr(result, "next_offset", "") or "")
            if not next_off or next_off == offset:
                break
            offset = next_off
        return collected

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
        # username всё ещё пуст — ещё одна попытка entity
        if lot.seller_id and not lot.seller:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_entity(int(lot.seller_id)), timeout=timeout
                )
                fill_user(lot, ent)
            except Exception:  # noqa: BLE001
                pass

    async def enrich_profile(self, lot: Lot, timeout: float = 5.0) -> None:
        if not lot.seller_id:
            return
        cached = self._profile_cache.get(int(lot.seller_id))
        if cached and cached.get("first_name"):
            _apply_cache(lot, cached)
            return
        try:
            await self._wait_flood()
            full = await asyncio.wait_for(
                self.client(GetFullUserRequest(lot.seller_id)),
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            if lot.first_name:
                self._profile_cache[int(lot.seller_id)] = _cache_from(lot)
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
            rating = getattr(uf, "stars_rating", None)
            if rating is not None:
                _apply_level(lot, rating)
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
        # сторис/список подарков не тянем на каждый лот — FloodWait глушит бота

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

    async def count_unique_gifts(self, lot: Lot, timeout: float = 4.0) -> None:
        """Считает только unique NFT. Дешёвые безлимитные (розы и т.п.) не входят."""
        if GetSavedStarGiftsRequest is None or lot.seller_id is None:
            return
        try:
            await self._wait_flood()
            ent = await asyncio.wait_for(
                self.client.get_input_entity(int(lot.seller_id)), timeout=timeout
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
        except Exception:  # noqa: BLE001
            return
        lot.gifts_count = count_unique_star_gifts(saved)
        # названия unique gifts — сигнал для female_score (без второго RPC)
        if not lot.gifts_text:
            titles: list[str] = []
            for item in getattr(saved, "gifts", None) or []:
                gift = getattr(item, "gift", None) or item
                title = str(
                    getattr(gift, "title", "") or getattr(gift, "slug", "") or ""
                )
                if title:
                    titles.append(title)
            if titles:
                lot.gifts_text = " ".join(titles)[:400]

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
        # username мог появиться только в FullUser.users
        if not lot.seller:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_entity(int(lot.seller_id)), timeout=timeout
                )
                fill_user(lot, ent)
            except Exception:  # noqa: BLE001
                pass
        await self.count_unique_gifts(lot, timeout=timeout)
        # Stories — доп. сигнал; FloodWait глотаем внутри
        if GetPeerStoriesRequest is not None and not lot.stories_text:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_input_entity(lot.seller_id), timeout=min(timeout, 3.0)
                )
                stories = await asyncio.wait_for(
                    self.client(GetPeerStoriesRequest(peer=ent)),
                    timeout=min(timeout, 3.0),
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
        if lot.free_dm is None:
            await self.check_free_dm(lot, timeout=timeout)
        cached = self._profile_cache.get(int(lot.seller_id))
        if cached is not None:
            cached.update(_cache_from(lot))
        else:
            self._profile_cache[int(lot.seller_id)] = _cache_from(lot)


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
        _apply_level(lot, info["account_level"])
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
