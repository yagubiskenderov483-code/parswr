"""Внутренний маркет Telegram: payments.getResaleStarGifts + профиль продавца."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import struct
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import config
from floors import FloorCatalog, extract_model_attr

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
    # instrumentation (v5.9) — listing_created_at только если API даст время выставления
    listing_created_at: float | None = None
    discovery_round: int | None = None
    username_source: str = ""
    model_id: int | None = None
    model_floor: float | None = None  # None = UNKNOWN; не выдумываем
    api_gender: str = ""  # male/female из API, если есть; иначе ""
    scan_page: int = 0
    scan_offset: str = ""
    scan_source: str = ""

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


def _user_api_gender(user: Any) -> str:
    """Telegram gender, если слой API его отдаёт. Иначе пусто — не выдумываем."""
    raw = getattr(user, "gender", None)
    if raw is None:
        raw = getattr(user, "sex", None)
    if raw is None:
        return ""
    if isinstance(raw, int):
        if raw == 1:
            return "male"
        if raw == 2:
            return "female"
        return ""
    s = str(raw).strip().lower()
    name = type(raw).__name__.lower()
    blob = f"{s} {name}"
    if any(x in blob for x in ("male", "man")) and "female" not in blob:
        return "male"
    if "female" in blob or "woman" in blob:
        return "female"
    if s in {"m", "1"}:
        return "male"
    if s in {"f", "2"}:
        return "female"
    return ""


def fill_user(lot: Lot, user: Any, *, username_source: str = "") -> None:
    username = _username_of(user)
    if username:
        had = bool(lot.seller)
        lot.seller = username
        if username_source and not had and not lot.username_source:
            lot.username_source = username_source
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
    g = _user_api_gender(user)
    if g:
        lot.api_gender = g


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


def extract_owner_user_id(owner: Any) -> int | None:
    """Telegram user id из PeerUser / raw int. Channel peer и выдумки — нет."""
    if owner is None or isinstance(owner, bool):
        return None
    if isinstance(owner, int):
        return owner if owner > 0 else None
    raw = getattr(owner, "user_id", None)
    if raw is None and not hasattr(owner, "channel_id"):
        raw = getattr(owner, "id", None)
    try:
        sid = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return sid if sid and sid > 0 else None


def parse_gift(gift: Any, users: dict[int, Any] | None = None) -> Lot | None:
    gift_id = getattr(gift, "id", None)
    slug = str(getattr(gift, "slug", None) or "")
    title = str(getattr(gift, "title", None) or "Gift")
    number = getattr(gift, "num", None)
    stars = _extract_stars(gift)
    if stars is None or stars <= 0:
        return None
    model = ""
    model_id: int | None = None
    for attr in getattr(gift, "attributes", None) or []:
        name, mid = extract_model_attr(attr)
        cls = attr.__class__.__name__.lower()
        if mid is not None:
            model_id = mid
            if name:
                model = name
        elif "model" in cls and name:
            model = name
    seller_id = extract_owner_user_id(getattr(gift, "owner_id", None))
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
        model_id=model_id,
    )
    if seller_id and users and seller_id in users:
        fill_user(lot, users[seller_id], username_source="resale_user")
    # StarGiftUnique не отдаёт timestamp выставления на resale — не выдумываем.
    lot.listing_created_at = None
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
        rpc_n = max(1, min(int(config.RPC_CONCURRENCY), int(config.SCAN_PARALLEL)))
        self._rpc_sem = asyncio.Semaphore(rpc_n)
        self._profile_cache: dict[int, dict[str, Any]] = {}
        self.last_error = ""
        self.diag: Any | None = None
        self._rpc_kind = "scan"
        self.last_fetch_ok = True
        self.floors = FloorCatalog(config.floor_cache_path())
        self.floors.load()
        self.scan_ids: list[int] = []
        self._model_cursors: dict[int, int] = {}
        self.last_next_offset = ""

    @contextmanager
    def rpc_kind(self, kind: str) -> Iterator[None]:
        prev = self._rpc_kind
        self._rpc_kind = kind
        try:
            yield
        finally:
            self._rpc_kind = prev

    def _note_flood(self, seconds: float) -> None:
        if self.diag is not None:
            self.diag.note_flood(self._rpc_kind, float(seconds))

    def _note_timeout(self) -> None:
        if self.diag is not None:
            self.diag.note_timeout(self._rpc_kind)

    def _note_exc(self, exc: BaseException) -> None:
        if isinstance(exc, FloodWaitError):
            self._note_flood(float(getattr(exc, "seconds", 0) or 0))
        elif isinstance(exc, asyncio.TimeoutError):
            self._note_timeout()

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

    def next_batch(self, n: int, pool: list[int] | None = None) -> list[int]:
        ids = list(self.gift_ids if pool is None else pool)
        if not ids:
            return []
        total = len(ids)
        if n <= 0 or n >= total:
            self._cursor = 0
            shuffled = list(ids)
            random.shuffle(shuffled)
            return shuffled
        take = min(max(1, n), total)
        batch = [ids[(self._cursor + i) % total] for i in range(take)]
        self._cursor = (self._cursor + take) % total
        return batch

    def next_model_chunk(
        self, gift_id: int, model_ids: list[int], chunk: int
    ) -> list[int]:
        """Ротация eligible model_id коллекции. chunk<=0 → все модели."""
        models = [int(x) for x in model_ids if int(x) > 0]
        if not models:
            return []
        n = int(chunk)
        if n <= 0 or n >= len(models):
            return list(models)
        cur = int(self._model_cursors.get(int(gift_id), 0) or 0)
        take = min(n, len(models))
        out = [models[(cur + i) % len(models)] for i in range(take)]
        self._model_cursors[int(gift_id)] = (cur + take) % len(models)
        return out

    async def fetch_page(
        self,
        gift_id: int,
        *,
        limit: int = 12,
        timeout: float = 8.0,
        gap: float = 0.02,
        sort_by_price: bool = False,
        offset: str = "",
        model_ids: list[int] | None = None,
    ) -> list[Lot]:
        stats = {"errors": 0, "floods": 0, "timeouts": 0}
        self.last_fetch_ok = True
        result = await self._request(
            gift_id,
            limit,
            True,
            stats,
            gap,
            timeout,
            offset=offset,
            sort_by_price=sort_by_price,
            model_ids=model_ids,
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
                model_ids=model_ids,
            )
        if result is None:
            self.last_fetch_ok = False
            self.last_next_offset = ""
            return []
        self.last_next_offset = str(getattr(result, "next_offset", "") or "")
        lots = parse_result(result)
        for lot in lots:
            lot.collection_id = int(gift_id)
            if lot.model_id is not None:
                lot.model_floor = self.floors.get_floor(int(gift_id), int(lot.model_id))
        return lots

    async def fetch_newest_until_known(
        self,
        gift_id: int,
        *,
        model_ids: list[int] | None,
        known_ids: set[str],
        seen: dict[str, float] | set[str],
        max_pages: int = 2,
        limit: int = 12,
        timeout: float = 8.0,
        gap: float = 0.02,
        stop_at_first_known: bool = False,
    ) -> tuple[list[Lot], dict[str, Any]]:
        """Newest pages с model filter.

        Live (stop_at_first_known): newest-first — после первого известного лота
        дальше только старый рынок, следующую страницу не берём.
        Seed: пагинируем вглубь, чтобы старые id не всплыли позже как NEW.
        """
        offset = ""
        collected: list[Lot] = []
        seen_ids: set[str] = set()
        new_n = 0
        old_n = 0
        pages_n = 0
        depths: dict[str, int] = {}
        offsets: list[str] = []
        cap = max(1, int(max_pages))
        page_lim = max(1, int(limit))
        mid_blob = ",".join(str(int(x)) for x in (model_ids or []) if int(x) > 0)
        hit_known = False

        def _old(lot: Lot) -> bool:
            # new_listing_seen = не в known_ids (observed ∪ snapshot) и не в pipeline seen.
            # Это НЕ «впервые на маркете»: known_ids должен быть глобальным observed.
            if lot.id in known_ids or lot.id in seen:
                return True
            if lot.slug and lot.slug in seen:
                return True
            return False

        for _page in range(cap):
            page_offset = offset
            lots = await self.fetch_page(
                gift_id,
                limit=page_lim,
                timeout=timeout,
                gap=gap,
                sort_by_price=False,
                offset=offset,
                model_ids=model_ids,
            )
            pages_n += 1
            offsets.append(page_offset)
            if not lots:
                break
            unknown = 0
            for i, lot in enumerate(lots):
                if lot.id in seen_ids:
                    continue
                seen_ids.add(lot.id)
                depths[lot.id] = (_page * page_lim) + i
                lot.scan_page = _page + 1
                lot.scan_offset = page_offset
                lot.scan_source = (
                    f"scan:collection={gift_id}:models={mid_blob}"
                    f":page={_page + 1}:offset={page_offset or '0'}"
                )
                if _old(lot):
                    old_n += 1
                    hit_known = True
                else:
                    new_n += 1
                    unknown += 1
                collected.append(lot)
            if unknown == 0:
                break
            if stop_at_first_known and hit_known:
                break
            nxt = str(self.last_next_offset or "")
            if not nxt or nxt == offset:
                break
            offset = nxt
        return collected, {
            "pages": pages_n,
            "new": new_n,
            "old": old_n,
            "depths": depths,
            "models": len(model_ids or []),
            "model_ids": list(model_ids or []),
            "offsets": offsets,
            "hit_known": hit_known,
        }

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

    def rebuild_scan_ids(self) -> list[int]:
        self.scan_ids = self.floors.scan_collection_ids(self.gift_ids)
        self._cursor = 0
        return self.scan_ids

    async def _refresh_one_collection(self, gift_id: int) -> None:
        """Price-sorted pages until listing > MAX_MODEL_FLOOR или конец/cap.

        Первая увиденная цена модели = floor. Не выдумываем UNKNOWN.
        """
        offset = ""
        stats = {"errors": 0, "floods": 0, "timeouts": 0}
        max_pages = max(1, int(config.FLOOR_REFRESH_MAX_PAGES))
        page_size = max(1, min(50, int(config.FLOOR_REFRESH_PAGE_SIZE)))
        cap = float(config.MAX_MODEL_FLOOR)
        for _ in range(max_pages):
            result = await self._request(
                gift_id,
                page_size,
                True,
                stats,
                config.REQUEST_GAP,
                config.REQUEST_TIMEOUT,
                offset=offset,
                sort_by_price=True,
            )
            if result is None:
                result = await self._request(
                    gift_id,
                    page_size,
                    False,
                    stats,
                    config.REQUEST_GAP,
                    config.REQUEST_TIMEOUT,
                    offset=offset,
                    sort_by_price=True,
                )
            if result is None:
                break
            lots = parse_result(result)
            self.floors.ingest_result(int(gift_id), result, lots)
            if not lots:
                break
            prices = [float(lot.stars) for lot in lots]
            # sort_by_price=True — дешёвые первые. Дальше MAX — дороже не нужны.
            if min(prices) > cap or max(prices) > cap:
                break
            next_off = str(getattr(result, "next_offset", "") or "")
            if not next_off or next_off == offset:
                break
            offset = next_off

    async def refresh_model_floors(
        self, gift_ids: list[int] | None = None, *, force: bool = False
    ) -> dict[str, int]:
        ids = list(gift_ids if gift_ids is not None else self.gift_ids)
        if not force and self.floors.is_fresh() and self.floors.models:
            self.rebuild_scan_ids()
            return self.floors.stats()
        logger.info(
            "Floor catalog refresh · collections=%s ttl=%ss pages≤%s",
            len(ids),
            int(config.FLOOR_CACHE_TTL),
            int(config.FLOOR_REFRESH_MAX_PAGES),
        )
        sem = asyncio.Semaphore(max(1, int(config.RPC_CONCURRENCY)))

        async def one(gid: int) -> None:
            async with sem:
                try:
                    with self.rpc_kind("scan"):
                        await self._refresh_one_collection(int(gid))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("floor refresh gid=%s: %s", gid, exc)

        chunk = max(int(config.RPC_CONCURRENCY) * 4, 8)
        for i in range(0, len(ids), chunk):
            part = ids[i : i + chunk]
            await asyncio.gather(*[one(g) for g in part], return_exceptions=True)
        self.floors.updated_at = time.time()
        self.floors.save()
        self.rebuild_scan_ids()
        st = self.floors.stats()
        logger.info(
            "Floor catalog · models=%s known=%s unknown=%s eligible=%s collections=%s",
            st.get("models_total", 0),
            st.get("model_floor_known", 0),
            st.get("model_floor_unknown", 0),
            st.get("eligible_model_count", 0),
            len(self.scan_ids),
        )
        return st

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
        model_ids: list[int] | None = None,
    ) -> Any | None:
        attr_objs = None
        if model_ids:
            from telethon.tl.types import StarGiftAttributeIdModel

            attr_objs = []
            seen: set[int] = set()
            for raw in model_ids:
                try:
                    mid = int(raw)
                except (TypeError, ValueError):
                    continue
                if mid > 0 and mid not in seen:
                    seen.add(mid)
                    attr_objs.append(StarGiftAttributeIdModel(document_id=mid))
            if not attr_objs:
                attr_objs = None
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
                                attributes=attr_objs,
                            )
                        ),
                        timeout=timeout,
                    )
            except FloodWaitError as exc:
                stats["floods"] += 1
                wait_s = float(exc.seconds) + 1.5
                self._flood_until = time.monotonic() + min(wait_s, 300.0)
                self.last_error = f"FloodWait {exc.seconds}s"
                self._note_flood(float(exc.seconds))
                await asyncio.sleep(min(wait_s, 120.0))
            except asyncio.TimeoutError:
                stats["errors"] += 1
                stats["timeouts"] = stats.get("timeouts", 0) + 1
                self.last_error = f"таймаут {timeout:g}s"
                self._note_timeout()
                await asyncio.sleep(0.2 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.2 * (attempt + 1))
        return None

    async def _try_get_entity_user(self, lot: Lot, timeout: float) -> None:
        """get_entity: пустой username — не успех, цепочку не обрываем."""
        if not lot.seller_id:
            return
        try:
            await self._wait_flood()
            ent = await asyncio.wait_for(
                self.client.get_entity(int(lot.seller_id)), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            self._note_exc(exc)
            return
        fill_user(lot, ent, username_source="get_entity")

    async def _try_unique_star_gift(self, lot: Lot, timeout: float) -> None:
        if not lot.slug:
            return
        try:
            await self._wait_flood()
            result = await asyncio.wait_for(
                self.client(GetUniqueStarGiftRequest(slug=lot.slug)),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._note_exc(exc)
            return
        gift = getattr(result, "gift", None)
        users = {
            int(u.id): u
            for u in (getattr(result, "users", None) or [])
            if getattr(u, "id", None) is not None
        }
        seller_id = extract_owner_user_id(getattr(gift, "owner_id", None) if gift else None)
        if seller_id:
            lot.seller_id = seller_id
            if seller_id in users:
                fill_user(lot, users[seller_id], username_source="unique_gift")

    async def resolve_owner(self, lot: Lot, timeout: float = 4.0) -> None:
        if lot.seller and lot.seller_id is not None:
            return
        if lot.seller_id and not lot.seller:
            await self._try_get_entity_user(lot, timeout)
            if lot.seller:
                return
        if not lot.seller or lot.seller_id is None:
            await self._try_unique_star_gift(lot, timeout)
        if lot.seller_id and not lot.seller:
            await self._try_get_entity_user(lot, timeout)

    async def enrich_profile(self, lot: Lot, timeout: float = 5.0) -> None:
        if not lot.seller_id:
            return
        cached = self._profile_cache.get(int(lot.seller_id))
        if cached:
            _apply_cache(lot, cached)
            if lot.seller and lot.first_name:
                return
        try:
            await self._wait_flood()
            inp = await asyncio.wait_for(
                self.client.get_input_entity(int(lot.seller_id)), timeout=timeout
            )
            full = await asyncio.wait_for(
                self.client(GetFullUserRequest(inp)),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._note_exc(exc)
            if lot.first_name:
                self._profile_cache[int(lot.seller_id)] = _cache_from(lot)
            return
        for u in getattr(full, "users", None) or []:
            if getattr(u, "id", None) == lot.seller_id:
                fill_user(lot, u, username_source="full_user")
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
            except Exception as exc:  # noqa: BLE001
                self._note_exc(exc)
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
            except Exception as exc:  # noqa: BLE001
                self._note_exc(exc)
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
        except Exception as exc:  # noqa: BLE001
            self._note_exc(exc)
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
        except Exception as exc:  # noqa: BLE001
            self._note_exc(exc)
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
        with self.rpc_kind("enrich"):
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
                    fill_user(lot, ent, username_source="get_entity")
                except Exception as exc:  # noqa: BLE001
                    self._note_exc(exc)
            await self.count_unique_gifts(lot, timeout=timeout)
            # Stories — доп. сигнал; FloodWait глотаем внутри
            if GetPeerStoriesRequest is not None and not lot.stories_text:
                try:
                    await self._wait_flood()
                    ent = await asyncio.wait_for(
                        self.client.get_input_entity(lot.seller_id),
                        timeout=min(timeout, 3.0),
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
                except Exception as exc:  # noqa: BLE001
                    self._note_exc(exc)
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
