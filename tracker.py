"""
Гифт-трекер внутреннего маркета Telegram.

Ловит только что выставленные на перепродажу NFT-подарки (за Stars),
фильтрует по цене 5–15 TON (дешёвые лоты) и постит карточки в канал.

Запуск:  python3 tracker.py
Настройки берутся из .env (см. .env.example) или переменных окружения.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    UserAlreadyParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)
from telethon.utils import get_peer_id

import market as market_mod
from market import (
    Lot,
    TelegramMarket,
    format_account_level,
    is_free_dm_lot,
    is_russian_lot,
)
from tracker_bot import ChannelStore, ControlBot, channel_file_path
from girl_names import GIRL_NAMES

logger = logging.getLogger("tracker")

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_BOT_TOKEN = "8807847926:AAE-lxYXhBkuhSfJRQ3WcUFgwrf7P298je4"
# Постоянная привязка — tracker market, не переопределяется
DEFAULT_CHANNEL_ID = -1004384888475
FIXED_CHANNEL_ID = -1004384888475
DEFAULT_TARGET_CHANNEL = ""
CHANNEL_NAME_HINTS = ("tracker market", "tracker", "market")

# Дешёвые подарки: 5–15 TON. Stars считаются через TON_RATE.
DEFAULT_TON_RATE = 0.0102
DEFAULT_MIN_TON = 5.0
DEFAULT_MAX_TON = 15.0


def stars_from_ton(ton: float, rate: float = DEFAULT_TON_RATE) -> float:
    """Stars за N TON при курсе TON за 1 Star."""
    r = float(rate) if rate else DEFAULT_TON_RATE
    if r <= 0:
        r = DEFAULT_TON_RATE
    return float(ton) / r


def star_window_from_ton(min_ton: float, max_ton: float, rate: float) -> tuple[float, float]:
    """Целые ⭐ на краях TON-окна: 5 TON → 490⭐, 15 TON → 1471⭐."""
    lo = math.floor(stars_from_ton(min_ton, rate))
    hi = math.ceil(stars_from_ton(max_ton, rate))
    return float(lo), float(hi)


DEFAULT_MIN_STARS, DEFAULT_MAX_STARS = star_window_from_ton(
    DEFAULT_MIN_TON, DEFAULT_MAX_TON, DEFAULT_TON_RATE
)


def lot_ton(stars: float, rate: float = DEFAULT_TON_RATE) -> float:
    r = float(rate) if rate else DEFAULT_TON_RATE
    return float(stars) * r


def in_cheap_ton_window(stars: float, cfg: "Config") -> bool:
    """Только лоты 5–15 TON (или MIN_TON..MAX_TON из конфига)."""
    price = float(stars)
    if not (cfg.min_stars <= price <= cfg.max_stars):
        return False
    ton = lot_ton(price, cfg.ton_rate)
    slack = max(float(cfg.ton_rate) or DEFAULT_TON_RATE, 0.01)
    return (cfg.min_ton - slack) <= ton <= (cfg.max_ton + slack)


def data_dir() -> Path:
    """Bothost хранит данные в /app/data; локально — рядом со скриптом."""
    bothost = Path("/app/data")
    if bothost.is_dir():
        return bothost
    return BASE_DIR


def acquire_singleton_lock() -> Any:
    """Один процесс на volume — второй инстанс рвёт auth key и вылетает сессия."""
    import fcntl

    path = data_dir() / "tracker_singleton.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SystemExit(
            "Уже запущен другой трекер (tracker_singleton.lock). "
            "На Bothost должен быть ОДИН инстанс — иначе Telegram кикает сессию."
        ) from exc
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def _catalog_file_path() -> Path:
    return data_dir() / "tracker_catalog.json"


def _load_catalog_file() -> tuple[list[int], int] | None:
    path = _catalog_file_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        ids = [int(x) for x in data.get("gift_ids", []) if str(x).isdigit()]
        if not ids:
            return None
        return ids, int(data.get("hash", 0) or 0)
    except (OSError, ValueError, TypeError):
        return None


def _save_catalog_file(gift_ids: list[int], hash_val: int = 0) -> None:
    if not gift_ids:
        return
    path = _catalog_file_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"gift_ids": gift_ids, "hash": int(hash_val or 0)}),
        encoding="utf-8",
    )
    tmp.replace(path)


def _setup_catalog_hooks(m: TelegramMarket) -> None:
    """Только tracker_catalog.json — в gifts.db юзов не пишем."""

    def _load() -> tuple[list[int], int] | None:
        return _load_catalog_file()

    def _save(ids: list[int], h: int) -> None:
        _save_catalog_file(ids, h)

    m.set_catalog_hooks(load_cb=_load, save_cb=_save)


async def wait_for_gift_ids(m: TelegramMarket) -> list[int]:
    """Не падаем при collections=0 — ждём сеть/сессию, крутим кэш."""
    for attempt in range(1, 9999):
        try:
            if attempt == 1:
                ids = await m.load_collections(force=False)
            else:
                ids = await m.load_collections(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("load_collections: %s", exc)
            ids = []
        if ids:
            return ids
        logger.error(
            "Коллекций 0 (попытка %s) — жду 30с. "
            "Проверь: /start в @markskskdbot, сессия жива, аккаунт не в бане",
            attempt,
        )
        await asyncio.sleep(30)
    return []


def _load_dotenv() -> None:
    """Мини-загрузчик .env: переменные окружения имеют приоритет."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


@dataclass(slots=True)
class Config:
    api_id: int
    api_hash: str
    session_string: str
    bot_token: str
    target_channel: str
    min_ton: float = DEFAULT_MIN_TON
    max_ton: float = DEFAULT_MAX_TON  # дороже 15 TON не постим
    min_stars: float = DEFAULT_MIN_STARS  # ~490⭐ ≈ 5 TON
    max_stars: float = DEFAULT_MAX_STARS  # ~1471⭐ ≈ 15 TON
    poll_interval: float = 0.4  # турбо-скан
    page_limit: int = 2  # API: только верх resale
    parallel: int = 6
    gap: float = 0.08
    timeout: float = 4.0
    enrich_cap: int = 60  # legacy; сканер больше не ждёт enrich
    enrich_parallel: int = 4
    scan_pages: int = 1
    scan_batch: int = 36  # legacy; вотчеры не используют кольцо
    hot_limit: int = 1  # только #1 на resale = только что выставили
    watchers: int = 40  # задач после логина, каждая сидит на своих NFT
    watch_parallel: int = 16  # одновременных GetResale
    max_account_level: int = 0  # lvl 0 или минус; lvl 1+ = уже не лох
    loh_mode: bool = False  # без GetFullUser / без копилки юзов
    skip_enrich: bool = False  # GetFullUser только для лота, в БД не пишем
    persist_sellers: bool = False
    max_gifts_count: int = 1
    persona_mode: bool = True  # только девушки по имени из листинга
    women_only: bool = True
    female_mix_target: float = 1.0
    fast_scan: bool = True
    turbo_scan: bool = True
    post_interval: float = 0.3
    ton_rate: float = DEFAULT_TON_RATE  # TON за 1 Star (для строки "X Stars / Y TON")
    tz_offset: float = 3.0  # часовой пояс для времени в карточке (МСК = 3)
    session_file: str = ""
    state_file: str = ""
    post_on_first_run: bool = False
    channel_id: int | None = None
    strict_ru: bool = True
    strict_free: bool = False  # False = скип только платных; True = только free_dm=True

    @classmethod
    def from_env(cls) -> "Config":
        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "") or default)
            except ValueError:
                return default

        api_id = int(os.environ.get("API_ID", "0") or 0)
        api_hash = os.environ.get("API_HASH", "").strip()
        if not api_id or not api_hash:
            try:
                import credentials as creds

                api_id = api_id or int(getattr(creds, "API_ID", 0) or 0)
                api_hash = api_hash or str(getattr(creds, "API_HASH", "") or "")
            except ImportError:
                pass
        if not api_id or not api_hash:
            raise SystemExit("API_ID/API_HASH не заданы — заполни .env или credentials.py")

        dd = data_dir()
        session_file = os.environ.get(
            "SESSION_FILE", str(dd / "tracker_session.txt")
        ).strip()
        state_file = os.environ.get(
            "STATE_FILE", str(dd / "tracker_state.json")
        ).strip()
        bot_token = os.environ.get("BOT_TOKEN", "").strip() or DEFAULT_BOT_TOKEN
        target = (
            os.environ.get("TARGET_CHANNEL", "").strip() or DEFAULT_TARGET_CHANNEL
        )
        ton_rate = _f("TON_RATE", DEFAULT_TON_RATE) or DEFAULT_TON_RATE
        min_ton = _f("MIN_TON", DEFAULT_MIN_TON)
        max_ton = _f("MAX_TON", DEFAULT_MAX_TON)
        if min_ton > max_ton:
            min_ton, max_ton = max_ton, min_ton
        derived_min, derived_max = star_window_from_ton(min_ton, max_ton, ton_rate)
        # MIN_STARS/MAX_STARS из старого .env принимаем, но режем по TON-окну:
        # leftover MAX_STARS=2000 (~20 TON) не должен пускать дорогие подарки.
        min_stars = _f("MIN_STARS", derived_min)
        max_stars = _f("MAX_STARS", derived_max)
        min_stars = max(min_stars, derived_min)
        max_stars = min(max_stars, derived_max)
        if min_stars > max_stars:
            min_stars, max_stars = derived_min, derived_max
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_string=os.environ.get("SESSION_STRING", "").strip(),
            bot_token=bot_token,
            target_channel=target,
            min_ton=min_ton,
            max_ton=max_ton,
            min_stars=min_stars,
            max_stars=max_stars,
            poll_interval=_f("POLL_INTERVAL", 0.12),
            page_limit=int(_f("PAGE_LIMIT", 2)),
            parallel=min(6, int(_f("PARALLEL", 6))),
            gap=_f("REQUEST_GAP", 0.02),
            timeout=_f("REQUEST_TIMEOUT", 2.5),
            enrich_cap=max(10, int(_f("ENRICH_CAP", 60))),
            enrich_parallel=max(2, min(4, int(_f("ENRICH_PARALLEL", 4)))),
            scan_pages=max(1, int(_f("SCAN_PAGES", 1))),
            scan_batch=int(_f("SCAN_BATCH", 36)),
            hot_limit=max(1, int(_f("HOT_LIMIT", 1))),
            watchers=max(1, min(60, int(_f("WATCHERS", 40)))),
            watch_parallel=max(2, min(20, int(_f("WATCH_PARALLEL", 16)))),
            max_account_level=int(_f("MAX_ACCOUNT_LEVEL", 0)),
            loh_mode=os.environ.get("TRACKER_LOH_MODE", "0") == "1",
            skip_enrich=os.environ.get("TRACKER_SKIP_ENRICH", "0") == "1",
            persist_sellers=os.environ.get("TRACKER_PERSIST_SELLERS", "0") == "1",
            max_gifts_count=max(0, int(_f("MAX_GIFTS_COUNT", 1))),
            persona_mode=os.environ.get("TRACKER_PERSONA_MODE", "1") == "1",
            women_only=os.environ.get("TRACKER_WOMEN_ONLY", "1") == "1",
            female_mix_target=min(1.0, max(0.0, _f("FEMALE_MIX_TARGET", 1.0))),
            fast_scan=os.environ.get("TRACKER_FAST_SCAN", "1") == "1",
            turbo_scan=os.environ.get("TRACKER_TURBO_SCAN", "1") == "1",
            post_interval=_f("POST_INTERVAL", 0.3),
            ton_rate=ton_rate,
            tz_offset=_f("TZ_OFFSET", 3.0),
            session_file=session_file,
            state_file=state_file,
            post_on_first_run=os.environ.get("POST_ON_FIRST_RUN", "0") == "1",
            channel_id=FIXED_CHANNEL_ID,
            strict_ru=os.environ.get("TRACKER_STRICT_RU", "1") == "1",
            strict_free=os.environ.get("TRACKER_STRICT_FREE", "0") == "1",
        )


# ---------------------------------------------------------------- state

SEEN_TTL = 7 * 24 * 3600  # помним лот неделю — дальше номер уже не «новый»
SELLER_TTL = 90 * 24 * 3600  # одного продавца не постим повторно 90 дней
HEAD_EMPTY = "__empty__"  # коллекция без лотов на resale


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("seen", {})
            data.setdefault("seen_sellers", {})
            data.setdefault("channel_id", None)
            data.setdefault("collection_heads", {})
            return data
    except (OSError, ValueError):
        pass
    return {"seen": {}, "seen_sellers": {}, "channel_id": None, "collection_heads": {}}


def save_state(path: Path, state: dict) -> None:
    now = time.time()
    seen = state.get("seen", {})
    if len(seen) > 200_000:
        state["seen"] = {
            k: v for k, v in seen.items() if now - float(v) < SEEN_TTL
        }
    sellers = state.get("seen_sellers", {})
    if len(sellers) > 100_000:
        state["seen_sellers"] = {
            k: v for k, v in sellers.items() if now - float(v) < SELLER_TTL
        }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- format

_esc = html.escape


def lot_slug(lot: Lot) -> str:
    if lot.slug:
        return lot.slug
    if lot.number is not None:
        clean = "".join(ch for ch in lot.title if ch.isalnum())
        return f"{clean}-{lot.number}"
    return lot.title


def format_lot(lot: Lot, cfg: Config, ts: float | None = None) -> str:
    stars = int(lot.stars) if float(lot.stars).is_integer() else lot.stars
    ton = lot.stars * cfg.ton_rate
    tz = timezone(timedelta(hours=cfg.tz_offset))
    when = datetime.fromtimestamp(ts or time.time(), tz=tz).strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    if lot.seller:
        seller = f"@{lot.seller}"
        if lot.seller_id:
            seller += f" (<code>{lot.seller_id}</code>)"
    elif lot.seller_id:
        seller = f"<code>{lot.seller_id}</code>"
    else:
        seller = "скрыт"

    if lot.free_dm is True:
        dm = "бесплатно"
    elif lot.free_dm is False:
        dm = f"платно ({lot.paid_dm_stars} ⭐)" if lot.paid_dm_stars else "платно"
    else:
        dm = "—"

    if lot.is_premium is True:
        status = "Premium"
    elif lot.is_premium is False:
        status = "без Premium"
    else:
        status = "—"

    return "\n".join(
        [
            "🎉 <b>НОВЫЙ ЛИСТИНГ</b>",
            "",
            f"🎁 Гифт: <b>{_esc(lot.title)}</b>",
            f"💲 Цена: <b>{stars} Stars / {ton:.2f} TON</b>",
            f"🏷 Модель: <b>{_esc(lot.model) or '—'}</b>",
            f"👤 Продавец: {seller}",
            f"📶 Level: {format_account_level(lot)}",
            f"📢 Сообщения: {dm}",
            f"🕺 Статус: {status}",
            f'🔗 <a href="{lot.nft_url}">{_esc(lot_slug(lot))}</a>',
            f"🕒 {when}",
        ]
    )


# ---------------------------------------------------------------- sending


class PostRateLimiter:
    """Один пост на весь Bothost: блокирующий flock + общий timestamp-файл."""

    def __init__(
        self, interval: float, lock_path: Path, timestamp_path: Path
    ) -> None:
        self._interval = max(0.15, float(interval))
        self._lock_path = lock_path
        self._ts_path = timestamp_path
        self._async_lock = asyncio.Lock()
        self._lock_handle: Any | None = None

    def _read_last_post(self) -> float:
        try:
            raw = self._ts_path.read_text(encoding="utf-8").strip()
            if raw:
                return float(raw)
        except (OSError, ValueError):
            pass
        return 0.0

    def _write_last_post(self, ts: float) -> None:
        self._ts_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._ts_path.with_suffix(".tmp")
        tmp.write_text(f"{ts:.6f}", encoding="utf-8")
        tmp.replace(self._ts_path)

    def _acquire_and_wait(self) -> None:
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # ждём, не скипаем
        last = self._read_last_post()
        if last > 0:
            wait = self._interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        self._lock_handle = handle

    def _release_after_send(self) -> None:
        import fcntl

        try:
            self._write_last_post(time.time())
        finally:
            if self._lock_handle is not None:
                try:
                    fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                    self._lock_handle.close()
                except OSError:
                    pass
                self._lock_handle = None

    async def gated(self, action: Any) -> None:
        """Ждёт слот (между процессами), выполняет action, фиксирует время."""
        async with self._async_lock:
            await asyncio.to_thread(self._acquire_and_wait)
            try:
                await action()
            finally:
                await asyncio.to_thread(self._release_after_send)


class Sender:
    """Шлёт карточки: через бота (с кнопками) или от юзер-сессии."""

    def __init__(
        self,
        cfg: Config,
        client: TelegramClient,
        *,
        rate_limiter: PostRateLimiter | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.chat_id: int | None = None
        self._bot = None
        self._rate_limiter = rate_limiter
        if cfg.bot_token:
            from aiogram import Bot

            self._bot = Bot(token=cfg.bot_token)

    def _keyboard(self, lot: Lot):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = [[InlineKeyboardButton(text="✓ Занять", url=lot.nft_url)]]
        second = [InlineKeyboardButton(text="🎁 Открыть лот", url=lot.nft_url)]
        if lot.seller:
            second.append(
                InlineKeyboardButton(
                    text="👤 Продавец", url=f"https://t.me/{lot.seller}"
                )
            )
        rows.append(second)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _do_send(self, lot: Lot, text: str) -> None:
        if self._bot is not None:
            from aiogram.types import LinkPreviewOptions

            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=self._keyboard(lot),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await self.client.send_message(
                self.chat_id, text, parse_mode="html", link_preview=False
            )

    async def send(self, lot: Lot) -> None:
        text = format_lot(lot, self.cfg)

        async def _send() -> None:
            await self._do_send(lot, text)

        if self._rate_limiter is not None:
            await self._rate_limiter.gated(_send)
        else:
            await _send()

    async def close(self) -> None:
        if self._bot is not None:
            await self._bot.session.close()


async def find_channel_in_dialogs(client: TelegramClient) -> int | None:
    """Канал, где юзербот уже состоит (инвайт мог протухнуть)."""
    channels: list[tuple[str, int]] = []
    try:
        async for dlg in client.iter_dialogs(limit=80):
            if not getattr(dlg, "is_channel", False):
                continue
            name = (dlg.name or "").strip()
            if not name:
                continue
            cid = get_peer_id(dlg.entity)
            channels.append((name, cid))
            low = name.lower()
            for hint in CHANNEL_NAME_HINTS:
                if hint in low:
                    logger.info("Канал из диалогов: «%s» → %s", name, cid)
                    return cid
        if channels:
            preview = ", ".join(f"«{n}»({c})" for n, c in channels[:12])
            logger.info("Каналы Mary в диалогах: %s", preview)
        if len(channels) == 1:
            name, cid = channels[0]
            logger.info("Единственный канал в диалогах: «%s» → %s", name, cid)
            return cid
    except Exception as exc:  # noqa: BLE001
        logger.warning("поиск канала в диалогах: %s", exc)
    return None


def pin_fixed_channel(
    store: Any,
    state: dict,
    state_path: Path,
    sender: Any | None = None,
) -> int:
    """Всегда канал tracker market — не зависит от env/файла/setchannel."""
    cid = int(FIXED_CHANNEL_ID)
    store.save(cid)
    state["channel_id"] = cid
    save_state(state_path, state)
    if sender is not None:
        sender.chat_id = cid
    logger.info("Канал привязан навсегда: %s", cid)
    return cid


async def obtain_channel_id(
    client: TelegramClient,
    cfg: Config,
    state: dict,
    state_path: Path,
    store: Any,
) -> int:
    """Всегда FIXED_CHANNEL_ID (-1004384888475)."""
    return pin_fixed_channel(store, state, state_path)


async def resolve_channel(client: TelegramClient, raw: str) -> int:
    """@username / -100id / t.me/name / инвайт t.me/+hash -> numeric chat id."""
    raw = raw.strip()
    if not raw:
        found = await find_channel_in_dialogs(client)
        if found is not None:
            return found
        raise SystemExit("Канал не задан — /setchannel в @markskskdbot")
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)

    async def _call(req: Any, label: str) -> Any:
        for attempt in range(4):
            try:
                return await client(req)
            except FloodWaitError as exc:
                wait = min(int(exc.seconds) + 1, 300)
                logger.warning(
                    "FloodWait %ss на %s (попытка %s/4) — жду",
                    wait,
                    label,
                    attempt + 1,
                )
                await asyncio.sleep(wait)
            except InviteHashExpiredError:
                raise
        raise SystemExit(
            f"FloodWait на {label}: Telegram просит подождать. "
            "Задай CHANNEL_ID=-100… в env Bothost."
        )

    m_invite = re.search(
        r"(?:t\.me/\+|t\.me/joinchat/|^\+)([A-Za-z0-9_-]+)", raw
    )
    if m_invite:
        invite = m_invite.group(1)
        try:
            info = await _call(CheckChatInviteRequest(invite), "CheckChatInvite")
            chat = getattr(info, "chat", None)
            if chat is not None:
                cid = get_peer_id(chat)
                logger.info("Канал по инвайту (уже участник): %s", cid)
                return cid
            updates = await _call(
                ImportChatInviteRequest(invite), "ImportChatInvite"
            )
            cid = get_peer_id(updates.chats[0])
            logger.info("Вступил в канал по инвайту: %s", cid)
            return cid
        except UserAlreadyParticipantError:
            info = await _call(CheckChatInviteRequest(invite), "CheckChatInvite")
            chat = getattr(info, "chat", None)
            if chat is not None:
                return get_peer_id(chat)
        except InviteHashExpiredError:
            logger.warning("Инвайт-ссылка истекла: %s", raw)
            found = await find_channel_in_dialogs(client)
            if found is not None:
                return found
            raise SystemExit(
                "Инвайт-ссылка канала истекла. Задай CHANNEL_ID=-100… "
                "или TARGET_CHANNEL=@username канала в Bothost."
            ) from None
        raise SystemExit(
            "Не удалось получить канал по инвайт-ссылке. "
            "Задай CHANNEL_ID=-100… в env."
        )

    m_user = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})$", raw)
    handle = raw.lstrip("@")
    if m_user:
        handle = m_user.group(1)
    if handle and not handle.startswith("+"):
        entity = await client.get_entity(handle)
        cid = get_peer_id(entity)
        logger.info("Канал по @%s: %s", handle, cid)
        return cid

    entity = await client.get_entity(raw)
    return get_peer_id(entity)


# ---------------------------------------------------------------- tracker


def _select_scan_batch(
    gift_ids: list[int],
    m: TelegramMarket,
    cfg: Config,
    *,
    baseline: bool,
) -> list[int]:
    n = len(gift_ids)
    if n == 0:
        return []
    if baseline or cfg.scan_batch <= 0 or cfg.scan_batch >= n:
        batch = list(gift_ids)
        random.shuffle(batch)
        return batch
    take = min(n, cfg.scan_batch)
    batch = [gift_ids[(m._cursor + i) % n] for i in range(take)]
    m._cursor = (m._cursor + take) % n
    return batch  # кольцо без shuffle — быстрый круг


async def _fetch_collection_pages(
    m: TelegramMarket,
    gid: int,
    cfg: Config,
    stats: dict[str, int],
) -> list[Lot]:
    """Только page1 resale; fallback без двойного счёта ошибок."""
    local: dict[str, int] = {"errors": 0, "floods": 0}
    result = await m._request(
        gid,
        cfg.page_limit,
        True,
        local,
        cfg.gap,
        cfg.timeout,
    )
    if result is None and not cfg.fast_scan:
        result = await m._request(
            gid,
            cfg.page_limit,
            False,
            local,
            cfg.gap,
            cfg.timeout,
        )
    stats["floods"] += local.get("floods", 0)
    if result is None:
        stats["errors"] += 1
        return []
    parsed = market_mod._parse_result(result)
    if parsed:
        stats["parsed"] += len(parsed)
        stats["ok"] += 1
    return parsed


def _collect_fresh_lot(
    lot: Lot,
    i: int,
    gid: int,
    seen: dict[str, float],
    collection_heads: dict[str, str],
    cfg: Config,
    *,
    baseline: bool,
    now: float,
) -> Lot | None:
    """Постим ТОЛЬКО при смене #1 resale — не то, что уже час висит наверху."""
    head_key = str(gid)
    if i >= cfg.hot_limit:
        if lot.id not in seen:
            seen[lot.id] = now
        return None

    if baseline:
        if i == 0:
            collection_heads[head_key] = lot.id
        seen[lot.id] = now
        return None

    if i != 0:
        return None

    prev_head = collection_heads.get(head_key)
    in_range = in_cheap_ton_window(lot.stars, cfg)

    # Первый раз видим коллекцию в кольце — запомнить #1, НЕ постить (может висеть час)
    if prev_head is None:
        collection_heads[head_key] = lot.id
        seen[lot.id] = now
        return None

    if prev_head == lot.id:
        return None

    # Смена головы: пусто→лот или старый #1→новый #1 = только что выставили
    collection_heads[head_key] = lot.id

    if lot.id in seen:
        return None
    if not in_range:
        seen[lot.id] = now
        return None

    lot.discovered_at = now
    lot.listed_at = now  # наш момент детекта смены #1
    return lot


async def poll_once(
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    collection_heads: dict[str, str],
    cfg: Config,
    *,
    baseline: bool,
    on_lot: Callable[[Lot], None] | None = None,
) -> tuple[list[Lot], dict[str, int | float | str]]:
    """Проход по коллекциям — лоты в очередь сразу по мере нахождения."""
    started = time.monotonic()
    api_stats: dict[str, int] = {"ok": 0, "errors": 0, "floods": 0, "parsed": 0}
    batch = _select_scan_batch(gift_ids, m, cfg, baseline=baseline)
    sem = asyncio.Semaphore(cfg.parallel)

    async def one(gid: int) -> tuple[int, list[Lot] | BaseException]:
        async with sem:
            try:
                return gid, await _fetch_collection_pages(m, gid, cfg, api_stats)
            except BaseException as exc:
                return gid, exc

    fresh: list[Lot] = []
    exc_errors = 0
    tasks = [asyncio.create_task(one(g)) for g in batch]
    for fut in asyncio.as_completed(tasks):
        gid, result = await fut
        if isinstance(result, BaseException):
            exc_errors += 1
            logger.warning("коллекция %s: %s", gid, result)
            continue
        now = time.time()
        if not result:
            collection_heads[str(gid)] = HEAD_EMPTY
            continue
        for i, lot in enumerate(result):
            accepted = _collect_fresh_lot(
                lot,
                i,
                gid,
                seen,
                collection_heads,
                cfg,
                baseline=baseline,
                now=now,
            )
            if accepted is None:
                continue
            fresh.append(accepted)
            if on_lot is not None:
                on_lot(accepted)
    stats: dict[str, int | float | str] = {
        "scanned": len(batch),
        "parsed": api_stats.get("parsed", 0),
        "ok": api_stats.get("ok", 0),
        "errors": api_stats.get("errors", 0) + exc_errors,
        "floods": api_stats.get("floods", 0),
        "collections_total": len(gift_ids),
        "batch_size": len(batch),
        "elapsed": round(time.monotonic() - started, 2),
    }
    if stats["floods"]:
        logger.warning("FloodWait x%s за проход — снижаю темп", stats["floods"])
    if int(stats["errors"]) > 0 and int(stats["scanned"]) > 0:
        err_ratio = int(stats["errors"]) / int(stats["scanned"])
        if err_ratio >= 0.5:
            logger.error(
                "Много ошибок API (%s/%s): %s",
                stats["errors"],
                stats["scanned"],
                m.last_error or "неизвестно",
            )
    return fresh, stats


async def enrich_one(m: TelegramMarket, lot: Lot, cfg: Config) -> None:
    """Быстрый enrich: resolve → profile+DM параллельно."""
    t = 0.75
    if not lot.seller or lot.seller_id is None:
        try:
            await m.resolve_owner(lot, timeout=t)
        except Exception:  # noqa: BLE001
            pass
    if not lot.seller_id:
        return
    need_profile = (
        not (lot.first_name or "").strip()
        or lot.account_level is None
        or lot.is_premium is None
        or lot.has_photo is None
        or lot.has_personal_channel is None
        or (cfg.loh_mode and lot.gifts_count is None)
        or (cfg.strict_ru and not lot.lang_code)
    )
    if need_profile:
        try:
            await m.enrich_profiles([lot], timeout=t, parallel=1)
        except Exception:  # noqa: BLE001
            pass


async def enrich(m: TelegramMarket, lots: list[Lot], cfg: Config) -> None:
    """Дотянуть username, lvl, статус ЛС — параллельно."""
    need_resolve = [lot for lot in lots if not lot.seller or lot.seller_id is None]
    if need_resolve:
        try:
            await m.resolve_owners(
                need_resolve,
                timeout=4.0,
                parallel=cfg.enrich_parallel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_owners: %s", exc)
    need_lvl = [lot for lot in lots if lot.seller_id is not None]
    if need_lvl:
        try:
            await m.enrich_profiles(
                need_lvl, timeout=4.0, parallel=cfg.enrich_parallel
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrich_profiles: %s", exc)
    try:
        await m.check_free_dm(lots, timeout=4.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("check_free_dm: %s", exc)


def mark_processed_lots(
    fresh: list[Lot], seen: dict[str, float], *, now: float
) -> int:
    """Помечаем обработанные лоты; без seller_key — оставляем на повтор."""
    retry = 0
    for lot in fresh:
        if not lot.seller_key:
            retry += 1
            continue
        seen[lot.id] = now
    return retry


def passes_account_level(
    lot: Lot, max_level: int, *, require_known: bool = False
) -> bool:
    """Level <= max или отрицательный рейтинг (как у Parser Gift)."""
    lvl = lot.account_level
    if lvl is None:
        return not require_known
    if lvl < 0:
        return True
    return lvl <= max_level


# Не имена: слишком часто у пацанов в никах (xxx_queen, baby_ton, darkangel).
_GENERIC_NICK_WORDS = frozenset(
    """
    baby kitty queen angel princess sweety cutie babe babygirl
    princessa kittycat angelbaby dummy lolita sweet xxx
    """.split()
)
_CRINGE_RE = re.compile(
    r"(💕|🌸|✨|🎀|🌷|💋|👑|💅|👩|💄|🐱|"
    r"милаш|зайка|зайчик|киса|киска|солныш|няша|няшка|лапочка|крошка|"
    r"принцесс|куколк|малышк|девоч|девуш|girl|woman|she/her|lolita)",
    re.IGNORECASE,
)
_MALE_HINT_RE = re.compile(
    r"(пацан|братан|bro\b|boy\b|\bman\b|муж|парень|мото|motor|bike|biker|"
    r"тачк|батя|мужик|качок)",
    re.IGNORECASE,
)
_MALE_NAME_ENDINGS = (
    "ий", "ей", "ёр", "ор", "ур", "им", "ом", "ен", "он", "ун",
)
# Дима/Рома/Саша и транслит — раньше проходили как «женское окончание а/я».
_MALE_NAMES = frozenset(
    """
    никита никитка илья ильюха фома кузьма савва данила данил даниил лука
    дима димка димон димуля дмитрий
    рома ромка роман
    коля колька николай
    вова вован володя вовка владимир
    саша сашка шура александр
    паша пашка пашук павел
    миша мишка михаил
    ваня ванек ванюша иван
    женя евгений
    леша леха алеша алексей
    юра юрка юрий
    степа степка степан
    толя толик анатолий
    витя витек виктор
    боря борис
    гоша гриша григорий
    слава вячеслав
    жора георгий
    лева лев
    тима тимофей
    федя федор
    петя петр
    сеня семен
    костя константин
    вася васек василий
    макс максим
    кирилл егор олег денис игорь артем андрей сергей
    матвей марк тимур руслан богдан ярослав глеб захар платон
    антон арсений владислав гордей
    семен серафим ростислав святослав доброслав
    dima dimka dimon dmitry dmitri dmitrii
    roma roman romka
    kolya kolyan nikolay nikolai
    vova vovan volodya vladimir vlad
    sasha sashka alexander alexandr alex
    pasha pashka pavel
    misha mishka mikhail michael mike
    vanya ivan
    zhenya evgeny evgeniy eugene
    lesha lyosha alexey alexei
    yura yuriy yuri
    stepa stepan
    tolya tolik
    vitya viktor victor
    borya boris
    gosha grisha
    slava
    tima timofey timothy
    fedya
    petya petr peter
    kostya
    vasya vasiliy
    max maxim maksim
    kirill egor yegor oleg denis igor artem andrey andrei sergey sergei
    matvey mark timur ruslan bogdan gleb anton
    danila danil daniel dan
    nikita ilya
    john jake tom bob paul chris david andrew
    """.split()
)
_NAME_CLEAN_RE = re.compile(r"[^a-zа-яё]+", re.IGNORECASE)


def _name_tokens(*parts: str) -> list[str]:
    out: list[str] = []
    for p in parts:
        raw = (p or "").strip().lower().replace("ё", "е")
        if not raw:
            continue
        out.append(raw)
        tok = _NAME_CLEAN_RE.sub(" ", raw)
        out.extend(x for x in tok.split() if x)
    return out


def _token_is_girl_name(tok: str) -> bool:
    if not tok or tok in _GENERIC_NICK_WORDS or tok in _MALE_NAMES:
        return False
    if tok in GIRL_NAMES:
        return True
    if tok.endswith("уха") and len(tok) >= 5:
        stem = tok[:-3]
        if stem in _MALE_NAMES:
            return False
        if stem in GIRL_NAMES or (stem + "я") in GIRL_NAMES:
            return True
    return False


def _token_is_male(tok: str) -> bool:
    if not tok or len(tok) < 2:
        return False
    if tok in _MALE_NAMES:
        return True
    if tok in GIRL_NAMES or tok in _GENERIC_NICK_WORDS:
        return False
    if len(tok) >= 4 and tok.endswith(_MALE_NAME_ENDINGS):
        return True
    return False


def _first_name_tokens(lot: Lot) -> list[str]:
    return [t for t in _name_tokens(lot.first_name or "") if t]


def _other_name_tokens(lot: Lot) -> list[str]:
    return [t for t in _name_tokens(lot.seller or "", lot.last_name or "") if t]


def _is_male_seller(lot: Lot) -> bool:
    """Пацан: мужское имя в first_name или, если имени нет, в нике."""
    fn = _first_name_tokens(lot)
    if any(_token_is_male(t) for t in fn):
        return True
    if any(_token_is_girl_name(t) for t in fn):
        return False
    if fn:
        return False
    if any(_token_is_male(t) for t in _other_name_tokens(lot)):
        return True
    blob = " ".join(
        x
        for x in (lot.first_name or "", lot.last_name or "", lot.about or "", lot.seller or "")
        if x
    )
    return bool(_MALE_HINT_RE.search(blob))


def is_ordinary_girl_name(lot: Lot) -> bool:
    """Лера, Катя, Настюха — не Дима/Саша и не baby/queen в нике."""
    if _is_male_seller(lot):
        return False
    fn = _first_name_tokens(lot)
    if any(_token_is_girl_name(t) for t in fn):
        return True
    if fn:
        return False
    return any(_token_is_girl_name(t) for t in _other_name_tokens(lot))


def is_cringe_girl_profile(lot: Lot) -> bool:
    if _is_male_seller(lot):
        return False
    blob = f"{lot.seller or ''} {lot.first_name or ''} {lot.about or ''}"
    return bool(_CRINGE_RE.search(blob))


def matches_girl_criteria(lot: Lot) -> bool:
    """Только девушки: женское имя/ник или явно девчачий ник. Ава/канал/сторис не считаются."""
    if _is_male_seller(lot):
        return False
    return is_ordinary_girl_name(lot) or is_cringe_girl_profile(lot)


def _looks_female(lot: Lot) -> bool:
    return matches_girl_criteria(lot)


def _looks_male(lot: Lot) -> bool:
    return _is_male_seller(lot)


def seller_persona(lot: Lot) -> str | None:
    if matches_girl_criteria(lot):
        return "female"
    if _looks_male(lot):
        return "male"
    return None


def passes_persona_filter(lot: Lot) -> str | None:
    """Только девушки: Катя/Лера/Настюха или девчачий ник. Пацанов нет."""
    if lot.is_premium is True:
        return "premium"
    if not matches_girl_criteria(lot):
        return "persona"
    return None


def passes_loh_filter(
    lot: Lot, *, max_gifts: int, max_level: int
) -> str | None:
    """Лохи: без TGP, низкий lvl, мало gifts, без канала.
    Обычные женские имена (Катя/Лера) — ава/короткое био ок.
    Позорный профиль — пустая ава / кринж-ник."""
    if lot.is_premium is True:
        return "premium"
    lvl = lot.account_level
    if lvl is None:
        return "level"
    if lvl >= 0 and lvl > max_level:
        return "level"
    gifts = lot.gifts_count
    if gifts is None:
        return "pro"
    if gifts > max_gifts:
        return "pro"
    if lot.has_personal_channel is True:
        return "pro"
    ordinary = is_ordinary_girl_name(lot)
    cringe = is_cringe_girl_profile(lot)
    about = (lot.about or "").strip()
    if ordinary or cringe:
        if len(about) > 80:
            return "pro"
        return None
    if lot.has_photo is True:
        return "pro"
    if about:
        return "pro"
    return None


def filter_for_post(
    lots: list[Lot],
    seen_sellers: dict[str, float],
    *,
    now: float,
    strict_ru: bool = True,
    strict_free: bool = False,
    max_account_level: int = 2,
    loh_mode: bool = True,
    max_gifts_count: int = 1,
    persona_mode: bool = True,
) -> tuple[list[Lot], dict[str, int]]:
    """RU + лох + персоны + бесплатные ЛС + один раз на продавца."""
    out: list[Lot] = []
    used: set[str] = set()
    stats = {
        "no_seller": 0,
        "dup": 0,
        "non_ru": 0,
        "paid": 0,
        "unknown_dm": 0,
        "level": 0,
        "premium": 0,
        "pro": 0,
        "persona": 0,
    }
    for lot in lots:
        key = lot.seller_key
        if not key:
            stats["no_seller"] += 1
            continue
        if key in used:
            continue
        prev = seen_sellers.get(key) if seen_sellers else None
        if prev is not None and now - float(prev) < SELLER_TTL:
            stats["dup"] += 1
            continue
        if strict_ru and not is_russian_lot(lot) and not _looks_female(lot):
            stats["non_ru"] += 1
            continue
        if loh_mode:
            if not passes_account_level(
                lot, max_account_level, require_known=True
            ):
                stats["level"] += 1
                continue
            skip = passes_loh_filter(
                lot, max_gifts=max_gifts_count, max_level=max_account_level
            )
            if skip:
                stats[skip] += 1
                continue
        if persona_mode:
            skip = passes_persona_filter(lot)
            if skip:
                stats[skip] += 1
                continue
        if strict_free:
            if lot.free_dm is not True:
                if lot.free_dm is False:
                    stats["paid"] += 1
                else:
                    stats["unknown_dm"] += 1
                continue
        elif lot.free_dm is False:
            stats["paid"] += 1
            continue
        used.add(key)
        out.append(lot)
    return out, stats


def _queue_priority(lot: Lot, cfg: Config, runtime: "TrackerRuntime") -> float:
    """Свежее = выше. Девушки всегда первые."""
    return -float(lot.listed_at or lot.discovered_at or time.time())


class PostQueue:
    """Очередь: enrich+фильтр+send в воркере — сканер не ждёт и не даёт пауз."""

    def __init__(
        self,
        sender: Sender,
        market: TelegramMarket,
        cfg: Config,
        seen: dict[str, float],
        seen_sellers: dict[str, float],
        state: dict,
        state_path: Path,
        runtime: TrackerRuntime,
        post_interval: float = 4.0,
    ) -> None:
        self._sender = sender
        self._m = market
        self._cfg = cfg
        self._seen = seen
        self._seen_sellers = seen_sellers
        self._state = state
        self._state_path = state_path
        self._runtime = runtime
        self._interval = max(0.15, float(post_interval))
        self._pq: asyncio.PriorityQueue[tuple[float, int, Lot | None]] = (
            asyncio.PriorityQueue()
        )
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._closed = False

    @property
    def pending(self) -> int:
        return self._pq.qsize()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._drip_worker(), name="post-drip")

    async def stop(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._seq += 1
            await self._pq.put((0.0, self._seq, None))
            try:
                await asyncio.wait_for(self._task, timeout=self._interval + 10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None

    def enqueue(self, lots: list[Lot]) -> int:
        if not lots:
            return 0
        for lot in lots:
            self._seq += 1
            prio = _queue_priority(lot, self._cfg, self._runtime)
            self._pq.put_nowait((prio, self._seq, lot))
        self._runtime.queue_pending = self.pending
        return len(lots)

    async def _drip_worker(self) -> None:
        logger.info("Drip: сразу в канал, без БД/профилей · /%.1fs", self._interval)
        while not self._closed:
            _, _, lot = await self._pq.get()
            if lot is None:
                break
            try:
                await enrich_one(self._m, lot, self._cfg)
                now = time.time()
                sellers = self._seen_sellers if self._cfg.persist_sellers else {}
                to_post, fstats = filter_for_post(
                    [lot],
                    sellers,
                    now=now,
                    strict_ru=self._cfg.strict_ru,
                    strict_free=False,
                    max_account_level=self._cfg.max_account_level,
                    loh_mode=self._cfg.loh_mode and not self._cfg.skip_enrich,
                    max_gifts_count=self._cfg.max_gifts_count,
                    persona_mode=True,
                )
                self._runtime.last_skip_ru = fstats["non_ru"]
                self._runtime.last_skip_dm = fstats["paid"] + fstats["unknown_dm"]
                self._runtime.last_skip_dup = fstats["dup"]
                self._runtime.last_skip_noseller = fstats["no_seller"]
                self._runtime.last_skip_level = fstats["level"]
                self._runtime.last_skip_premium = fstats["premium"]
                self._runtime.last_skip_pro = fstats["pro"]
                self._runtime.last_skip_persona = fstats["persona"]
                if not to_post:
                    if lot.seller_key:
                        self._seen[lot.id] = now
                    continue
                lot = to_post[0]
                await self._sender.send(lot)
                self._seen[lot.id] = now
                self._runtime.posted_total += 1
                if seller_persona(lot) == "female":
                    self._runtime.posted_female += 1
                self._runtime.queue_pending = self.pending
                logger.info(
                    "В канал: %s за %s⭐ (%s) · +%.2fs · очередь %s",
                    lot.title,
                    int(lot.stars),
                    lot_slug(lot),
                    time.time() - float(lot.discovered_at or time.time()),
                    self.pending,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Не отправилось (%s): %s", getattr(lot, "id", "?"), exc)
            finally:
                self._pq.task_done()


TRACKER_VERSION = "4.8"


@dataclass
class TrackerRuntime:
    """Статистика для /status и логов."""

    passes: int = 0
    posted_total: int = 0
    posted_female: int = 0
    last_fresh: int = 0
    last_posted: int = 0
    last_skip_ru: int = 0
    last_skip_dm: int = 0
    last_skip_dup: int = 0
    last_skip_noseller: int = 0
    last_skip_level: int = 0
    last_skip_premium: int = 0
    last_skip_pro: int = 0
    last_skip_persona: int = 0
    scan_parallel: int = 8
    seen_lots: int = 0
    queue_pending: int = 0
    collections_total: int = 0
    last_scan_batch: int = 0
    last_scan_parsed: int = 0
    last_scan_errors: int = 0
    last_scan_elapsed: float = 0.0
    last_api_error: str = ""
    watchers_alive: int = 0
    channel_id: int | None = None
    cfg: Config | None = None
    state_path: Path | None = None
    state: dict | None = None


def _load_session_from_db() -> str:
    """Сессия из Neptun Parser (gifts.db) — тот же volume /app/data."""
    try:
        from db import GiftDB

        db = GiftDB()
        acc = db.get_active_account()
        if acc and str(acc.get("session") or "").strip():
            return str(acc["session"]).strip()
        for row in db.list_accounts():
            sess = str(row.get("session") or "").strip()
            if sess:
                return sess
    except Exception as exc:  # noqa: BLE001
        logger.debug("session from db: %s", exc)
    return ""


async def _load_session_string(cfg: Config) -> str:
    if cfg.session_string:
        return cfg.session_string
    path = Path(cfg.session_file)
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    fallback = _load_session_from_db()
    if fallback:
        logger.info(
            "Сессия взята из gifts.db → сохраняю в %s", cfg.session_file
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fallback, encoding="utf-8")
    return fallback


async def _get_client(cfg: Config, store: ChannelStore) -> tuple[TelegramClient, ControlBot]:
    session_string = await _load_session_string(cfg)
    client: TelegramClient
    if session_string:
        client = TelegramClient(
            StringSession(session_string), cfg.api_id, cfg.api_hash
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            logger.warning(
                "Сессия в %s недействительна — нужен повторный вход",
                cfg.session_file,
            )
            client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
            await client.connect()
    else:
        client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
        await client.connect()

    bot = ControlBot(
        cfg.bot_token,
        client,
        cfg.session_file,
        store,
        tracker_version=TRACKER_VERSION,
    )
    await bot.start()

    if not await client.is_user_authorized():
        logger.warning(
            "⚠️ Трекер v%s: нет сессии — войди через @markskskdbot /start",
            TRACKER_VERSION,
        )
        await bot.wait_login()

    if not await client.is_user_authorized():
        raise RuntimeError("Вход не завершён")

    return client, bot


def watcher_slice(gift_ids: list[int], worker_id: int, watchers: int) -> list[int]:
    """NFT, на которых сидит задача worker_id из watchers."""
    if watchers <= 0:
        return list(gift_ids)
    return list(gift_ids[worker_id::watchers])


async def _watch_one_collection(
    m: TelegramMarket,
    gid: int,
    cfg: Config,
    seen: dict[str, float],
    collection_heads: dict[str, str],
    post_queue: PostQueue,
    *,
    baseline: bool,
) -> tuple[int, int]:
    """Один запрос resale #1. Возвращает (parsed, fresh)."""
    stats: dict[str, int] = {"errors": 0, "floods": 0, "parsed": 0, "ok": 0}
    try:
        lots = await _fetch_collection_pages(m, gid, cfg, stats)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NFT %s: %s", gid, exc)
        return 0, 0
    now = time.time()
    if not lots:
        if not baseline:
            collection_heads.setdefault(str(gid), HEAD_EMPTY)
        else:
            collection_heads[str(gid)] = HEAD_EMPTY
        return int(stats.get("parsed", 0)), 0
    fresh_n = 0
    for i, lot in enumerate(lots):
        accepted = _collect_fresh_lot(
            lot,
            i,
            gid,
            seen,
            collection_heads,
            cfg,
            baseline=baseline,
            now=now,
        )
        if accepted is None:
            continue
        if _is_male_seller(accepted):
            seen[accepted.id] = now
            continue
        post_queue.enqueue([accepted])
        fresh_n += 1
    return int(stats.get("parsed", 0) or len(lots)), fresh_n


async def collection_watcher(
    worker_id: int,
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    collection_heads: dict[str, str],
    cfg: Config,
    post_queue: PostQueue,
    runtime: TrackerRuntime,
    api_sem: asyncio.Semaphore,
) -> None:
    """После логина: запомнить #1, потом сидеть на своих NFT и ловить смену головы."""
    mine = watcher_slice(gift_ids, worker_id, cfg.watchers)
    if not mine:
        return
    logger.info("Задача %s/%s: сижу на %s NFT", worker_id + 1, cfg.watchers, len(mine))
    runtime.watchers_alive += 1
    try:
        for gid in mine:
            async with api_sem:
                await _watch_one_collection(
                    m, gid, cfg, seen, collection_heads, post_queue, baseline=True
                )
            await asyncio.sleep(0.002)

        pass_no = 0
        while True:
            mine = watcher_slice(gift_ids, worker_id, cfg.watchers)
            if not mine:
                await asyncio.sleep(0.2)
                continue
            started = time.monotonic()
            parsed = 0
            fresh = 0
            errors = 0
            for gid in mine:
                async with api_sem:
                    try:
                        p, f = await _watch_one_collection(
                            m,
                            gid,
                            cfg,
                            seen,
                            collection_heads,
                            post_queue,
                            baseline=False,
                        )
                        parsed += p
                        fresh += f
                    except Exception:  # noqa: BLE001
                        errors += 1
            pass_no += 1
            runtime.passes += 1
            runtime.seen_lots = len(seen)
            runtime.last_scan_parsed = parsed
            runtime.last_scan_errors = errors
            runtime.last_scan_elapsed = round(time.monotonic() - started, 2)
            runtime.last_scan_batch = len(mine)
            runtime.queue_pending = post_queue.pending
            if fresh:
                runtime.last_fresh = fresh
                runtime.last_posted = fresh
                logger.info(
                    "Задача %s: +%s новых → канал сразу · очередь %s · %ss",
                    worker_id + 1,
                    fresh,
                    post_queue.pending,
                    runtime.last_scan_elapsed,
                )
            elif pass_no % 40 == 0 and worker_id == 0:
                logger.info(
                    "Вотчеры живы %s · колл %s · очередь %s",
                    runtime.watchers_alive,
                    len(gift_ids),
                    post_queue.pending,
                )
            await asyncio.sleep(0.0)
    finally:
        runtime.watchers_alive = max(0, runtime.watchers_alive - 1)


async def watch_supervisor(
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    collection_heads: dict[str, str],
    cfg: Config,
    post_queue: PostQueue,
    runtime: TrackerRuntime,
    state_path: Path,
    state: dict,
) -> None:
    """40 задач на NFT + периодический сейв state и обновление каталога."""
    runtime.scan_parallel = cfg.watch_parallel
    api_sem = asyncio.Semaphore(cfg.watch_parallel)
    workers = [
        asyncio.create_task(
            collection_watcher(
                i,
                m,
                gift_ids,
                seen,
                collection_heads,
                cfg,
                post_queue,
                runtime,
                api_sem,
            ),
            name=f"nft-watch-{i}",
        )
        for i in range(cfg.watchers)
    ]
    logger.info(
        "Стартовал %s задач на %s NFT (parallel≤%s) — жду новые лоты",
        cfg.watchers,
        len(gift_ids),
        cfg.watch_parallel,
    )
    catalog_refreshed = time.monotonic()
    last_save = time.monotonic()
    try:
        while True:
            await asyncio.sleep(4.0)
            if time.monotonic() - last_save > 8.0:
                save_state(state_path, state)
                last_save = time.monotonic()
            if time.monotonic() - catalog_refreshed > 600:
                try:
                    gift_ids[:] = await m.load_collections(force=True)
                    runtime.collections_total = len(gift_ids)
                    catalog_refreshed = time.monotonic()
                except Exception:  # noqa: BLE001
                    pass
            dead = [t for t in workers if t.done()]
            for t in dead:
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    logger.error("Вотчер упал: %s", exc)
                idx = workers.index(t)
                workers[idx] = asyncio.create_task(
                    collection_watcher(
                        idx,
                        m,
                        gift_ids,
                        seen,
                        collection_heads,
                        cfg,
                        post_queue,
                        runtime,
                        api_sem,
                    ),
                    name=f"nft-watch-{idx}",
                )
    finally:
        for t in workers:
            t.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def scanner_loop(
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    collection_heads: dict[str, str],
    cfg: Config,
    post_queue: PostQueue,
    runtime: TrackerRuntime,
    state_path: Path,
    state: dict,
) -> None:
    """Совместимость: теперь это пул вотчеров, не кольцевой скан."""
    await watch_supervisor(
        m,
        gift_ids,
        seen,
        collection_heads,
        cfg,
        post_queue,
        runtime,
        state_path,
        state,
    )


async def run() -> None:
    _load_dotenv()
    cfg = Config.from_env()
    _singleton_lock = acquire_singleton_lock()
    store = ChannelStore(channel_file_path(data_dir()))
    control_bot: ControlBot | None = None

    client, control_bot = await _get_client(cfg, store)
    me = await client.get_me()
    logger.info(
        "✅ Парсер v%s · %s · %s вотчеров · канал %s · пост /%ss",
        TRACKER_VERSION,
        me.username or me.first_name,
        cfg.watchers,
        FIXED_CHANNEL_ID,
        int(cfg.post_interval),
    )
    logger.warning(
        "Сессия: один инстанс Bothost; не жми /start повторно; "
        "не запускай parser+tracker на одном аккаунте"
    )

    state_path = Path(cfg.state_file)
    state = load_state(state_path)
    seen: dict[str, float] = state["seen"]
    seen_sellers: dict[str, float] = state.get("seen_sellers", {})
    collection_heads: dict[str, str] = state.setdefault("collection_heads", {})

    chat_id = pin_fixed_channel(store, state, state_path)
    logger.info(
        "Канал: %s (постоянно) · %.0f–%.0f TON (%s–%s⭐) · %s задач на NFT",
        chat_id,
        cfg.min_ton,
        cfg.max_ton,
        int(cfg.min_stars),
        int(cfg.max_stars),
        cfg.watchers,
    )

    runtime = TrackerRuntime(
        channel_id=chat_id,
        cfg=cfg,
        state_path=state_path,
        state=state,
        seen_lots=len(seen),
        scan_parallel=cfg.parallel,
    )

    dd = data_dir()
    rate_limiter = PostRateLimiter(
        cfg.post_interval,
        dd / "tracker_post.lock",
        dd / "tracker_last_post.txt",
    )
    m = TelegramMarket(client)
    _setup_catalog_hooks(m)
    sender = Sender(cfg, client, rate_limiter=rate_limiter)
    sender.chat_id = chat_id
    control_bot.runtime = runtime
    control_bot.sender = sender

    post_queue = PostQueue(
        sender,
        m,
        cfg,
        seen,
        seen_sellers,
        state,
        state_path,
        runtime,
        post_interval=cfg.post_interval,
    )
    post_queue.start()
    control_bot.post_queue = post_queue

    gift_ids = await wait_for_gift_ids(m)
    if not gift_ids:
        raise SystemExit("Не удалось загрузить коллекции — проверь сессию")
    logger.info(
        "Коллекций: %s · вотчеров %s · каждый сидит на ~%s NFT · пост /%ss",
        len(gift_ids),
        cfg.watchers,
        max(1, (len(gift_ids) + cfg.watchers - 1) // cfg.watchers),
        int(cfg.post_interval),
    )

    runtime.collections_total = len(gift_ids)
    pin_fixed_channel(store, state, state_path, sender)

    logger.info("Логин ок — копирую %s задач, запоминаю текущие #1, потом жду новые", cfg.watchers)
    scan_task = asyncio.create_task(
        watch_supervisor(
            m,
            gift_ids,
            seen,
            collection_heads,
            cfg,
            post_queue,
            runtime,
            state_path,
            state,
        ),
        name="watch-pool",
    )
    try:
        await scan_task
    finally:
        scan_task.cancel()
        await post_queue.stop()
        await sender.close()
        if control_bot:
            await control_bot.stop()
        await client.disconnect()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Остановлен.")


if __name__ == "__main__":
    main()
