"""
Telegram Market scanner.

burst_search() — быстрый проход (пара секунд), как FreeGiftsParser.
run_check() — регулярный чек по кругу.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
)
from telethon.tl.functions.users import (
    GetFullUserRequest,
    GetRequirementsToContactRequest,
    GetUsersRequest,
)
from telethon.tl.types import (
    StarsAmount,
    StarsTonAmount,
    RequirementToContactPaidMessages,
    RequirementToContactPremium,
    UserStatusOnline,
)
from telethon.tl.types.payments import StarGiftsNotModified

logger = logging.getLogger(__name__)

_CYR_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
_ARAB_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)
_NON_RU_LANG_PREFIXES = (
    "ar",
    "fa",
    "ur",
    "ps",
    "ku",
    "he",
    "ckb",
    "az",
    "tr",
    "uz",
    "kk",
    "ky",
    "tg",
)


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
    is_premium: bool | None = None
    account_level: int | None = None
    gifts_count: int | None = None
    # None=неизвестно, True=можно писать бесплатно, False=нужны Stars/Premium
    free_dm: bool | None = None
    paid_dm_stars: int | None = None
    # None=неизвестно, True=сейчас в сети
    is_online: bool | None = None
    lang_code: str = ""
    seen_at: float = field(default_factory=time.time)
    discovered_at: float = 0.0  # когда трекер впервые увидел лот
    collection_id: int | None = None  # gift_id коллекции для запроса пола рынка
    market_floor: float | None = None  # актуальный пол коллекции (sort_by_price)
    telegram_value: float | None = None  # оценка Telegram, если есть

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
    def seller_key(self) -> str:
        if self.seller:
            return self.seller.lower().lstrip("@").strip()
        if self.seller_id is not None:
            return f"id:{int(self.seller_id)}"
        return ""

    @property
    def display(self) -> str:
        parts = [self.title]
        if self.number is not None:
            parts.append(f"#{self.number}")
        extra = " · ".join(x for x in (self.model, self.backdrop, self.symbol) if x)
        if extra:
            parts.append(f"({extra})")
        return " ".join(parts)


# --- Фильтры профиля: только девочки, без рекламы/отзывов/GiftDouble ---

_FEMALE_HINT_RE = re.compile(
    r"(девоч|девуш|girl|woman|she/her|👩|💅|💄|🎀|💖|💕|💗|🌸)",
    re.IGNORECASE,
)
_MALE_HINT_RE = re.compile(
    r"(парень|мужчин|мальчик|пацан|boy|man|he/him|👨|🧔|брат|бро\b|bro\b)",
    re.IGNORECASE,
)
_MALE_NAMES_RE = re.compile(
    r"^(?:"
    r"никита|илья|саша|женя|ваня|петя|петя|коля|вася|дима|миша|паша|фома|лука|савва|"
    r"валера|слава|вова|лёша|леша|гоша|костя|артём|артем|макс|рома|"
    r"кирилл|егор|игорь|олег|влад|данил|даниил|андрей|алексей|сергей|павел|"
    r"иван|денис|роман|виктор|стас|тимур|глеб|борис|антон|ярослав|матвей|"
    r"stepan|ivan|nikita|alex|max|dmitry|daniil|artem|roman|sergey|andrey|pavel|ilya|vlad"
    r")$",
    re.IGNORECASE,
)
_STRICT_FEMALE_NAME_RE = re.compile(
    r"(?:"
    r"ия|ья|ина|ела|ёна|юна|ита|лия|ея|"
    r"овна|евна|ична|"
    r"анна|мария|елена|ольга|наташа|катя|юля|даша|маша|"
    r"света|лена|ира|вика|настя|полина|алина|диана|вероника|"
    r"vera|maria|anna|elena|olga|kate|julia|diana"
    r")$",
    re.IGNORECASE,
)
_AD_PROFILE_RE = re.compile(
    r"("
    r"дарю\s*гифт|дарю\s*gift|дарю\s*подар|раздач|"
    r"бесплатн\s*гифт|free\s*gift|giveaway|акци[яи]\s|"
    r"пиши\s*в\s*лс|реклам|продам\s*гифт|купл[юу]\s*гифт|"
    r"взаимн|nft\s*drop|airdrop|крипт|казино|заработок|инвест|"
    r"100%\s*profit|ставки|"
    r"@\w*bot\b|"
    r"подпис\w*\s+на\s+канал|subscribe\s+to|join\s+chat|"
    r"розыгрыш|промо|скидк|referral|реферал"
    r")",
    re.IGNORECASE,
)
_GIFTDOUBLE_RE = re.compile(r"giftdouble|@giftdouble", re.IGNORECASE)
_REVIEW_RE = re.compile(
    r"("
    r"отзыв|reviews?|рейтинг|rating|"
    r"\d+\s*/\s*5|"
    r"довольн\w+\s+клиент|проверенн\w+\s+продав"
    r")",
    re.IGNORECASE,
)
_FEMALE_USER_RE = re.compile(
    r"(?:"
    r"girl|woman|lady|queen|princess|devoch|devush|miss|mrs|"
    r"ann|maria|elena|olga|kate|julia|diana|vika|nastya|polina|alina|"
    r"маша|даша|катя|юля|настя|полина|алина|вика|лена|света"
    r")",
    re.IGNORECASE,
)
_FEMALE_NAME_END_RE = re.compile(
    r"(ия|ья|ина|ела|ёна|юна|ита|лия|ея|овна|евна|ична)$"
)


def profile_text_blob(lot: Lot) -> str:
    return " ".join(
        x
        for x in (
            lot.first_name or "",
            lot.last_name or "",
            lot.about or "",
            lot.seller or "",
        )
        if x
    ).strip()


def looks_male(lot: Lot) -> bool:
    blob = profile_text_blob(lot).lower()
    if _MALE_HINT_RE.search(blob):
        return True
    fn = (lot.first_name or "").strip().lower()
    ln = (lot.last_name or "").strip().lower()
    if fn and _MALE_NAMES_RE.search(fn):
        return True
    if len(fn) >= 3:
        if fn.endswith(("ич", "он", "ил", "ём", "ем", "ур", "им")):
            if not fn.endswith(("ия", "ья")):
                return True
        if fn.endswith(("ан", "ен")) and not fn.endswith(("ина", "ена", "ана", "яна")):
            return True
    if ln.endswith(("ович", "евич", "ич")):
        return True
    seller = _normalize_handle(lot.seller or "")
    if seller and len(seller) >= 3:
        if _MALE_NAMES_RE.search(seller):
            return True
        raw_seller = (lot.seller or "").strip().lower().lstrip("@")
        for part in re.split(r"[_.\-]+", raw_seller):
            part_norm = _normalize_handle(part)
            if part_norm and _MALE_NAMES_RE.match(part_norm):
                return True
    return False


def _normalize_handle(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]", "", (text or "").lower().lstrip("@"))


def _username_looks_female(username: str) -> bool:
    """Ник @seller — часто единственный признак на маркете."""
    u = _normalize_handle(username)
    if len(u) < 3:
        return False
    if _MALE_NAMES_RE.search(u):
        return False
    if _FEMALE_USER_RE.search(u):
        return True
    if _FEMALE_NAME_END_RE.search(u):
        return True
    if u.endswith(("ka", "ya", "na", "sha", "nya", "lia", "iya")):
        return True
    return False


def looks_female(lot: Lot) -> bool:
    """Женский профиль: имя, фамилия, bio или @username."""
    if looks_male(lot):
        return False
    fn = (lot.first_name or "").strip().lower()
    ln = (lot.last_name or "").strip().lower()
    if fn and _MALE_NAMES_RE.search(fn):
        return False
    blob = profile_text_blob(lot).lower()
    if _FEMALE_HINT_RE.search(blob):
        return True
    if fn and len(fn) >= 3:
        if _STRICT_FEMALE_NAME_RE.search(fn):
            return True
        if _FEMALE_NAME_END_RE.search(fn):
            return True
    if ln and (ln.endswith("овна") or ln.endswith("евна") or ln.endswith("ична")):
        return True
    seller = (lot.seller or "").strip()
    if seller and _username_looks_female(seller):
        return True
    return False


def has_review_in_profile(lot: Lot) -> bool:
    blob = profile_text_blob(lot)
    return bool(blob and _REVIEW_RE.search(blob))


def has_giftdouble(lot: Lot) -> bool:
    return bool(_GIFTDOUBLE_RE.search(profile_text_blob(lot)))


def is_ad_profile(lot: Lot) -> bool:
    blob = profile_text_blob(lot)
    return bool(blob and _AD_PROFILE_RE.search(blob))


def female_filter_reason(lot: Lot) -> str:
    """Почему профиль не прошёл (для логов)."""
    if looks_male(lot):
        return "мужской"
    if is_ad_profile(lot):
        return "реклама"
    if has_review_in_profile(lot):
        return "отзывы"
    if has_giftdouble(lot):
        return "giftdouble"
    return ""


def is_clean_female_profile(lot: Lot) -> bool:
    """Без мужчин/рекламы. Неизвестный профиль (пустое имя, латинский ник) — ок."""
    return not female_filter_reason(lot)


class MarketPriceBook:
    """Пол рынка: дешёвые лоты коллекции (sort_by_price), не свежие дампы за 10к."""

    MAX_SAMPLES = 60
    MIN_SAMPLES = 2
    DEFAULT_MAX_RATIO = 1.55
    FLOOR_TTL = 180.0

    def __init__(self) -> None:
        self._samples: dict[str, list[float]] = {}
        self._floors: dict[str, tuple[float, float]] = {}  # key → (floor, ts)

    def _keys(self, lot: Lot) -> list[str]:
        keys: list[str] = []
        mk = (lot.model_key or "").strip()
        if mk:
            keys.append(mk)
        if lot.collection_id is not None:
            cid = f"cid:{int(lot.collection_id)}"
            if cid not in keys:
                keys.append(cid)
        title = (lot.title or "").strip().lower()
        if title and title not in keys:
            keys.append(title)
        return keys

    def ingest(self, lots: Iterable[Lot]) -> None:
        """Только дешёвая выборка (пол), не page1 по дате — иначе 10к затирает 300⭐."""
        for lot in lots:
            stars = float(lot.stars or 0)
            if stars <= 0:
                continue
            for key in self._keys(lot):
                arr = self._samples.setdefault(key, [])
                arr.append(stars)
                if len(arr) > self.MAX_SAMPLES:
                    arr.sort()
                    self._samples[key] = arr[: self.MAX_SAMPLES]

    def set_floor(self, keys: Iterable[str], floor: float) -> None:
        now = time.time()
        val = float(floor)
        if val <= 0:
            return
        for key in keys:
            k = str(key or "").strip()
            if k:
                self._floors[k] = (val, now)

    def live_floor(self, lot: Lot) -> float | None:
        now = time.time()
        for key in self._keys(lot):
            hit = self._floors.get(key)
            if hit and now - hit[1] < self.FLOOR_TTL and hit[0] > 0:
                return hit[0]
        return None

    def remember_floor(self, lot: Lot, floor: float) -> None:
        self.set_floor(self._keys(lot), floor)
        lot.market_floor = float(floor)

    def _fair_for_key(self, key: str) -> float | None:
        prices = self._samples.get(key)
        if not prices or len(prices) < self.MIN_SAMPLES:
            return None
        sorted_p = sorted(prices)
        n = max(1, len(sorted_p) // 4)
        return sum(sorted_p[:n]) / n

    def fair_price(self, lot: Lot) -> float | None:
        live = self.live_floor(lot)
        if live is not None:
            return live
        if lot.market_floor and lot.market_floor > 0:
            return float(lot.market_floor)
        if lot.telegram_value and lot.telegram_value > 0:
            return float(lot.telegram_value)
        for key in self._keys(lot):
            fair = self._fair_for_key(key)
            if fair is not None:
                return fair
        return None

    def price_cap(self, lot: Lot, *, max_ratio: float | None = None) -> float | None:
        fair = self.fair_price(lot)
        if fair is None:
            return None
        ratio = float(max_ratio or self.DEFAULT_MAX_RATIO)
        return max(fair * ratio, fair + min(400.0, fair * 0.5))

    def is_fair_price(self, lot: Lot, *, max_ratio: float | None = None) -> bool:
        cap = self.price_cap(lot, max_ratio=max_ratio)
        if cap is not None:
            return float(lot.stars) <= cap
        # Нет снимка пола — режем явные дампы: TG value или типичный пол коллекции < 40% цены
        stars = float(lot.stars or 0)
        if stars <= 0:
            return True
        ref = lot.telegram_value
        if ref and ref > 0 and stars > ref * 2.5:
            return False
        return True

    def overprice_reason(self, lot: Lot, *, max_ratio: float | None = None) -> str:
        fair = self.fair_price(lot)
        cap = self.price_cap(lot, max_ratio=max_ratio)
        if fair is None or cap is None:
            return ""
        if float(lot.stars) <= cap:
            return ""
        return f"завышено {int(lot.stars):,}⭐ > рынок ~{int(fair):,}⭐ (макс {int(cap):,})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": {
                k: [float(x) for x in v[: self.MAX_SAMPLES]]
                for k, v in self._samples.items()
                if v
            },
            "floors": {
                k: [float(floor), float(ts)]
                for k, (floor, ts) in self._floors.items()
                if floor > 0
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MarketPriceBook":
        book = cls()
        if not isinstance(data, dict):
            return book
        if "samples" in data or "floors" in data:
            samples = data.get("samples") if isinstance(data.get("samples"), dict) else {}
            floors = data.get("floors") if isinstance(data.get("floors"), dict) else {}
        else:
            samples = data
            floors = {}
        for key, raw in (samples or {}).items():
            if not isinstance(raw, list):
                continue
            prices = []
            for item in raw:
                try:
                    val = float(item)
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    prices.append(val)
            if prices:
                book._samples[str(key)] = prices[: cls.MAX_SAMPLES]
        now = time.time()
        for key, raw in (floors or {}).items():
            if not isinstance(raw, (list, tuple)) or len(raw) < 1:
                continue
            try:
                floor = float(raw[0])
                ts = float(raw[1]) if len(raw) > 1 else now
            except (TypeError, ValueError):
                continue
            if floor > 0:
                book._floors[str(key)] = (floor, ts)
        return book


def sort_lots_fresh_first(lots: list[Lot], *, live_ids: set[str] | None = None) -> list[Lot]:
    """Сначала live с маркета, внутри группы — по свежести."""
    live = live_ids or set()

    def _key(lot: Lot) -> tuple[int, float]:
        is_live = 0 if lot.id in live else 1
        ts = float(lot.discovered_at or lot.seen_at or 0)
        return (is_live, -ts)

    return sorted(lots, key=_key)


def seller_identity_keys(lot: Lot) -> set[str]:
    """Все ключи TG-аккаунта — чтобы не дублировать по нику и id."""
    keys: set[str] = set()
    if lot.seller:
        u = lot.seller.lower().lstrip("@").strip()
        if u:
            keys.add(u)
            keys.add(f"u:{u}")
    if lot.seller_id is not None:
        keys.add(f"id:{int(lot.seller_id)}")
    if not keys:
        keys.add(lot.owner_key)
    return keys


def seller_keys_overlap(lot: Lot, blocked: set[str]) -> bool:
    return bool(seller_identity_keys(lot) & blocked)


def stamp_live_lots(lots: list[Lot], *, now: float | None = None) -> None:
    """Пометить лоты как только что увиденные на маркете."""
    ts = float(now if now is not None else time.time())
    for lot in lots:
        lot.discovered_at = ts
        lot.seen_at = ts


def is_fresh_market_lot(
    lot: Lot,
    *,
    max_age_sec: float = 1200.0,
    now: float | None = None,
) -> bool:
    """Свежий лот с live-скана (не старый из накопленного пула)."""
    ts = float(now if now is not None else time.time())
    seen = float(lot.discovered_at or lot.seen_at or 0)
    if seen <= 0:
        return True
    return (ts - seen) <= max(60.0, float(max_age_sec))


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
        self._gifts_hash = 0
        self._cursor = 0
        self._flood_until = 0.0
        self._gap_lock = asyncio.Lock()
        self._last_req = 0.0
        self._owner_cache: dict[str, str] = {}
        self._profile_cache: dict[int, dict[str, Any]] = {}
        self._found_users: list[dict[str, Any]] = []
        self._progress_cb = None
        self._catalog_load: Any | None = None  # () -> (ids, hash) | None
        self._catalog_save: Any | None = None  # (ids, hash) -> None
        self._refresh_task: asyncio.Task | None = None
        self.check_no = 0
        self.last_error = ""
        self._bad_until: dict[int, float] = {}

    def mark_collection_bad(self, gift_id: int, cooldown: float = 300.0) -> None:
        self._bad_until[int(gift_id)] = time.time() + max(30.0, float(cooldown))

    def is_collection_bad(self, gift_id: int) -> bool:
        until = self._bad_until.get(int(gift_id), 0.0)
        if until and time.time() < until:
            return True
        if until:
            self._bad_until.pop(int(gift_id), None)
        return False

    def set_catalog_hooks(
        self,
        load_cb: Any | None = None,
        save_cb: Any | None = None,
    ) -> None:
        """Подключить кэш коллекций из БД — старт без ожидания GetStarGifts."""
        self._catalog_load = load_cb
        self._catalog_save = save_cb

    def set_client(self, client: TelegramClient) -> None:
        self.client = client
        # gift_ids оставляем из кэша — не сбрасываем список коллекций
        self._owner_cache.clear()
        self._profile_cache.clear()
        self._found_users.clear()
        self._progress_cb = None
        self._flood_until = 0.0
        self.check_no = 0
        self.last_error = ""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = None

    def drain_users(self) -> list[dict[str, Any]]:
        """Забрать юзеров, собранных из ответов маркета (для записи в БД)."""
        out = self._found_users
        self._found_users = []
        return out

    def _remember_users(self, users: list[dict[str, Any]] | Any) -> None:
        if not users:
            return
        if not isinstance(users, list):
            return
        self._found_users.extend(users)

    async def ensure_connected(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()

    def _hydrate_from_cache(self) -> bool:
        if self._gift_ids:
            return True
        if not callable(self._catalog_load):
            return False
        try:
            cached = self._catalog_load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog load: %s", exc)
            return False
        if not cached:
            return False
        ids, h = cached
        if not ids:
            return False
        self._gift_ids = list(ids)
        self._gifts_hash = int(h or 0)
        if self._gift_ids:
            self._cursor = random.randrange(len(self._gift_ids))
        return True

    def _persist_catalog(self) -> None:
        if not callable(self._catalog_save) or not self._gift_ids:
            return
        try:
            self._catalog_save(list(self._gift_ids), int(self._gifts_hash or 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog save: %s", exc)

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
        if not ids:
            ids = [
                int(g.id)
                for g in (gifts or [])
                if getattr(g, "id", None) is not None
            ]
        return ids

    async def _fetch_collections_remote(self, *, use_hash: bool = True) -> list[int]:
        await self.ensure_connected()
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                req_hash = 0 if attempt >= 2 else (
                    int(self._gifts_hash or 0) if use_hash else 0
                )
                result = await asyncio.wait_for(
                    self.client(GetStarGiftsRequest(hash=req_hash)),
                    timeout=20.0,
                )
                if isinstance(result, StarGiftsNotModified) or (
                    result.__class__.__name__ == "StarGiftsNotModified"
                ):
                    if self._gift_ids:
                        return self._gift_ids
                    result = await asyncio.wait_for(
                        self.client(GetStarGiftsRequest(hash=0)),
                        timeout=20.0,
                    )
                gifts = getattr(result, "gifts", []) or []
                ids = self._ids_from_gifts(gifts)
                if not ids and gifts:
                    ids = [
                        int(g.id)
                        for g in gifts
                        if getattr(g, "id", None) is not None
                    ]
                try:
                    self._gifts_hash = int(getattr(result, "hash", 0) or 0)
                except (TypeError, ValueError):
                    self._gifts_hash = 0
                if ids:
                    if set(ids) != set(self._gift_ids):
                        random.shuffle(ids)
                        self._gift_ids = ids
                        self._cursor = random.randrange(len(ids))
                    elif not self._gift_ids:
                        random.shuffle(ids)
                        self._gift_ids = ids
                        self._cursor = random.randrange(len(ids))
                    self._persist_catalog()
                logger.info(
                    "collections=%s hash=%s cursor=%s (api gifts=%s attempt=%s)",
                    len(self._gift_ids),
                    self._gifts_hash,
                    self._cursor,
                    len(gifts),
                    attempt + 1,
                )
                if self._gift_ids:
                    return self._gift_ids
                logger.warning(
                    "GetStarGifts: api вернул %s gifts, resale ids=0",
                    len(gifts),
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.last_error = str(exc)
                logger.warning(
                    "GetStarGifts attempt %s/4: %s", attempt + 1, exc
                )
                await asyncio.sleep(2.0 * (attempt + 1))
        if last_exc:
            logger.error("catalog fetch failed: %s", last_exc)
        return self._gift_ids

    def _schedule_refresh(self) -> None:
        """Фоновое обновление списка коллекций — не блокирует парсинг."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._refresh_task and not self._refresh_task.done():
            return

        async def _job() -> None:
            try:
                await self._fetch_collections_remote(use_hash=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("catalog refresh: %s", exc)

        self._refresh_task = loop.create_task(_job())

    async def load_collections(self, force: bool = False) -> list[int]:
        """Мгновенно из RAM/БД, сеть — в фоне (или force=True)."""
        if self._gift_ids and not force:
            self._schedule_refresh()
            return self._gift_ids
        if not force and self._hydrate_from_cache():
            self._schedule_refresh()
            return self._gift_ids
        return await self._fetch_collections_remote(use_hash=bool(self._gifts_hash))

    async def preload_collections(self) -> int:
        """Прогрев кэша при логине — парсер стартует без паузы."""
        ids = await self.load_collections(force=False)
        return len(ids)

    def reshuffle_collections(self) -> None:
        """Случайный порядок коллекций + курсор — чтобы выдача не повторялась."""
        if not self._gift_ids:
            return
        random.shuffle(self._gift_ids)
        self._cursor = random.randrange(len(self._gift_ids))

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
        bump_check: bool = True,
        touch_cursor: bool = True,
        time_budget: float = 3.5,
        early_show_at: int = 0,
        on_early_lots: Any | None = None,
        collection_ids: list[int] | None = None,
        stop_event: Any | None = None,
        deep: bool = False,
    ) -> CheckResult:
        """Поиск лотов. Может отдать early-пул для быстрой выдачи, потом добить 149."""
        started = time.monotonic()
        if bump_check:
            self.check_no += 1
        stats = {"ok": 0, "errors": 0, "floods": 0, "scanned": 0}

        try:
            gift_ids = await self.load_collections()
        except Exception as exc:  # noqa: BLE001
            if bump_check:
                self.last_error = str(exc)
            return CheckResult(
                check_no=self.check_no if bump_check else 0,
                scanned=0,
                lots=[],
                collections_total=0,
                errors=1,
                elapsed=time.monotonic() - started,
                error=str(exc),
            )

        saved_ids = list(self._gift_ids)
        saved_cursor = self._cursor
        if collection_ids is not None:
            ids = list(collection_ids)
            random.shuffle(ids)
        elif touch_cursor:
            self.reshuffle_collections()
            ids = list(self._gift_ids)
        else:
            ids = list(gift_ids)
            random.shuffle(ids)
        if max_collections <= 0 or max_collections >= len(ids):
            batch = ids
        else:
            batch = list(ids[:max_collections])
            random.shuffle(batch)
        sem = asyncio.Semaphore(parallel)
        lots: list[Lot] = []
        early_fired = False

        async def one(gid: int) -> list[Lot]:
            async with sem:
                return await self._fetch_one(
                    gid,
                    per_collection,
                    stats,
                    gap=gap,
                    timeout=timeout,
                    sem=None,
                    deep=deep,
                )

        progress_cb = getattr(self, "_progress_cb", None)
        for i in range(0, len(batch), parallel):
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            if 0 < time_budget < 1e8 and time.monotonic() - started > time_budget:
                break
            group = batch[i : i + parallel]
            parts = await asyncio.gather(*[one(g) for g in group], return_exceptions=True)
            for part in parts:
                stats["scanned"] += 1
                if isinstance(part, list):
                    lots.extend(part)
                else:
                    stats["errors"] += 1
            # всегда копить в БД по ходу скана (не ждать выдачи)
            batch_save = getattr(self, "_batch_save_cb", None)
            if callable(batch_save):
                try:
                    fresh = []
                    for part in parts:
                        if isinstance(part, list):
                            fresh.extend(part)
                    if fresh:
                        await batch_save(fresh)
                except Exception:  # noqa: BLE001
                    pass
            # уникальные модели/типы в сыром пуле
            titles = {
                (lot.title or lot.model or "").strip().lower()
                for lot in lots
                if (lot.title or lot.model)
            }
            models = {
                lot.model_key for lot in lots if getattr(lot, "model_key", None)
            }
            if callable(progress_cb):
                try:
                    await progress_cb(
                        stats["scanned"],
                        len(batch),
                        len(lots),
                        len(titles),
                        len(models),
                    )
                except TypeError:
                    try:
                        await progress_cb(stats["scanned"], len(batch), len(lots))
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass

            matched_now = [
                lot for lot in _dedupe(lots) if min_stars <= lot.stars <= max_stars
            ]
            # ранняя выдача — не ждём конец 149
            if (
                not early_fired
                and early_show_at > 0
                and callable(on_early_lots)
                and len(matched_now) >= early_show_at
            ):
                early_fired = True
                try:
                    await on_early_lots(list(matched_now), stats["scanned"], len(batch))
                except Exception:  # noqa: BLE001
                    pass

            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break

            # time_budget>0 — можно рано выйти полностью
            if 0 < time_budget < 1e8 and len(matched_now) >= max(
                limit_results * 3, limit_results
            ):
                break

        if not touch_cursor or collection_ids is not None:
            self._gift_ids = saved_ids
            self._cursor = saved_cursor

        unique = _dedupe(lots)
        now = time.time()
        for lot in unique:
            lot.discovered_at = now
        random.shuffle(unique)
        matched = [lot for lot in unique if min_stars <= lot.stars <= max_stars]
        random.shuffle(matched)
        pool_n = max(limit_results * 20, 300)
        matched = matched[:pool_n]

        return CheckResult(
            check_no=self.check_no if bump_check else 0,
            scanned=stats["scanned"],
            lots=matched,
            collections_total=len(gift_ids),
            ok=stats["ok"],
            errors=stats["errors"],
            floods=stats["floods"],
            elapsed=time.monotonic() - started,
            error=self.last_error if bump_check else "",
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
        # batch_size<=0 → все коллекции за один чек (149/149)
        if batch_size <= 0 or batch_size >= n:
            take = n
            batch = list(gift_ids)
            random.shuffle(batch)
            self._cursor = 0
        else:
            take = min(n, batch_size)
            batch = [gift_ids[(self._cursor + i) % n] for i in range(take)]
            self._cursor = (self._cursor + take) % n

        sem = asyncio.Semaphore(parallel)

        async def one(gid: int) -> list[Lot]:
            async with sem:
                return await self._fetch_one(
                    gid, per_collection, stats, gap=gap, timeout=timeout, sem=None
                )

        lots: list[Lot] = []
        for i in range(0, len(batch), parallel):
            group = batch[i : i + parallel]
            parts = await asyncio.gather(*[one(g) for g in group], return_exceptions=True)
            for part in parts:
                stats["scanned"] += 1
                if isinstance(part, list):
                    lots.extend(part)
                else:
                    stats["errors"] += 1

        unique = _dedupe(lots)
        random.shuffle(unique)
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
            all_lots=list(unique),
        )

    async def resolve_owners(
        self,
        lots: list[Lot],
        timeout: float = 0.9,
        *,
        parallel: int = 12,
    ) -> None:
        # только тем, кому реально нужен owner — не гоняем лишние API
        need = [lot for lot in lots if not lot.seller or lot.seller_id is None]
        if not need:
            return
        sem = asyncio.Semaphore(max(1, int(parallel)))

        async def one(lot: Lot) -> None:
            async with sem:
                await self.resolve_owner(lot, timeout=timeout)

        await asyncio.gather(*[one(lot) for lot in need])

    async def _resolve_via_full_user(self, lot: Lot, *, timeout: float) -> bool:
        """Дотянуть юзернейм/имя через GetFullUser — для скрытых профилей."""
        if not lot.seller_id:
            return False
        try:
            await self._wait_flood()
            full = await asyncio.wait_for(
                self.client(GetFullUserRequest(lot.seller_id)),
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            return False
        for u in getattr(full, "users", None) or []:
            if getattr(u, "id", None) == lot.seller_id:
                _fill_user(lot, u)
                break
        uf = getattr(full, "full_user", None)
        if uf is not None:
            about = str(getattr(uf, "about", "") or "")
            if about and not lot.about:
                lot.about = about
            rating = getattr(uf, "stars_rating", None)
            if rating is not None:
                level = _normalize_level(getattr(rating, "level", None))
                if level is not None:
                    lot.account_level = level
        return bool(lot.seller)

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
            if await self._resolve_via_full_user(lot, timeout=timeout):
                if lot.slug and lot.seller:
                    self._owner_cache[lot.slug] = lot.seller
                return
        if not lot.slug:
            return
        try:
            await self._wait_flood()
            result = await asyncio.wait_for(
                self.client(GetUniqueStarGiftRequest(slug=lot.slug)),
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            if lot.seller_id and not lot.seller:
                await self._resolve_via_full_user(lot, timeout=timeout)
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
                return
        if seller_id and not lot.seller:
            try:
                await self._wait_flood()
                ent = await asyncio.wait_for(
                    self.client.get_entity(seller_id), timeout=timeout
                )
                _fill_user(lot, ent)
                if lot.seller:
                    self._owner_cache[lot.slug] = lot.seller
                    return
            except Exception:  # noqa: BLE001
                pass
            await self._resolve_via_full_user(lot, timeout=timeout)
            if lot.seller and lot.slug:
                self._owner_cache[lot.slug] = lot.seller

    async def load_abouts(
        self, lots: list[Lot], *, timeout: float = 0.7, parallel: int = 8
    ) -> None:
        """Bio / lvl / gifts / premium для фильтров. Поиск лотов не трогает."""
        await self.enrich_profiles(lots, timeout=timeout, parallel=parallel)

    async def enrich_profiles(
        self, lots: list[Lot], *, timeout: float = 0.7, parallel: int = 8
    ) -> None:
        sem = asyncio.Semaphore(parallel)

        async def one(lot: Lot) -> None:
            if not lot.seller_id:
                return
            cached = self._profile_cache.get(lot.seller_id)
            if cached and not cached.get("_failed"):
                _apply_profile(lot, cached)
                return
            async with sem:
                try:
                    await self._wait_flood()
                    full = await asyncio.wait_for(
                        self.client(GetFullUserRequest(lot.seller_id)),
                        timeout=timeout,
                    )
                except Exception:  # noqa: BLE001
                    # не кэшируем навсегда — скрытый профиль может открыться позже
                    return
                for u in getattr(full, "users", None) or []:
                    if getattr(u, "id", None) == lot.seller_id:
                        _fill_user(lot, u)
                        break
                uf = getattr(full, "full_user", None)
                about = str(getattr(uf, "about", "") or "") if uf else ""
                level = None
                gifts = None
                paid_stars: int | None = None
                free_dm: bool | None = None
                if uf is not None:
                    raw_gifts = getattr(uf, "stargifts_count", None)
                    if raw_gifts is not None:
                        try:
                            gifts = int(raw_gifts)
                        except (TypeError, ValueError):
                            gifts = None
                    rating = getattr(uf, "stars_rating", None)
                    if rating is not None:
                        level = _normalize_level(getattr(rating, "level", None))
                    # точный free/paid — через check_free_dm; здесь только явно платные
                    if hasattr(uf, "send_paid_messages_stars"):
                        raw_paid = getattr(uf, "send_paid_messages_stars", None)
                        if raw_paid is not None:
                            try:
                                paid_stars = int(raw_paid)
                            except (TypeError, ValueError):
                                paid_stars = None
                            if paid_stars is not None and paid_stars > 0:
                                free_dm = False
                lot.about = about
                if level is not None:
                    lot.account_level = level
                if gifts is not None:
                    lot.gifts_count = gifts
                if free_dm is not None:
                    lot.free_dm = free_dm
                if paid_stars is not None:
                    lot.paid_dm_stars = paid_stars
                info = {
                    "username": lot.seller,
                    "first_name": lot.first_name,
                    "last_name": lot.last_name,
                    "about": about,
                    "is_premium": lot.is_premium,
                    "account_level": level,
                    "gifts_count": gifts,
                    "free_dm": free_dm,
                    "paid_dm_stars": paid_stars,
                }
                self._profile_cache[lot.seller_id] = info

        await asyncio.gather(*[one(lot) for lot in lots])

    async def check_free_dm(
        self, lots: list[Lot], *, timeout: float = 2.5
    ) -> None:
        """Пометить free_dm. Ошибки API → None (не режем выдачу)."""
        need = [lot for lot in lots if lot.seller_id is not None]
        if not need:
            return
        by_id: dict[int, list[Lot]] = {}
        for lot in need:
            by_id.setdefault(int(lot.seller_id), []).append(lot)

        inputs = []
        id_order: list[int] = []
        for uid in list(by_id.keys()):
            try:
                ent = await asyncio.wait_for(
                    self.client.get_input_entity(uid), timeout=timeout
                )
                inputs.append(ent)
                id_order.append(uid)
            except Exception:  # noqa: BLE001
                # неизвестно — оставляем None, в выдаче покажем
                continue

        for i in range(0, len(inputs), 40):
            chunk = inputs[i : i + 40]
            ids_chunk = id_order[i : i + 40]
            try:
                await self._wait_flood()
                result = await asyncio.wait_for(
                    self.client(GetRequirementsToContactRequest(id=chunk)),
                    timeout=timeout,
                )
            except Exception:  # noqa: BLE001
                continue
            reqs = list(result or [])
            for uid, req in zip(ids_chunk, reqs):
                free = True
                paid = None
                name = req.__class__.__name__
                if isinstance(req, RequirementToContactPaidMessages) or name == (
                    "RequirementToContactPaidMessages"
                ):
                    try:
                        paid = int(getattr(req, "stars_amount", 0) or 0)
                    except (TypeError, ValueError):
                        paid = 1
                    free = paid <= 0
                elif isinstance(req, RequirementToContactPremium) or name == (
                    "RequirementToContactPremium"
                ):
                    free = False
                for lot in by_id[uid]:
                    lot.free_dm = free
                    if paid is not None:
                        lot.paid_dm_stars = paid

    async def refresh_online(
        self, lots: list[Lot], *, timeout: float = 2.0
    ) -> None:
        """Обновить is_online по текущему User.status (кто в сети сейчас)."""
        need = [lot for lot in lots if lot.seller_id is not None]
        if not need:
            return
        by_id: dict[int, list[Lot]] = {}
        for lot in need:
            by_id.setdefault(int(lot.seller_id), []).append(lot)
        inputs = []
        id_order: list[int] = []
        for uid in list(by_id.keys()):
            try:
                ent = await asyncio.wait_for(
                    self.client.get_input_entity(uid), timeout=timeout
                )
                inputs.append(ent)
                id_order.append(uid)
            except Exception:  # noqa: BLE001
                continue
        for i in range(0, len(inputs), 50):
            chunk = inputs[i : i + 50]
            ids_chunk = id_order[i : i + 50]
            try:
                await self._wait_flood()
                users = await asyncio.wait_for(
                    self.client(GetUsersRequest(id=chunk)),
                    timeout=max(timeout, 3.0),
                )
            except Exception:  # noqa: BLE001
                continue
            by_uid = {
                int(u.id): u
                for u in (users or [])
                if getattr(u, "id", None) is not None
            }
            for uid in ids_chunk:
                u = by_uid.get(uid)
                if u is None:
                    continue
                online = _user_online_flag(u)
                for lot in by_id[uid]:
                    if online is not None:
                        lot.is_online = online
                    _fill_user(lot, u)

    async def afk_fetch_page(
        self,
        gift_id: int,
        *,
        offset: str = "",
        limit: int = 50,
        gap: float = 0.05,
        timeout: float = 8.0,
    ) -> tuple[list[Lot], list[dict[str, Any]], str, int]:
        """
        Одна страница resale для AFK-фарма.
        Returns: (lots, users, next_offset, total_count)
        """
        stats = {"ok": 0, "errors": 0, "floods": 0}
        result = await self._request(
            gift_id, limit, True, stats, gap, timeout, offset=offset
        )
        if result is None:
            result = await self._request(
                gift_id, limit, False, stats, gap, timeout, offset=offset
            )
        if result is None:
            return [], [], "", 0

        lots = _parse_result(result)
        users = _extract_users(result)
        self._remember_users(users)
        # также продавцы с лотов — чтобы юзы точно копились
        for lot in lots:
            if lot.seller_id is None:
                continue
            users.append(
                {
                    "user_id": lot.seller_id,
                    "username": lot.seller,
                    "first_name": lot.first_name,
                    "last_name": lot.last_name,
                    "is_premium": lot.is_premium,
                }
            )
        self._remember_users(
            [
                {
                    "user_id": lot.seller_id,
                    "username": lot.seller,
                    "first_name": lot.first_name,
                    "last_name": lot.last_name,
                    "is_premium": lot.is_premium,
                }
                for lot in lots
                if lot.seller_id is not None
            ]
        )
        next_offset = str(getattr(result, "next_offset", "") or "")
        try:
            total = int(getattr(result, "count", 0) or 0)
        except (TypeError, ValueError):
            total = 0
        if lots:
            stats["ok"] += 1
        return lots, users, next_offset, total

    async def fetch_cheapest(
        self,
        gift_id: int,
        *,
        limit: int = 15,
        timeout: float = 3.0,
        gap: float = 0.02,
    ) -> list[Lot]:
        """Первая страница resale, самые дешёвые — пол рынка коллекции."""
        stats: dict[str, int] = {"ok": 0, "errors": 0, "floods": 0}
        result = await self._request(
            int(gift_id),
            limit,
            True,
            stats,
            gap,
            timeout,
            max_attempts=1,
            sort_by_price=True,
        )
        if result is None:
            return []
        self._remember_users(_extract_users(result))
        lots = _parse_result(result)
        for lot in lots:
            lot.collection_id = int(gift_id)
        return lots

    async def _fetch_one(
        self,
        gift_id: int,
        limit: int,
        stats: dict[str, int],
        *,
        gap: float,
        timeout: float,
        sem: asyncio.Semaphore | None,
        deep: bool = False,
    ) -> list[Lot]:
        async def _do() -> list[Lot]:
            result = await self._request(gift_id, limit, True, stats, gap, timeout)
            lots = _parse_result(result) if result is not None else []
            if result is not None:
                self._remember_users(_extract_users(result))
            if not lots:
                result2 = await self._request(
                    gift_id, limit, False, stats, gap, timeout
                )
                if result2 is not None:
                    lots = _parse_result(result2)
                    self._remember_users(_extract_users(result2))
                    result = result2
            # deep=True (Заново) — 2-я страница для новых юзов; обычный парс — быстро
            if deep and lots and result is not None:
                next_off = str(getattr(result, "next_offset", "") or "")
                if next_off:
                    try:
                        more = await self._request(
                            gift_id,
                            limit,
                            True,
                            stats,
                            gap,
                            timeout,
                            offset=next_off,
                        )
                        if more is not None:
                            extra = _parse_result(more)
                            self._remember_users(_extract_users(more))
                            lots.extend(extra)
                    except Exception:  # noqa: BLE001
                        pass
            if lots:
                stats["ok"] += 1
                self._remember_users(
                    [
                        {
                            "user_id": lot.seller_id,
                            "username": lot.seller,
                            "first_name": lot.first_name,
                            "last_name": lot.last_name,
                            "is_premium": lot.is_premium,
                        }
                        for lot in lots
                        if lot.seller_id is not None
                    ]
                )
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
        *,
        offset: str = "",
        max_attempts: int = 2,
        sort_by_price: bool = False,
    ) -> Any | None:
        for attempt in range(max(1, int(max_attempts))):
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
                self.last_error = f"FloodWait {exc.seconds}s · торможу"
                await asyncio.sleep(min(wait_s, 120.0))
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                self.last_error = str(exc)
                await asyncio.sleep(0.2 * (attempt + 1))
        return None

    async def _wait_flood(self) -> None:
        delay = self._flood_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


def _extract_users(result: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in getattr(result, "users", None) or []:
        uid = getattr(u, "id", None)
        if uid is None:
            continue
        try:
            uid_i = int(uid)
        except (TypeError, ValueError):
            continue
        username = str(getattr(u, "username", "") or "").lstrip("@").strip()
        if not username:
            for alt in getattr(u, "usernames", None) or []:
                name = str(getattr(alt, "username", "") or "").lstrip("@").strip()
                if name and getattr(alt, "active", True):
                    username = name
                    break
        out.append(
            {
                "user_id": uid_i,
                "username": username,
                "first_name": str(getattr(u, "first_name", "") or ""),
                "last_name": str(getattr(u, "last_name", "") or ""),
            }
        )
    return out


def _user_online_flag(user: Any) -> bool | None:
    """True только если статус UserStatusOnline прямо сейчас."""
    st = getattr(user, "status", None)
    if st is None:
        return None
    if isinstance(st, UserStatusOnline):
        return True
    name = st.__class__.__name__
    if name == "UserStatusOnline":
        return True
    # любой другой известный статус = не в сети сейчас
    if name.startswith("UserStatus"):
        return False
    return None


def _normalize_level(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_russian_lot(lot: Lot) -> bool | None:
    """RU-фильтр: True/False если уверены; None — нет данных (не отбрасываем)."""
    lc = (getattr(lot, "lang_code", "") or "").lower().strip()
    if lc:
        if lc.startswith("ru"):
            return True
        if any(lc.startswith(p) for p in _NON_RU_LANG_PREFIXES):
            return False
    parts = [
        lot.seller or "",
        lot.first_name or "",
        lot.last_name or "",
        lot.about or "",
    ]
    blob = " ".join(p for p in parts if p).strip()
    if not blob:
        return None
    if _ARAB_RE.search(blob):
        return False
    for flag in ("🇸🇦", "🇦🇪", "🇪🇬", "🇮🇶", "🇶🇦", "🇰🇼", "🇧🇭", "🇴🇲", "🇾🇪", "🇵🇸"):
        if flag in blob:
            return False
    if "🇷🇺" in blob:
        return True
    if _CYR_RE.search(blob):
        return True
    # Латинский ник/имя без явных чужих сигналов — неизвестно, не режем.
    # У русских почти всегда латинский username; lang_code Telegram часто пустой.
    return None


def is_free_dm_lot(lot: Lot) -> bool:
    """Строго бесплатные ЛС."""
    return lot.free_dm is True


def format_account_level(lot: Lot) -> str:
    lvl = lot.account_level
    if lvl is None or lvl < 0:
        return "—"
    return str(lvl)


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
    if getattr(user, "premium", None) is not None:
        lot.is_premium = bool(user.premium)
    online = _user_online_flag(user)
    if online is not None:
        lot.is_online = online
    lc = str(getattr(user, "lang_code", "") or "").strip().lower()
    if lc:
        lot.lang_code = lc
    rating = getattr(user, "stars_rating", None)
    if rating is not None and lot.account_level is None:
        lot.account_level = _normalize_level(getattr(rating, "level", None))
    if hasattr(user, "send_paid_messages_stars"):
        raw = getattr(user, "send_paid_messages_stars", None)
        if raw is None:
            # флаг не выставлен на User — ещё не знаем точно (нужен UserFull/requirements)
            pass
        else:
            try:
                paid = int(raw)
            except (TypeError, ValueError):
                paid = None
            if paid is not None:
                lot.paid_dm_stars = paid
                lot.free_dm = paid <= 0


def _apply_profile(lot: Lot, info: dict[str, Any]) -> None:
    if info.get("username") and not lot.seller:
        lot.seller = str(info["username"])
    if info.get("first_name") and not lot.first_name:
        lot.first_name = str(info["first_name"])
    if info.get("last_name") and not lot.last_name:
        lot.last_name = str(info["last_name"])
    if "about" in info:
        lot.about = str(info.get("about") or "")
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
    collection_id: int | None = None
    raw_coll = getattr(gift, "gift_id", None)
    if raw_coll is not None:
        try:
            collection_id = int(raw_coll)
        except (TypeError, ValueError):
            collection_id = None
    telegram_value: float | None = None
    raw_val = getattr(gift, "value_amount", None)
    if raw_val is not None:
        try:
            telegram_value = float(raw_val)
            if telegram_value <= 0:
                telegram_value = None
        except (TypeError, ValueError):
            telegram_value = None
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
        collection_id=collection_id,
        telegram_value=telegram_value,
    )
    if seller_id and users and seller_id in users:
        _fill_user(lot, users[seller_id])
    return lot
