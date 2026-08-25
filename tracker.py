"""
Гифт-трекер внутреннего маркета Telegram.

Ловит только что выставленные на перепродажу NFT-подарки (за Stars),
фильтрует по цене MIN_STARS..MAX_STARS и постит карточки в канал.

Запуск:  python3 tracker.py
Настройки берутся из .env (см. .env.example) или переменных окружения.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

logger = logging.getLogger("tracker")

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_BOT_TOKEN = "8807847926:AAGIoGRUd9Pw8LSIJmx5qRSaqZUn2hx4-sI"
# Инвайты протухают — задавай CHANNEL_ID или TARGET_CHANNEL=@username в env
DEFAULT_TARGET_CHANNEL = ""
CHANNEL_NAME_HINTS = ("tracker market", "tracker", "market")


def data_dir() -> Path:
    """Bothost хранит данные в /app/data; локально — рядом со скриптом."""
    bothost = Path("/app/data")
    if bothost.is_dir():
        return bothost
    return BASE_DIR


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
    min_stars: float = 500.0
    max_stars: float = 5000.0
    poll_interval: float = 2.0
    page_limit: int = 25
    parallel: int = 10
    gap: float = 0.05
    timeout: float = 6.0
    scan_pages: int = 2  # страниц resale на коллекцию
    post_interval: float = 3.0  # сек между постами в канал (строгий тикер)
    ton_rate: float = 0.0102  # TON за 1 Star (для строки "X Stars / Y TON")
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
        channel_id_raw = os.environ.get("CHANNEL_ID", "").strip()
        channel_id: int | None = None
        if channel_id_raw and re.fullmatch(r"-?\d+", channel_id_raw):
            channel_id = int(channel_id_raw)
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_string=os.environ.get("SESSION_STRING", "").strip(),
            bot_token=bot_token,
            target_channel=target,
            min_stars=_f("MIN_STARS", 500),
            max_stars=_f("MAX_STARS", 5000),
            poll_interval=_f("POLL_INTERVAL", 2.0),
            page_limit=int(_f("PAGE_LIMIT", 25)),
            parallel=int(_f("PARALLEL", 10)),
            gap=_f("REQUEST_GAP", 0.05),
            timeout=_f("REQUEST_TIMEOUT", 6.0),
            scan_pages=max(1, int(_f("SCAN_PAGES", 2))),
            post_interval=_f("POST_INTERVAL", 3.0),
            ton_rate=_f("TON_RATE", 0.0102),
            tz_offset=_f("TZ_OFFSET", 3.0),
            session_file=session_file,
            state_file=state_file,
            post_on_first_run=os.environ.get("POST_ON_FIRST_RUN", "0") == "1",
            channel_id=channel_id,
            strict_ru=os.environ.get("TRACKER_STRICT_RU", "1") != "0",
            strict_free=os.environ.get("TRACKER_STRICT_FREE", "0") == "1",
        )


# ---------------------------------------------------------------- state

SEEN_TTL = 7 * 24 * 3600  # помним лот неделю — дальше номер уже не «новый»
SELLER_TTL = 90 * 24 * 3600  # одного продавца не постим повторно 90 дней


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("seen", {})
            data.setdefault("seen_sellers", {})
            data.setdefault("channel_id", None)
            return data
    except (OSError, ValueError):
        pass
    return {"seen": {}, "seen_sellers": {}, "channel_id": None}


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


class Sender:
    """Шлёт карточки: через бота (с кнопками) или от юзер-сессии."""

    def __init__(self, cfg: Config, client: TelegramClient) -> None:
        self.cfg = cfg
        self.client = client
        self.chat_id: int | None = None
        self._bot = None
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

    async def send(self, lot: Lot) -> None:
        text = format_lot(lot, self.cfg)
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


async def obtain_channel_id(
    client: TelegramClient,
    cfg: Config,
    state: dict,
    state_path: Path,
    store: Any,
) -> int:
    """Канал: env → файл → state → target → диалоги → ждём /setchannel."""
    if cfg.channel_id:
        cid = int(cfg.channel_id)
        store.save(cid)
        state["channel_id"] = cid
        save_state(state_path, state)
        logger.info("Канал из CHANNEL_ID: %s", cid)
        return cid

    saved = store.load()
    if saved is not None:
        state["channel_id"] = saved
        save_state(state_path, state)
        logger.info("Канал из файла: %s", saved)
        return saved

    if state.get("channel_id"):
        cid = int(state["channel_id"])
        store.save(cid)
        logger.info("Канал из state: %s", cid)
        return cid

    while True:
        if cfg.target_channel.strip():
            try:
                cid = await resolve_channel(client, cfg.target_channel)
                store.save(cid)
                state["channel_id"] = cid
                save_state(state_path, state)
                return cid
            except SystemExit as exc:
                logger.warning("resolve_channel: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("resolve_channel: %s", exc)

        found = await find_channel_in_dialogs(client)
        if found is not None:
            store.save(found)
            state["channel_id"] = found
            save_state(state_path, state)
            return found

        logger.warning(
            "Канал не задан. Открой @markskskdbot → /channels или "
            "/setchannel @имя_канала (жду 60с…)"
        )
        waited = await store.wait(timeout=60.0)
        if waited is not None:
            state["channel_id"] = waited
            save_state(state_path, state)
            return waited


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


async def poll_once(
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    cfg: Config,
    *,
    baseline: bool,
) -> list[Lot]:
    """Проход по всем коллекциям (несколько страниц) — лоты до max_stars."""
    stats = {"ok": 0, "errors": 0, "floods": 0}
    sem = asyncio.Semaphore(cfg.parallel)

    async def one(gid: int) -> list[Lot]:
        async with sem:
            out: list[Lot] = []
            offset = ""
            for _ in range(max(1, int(cfg.scan_pages))):
                result = await m._request(
                    gid,
                    cfg.page_limit,
                    True,
                    stats,
                    cfg.gap,
                    cfg.timeout,
                    offset=offset,
                )
                if result is None:
                    break
                out.extend(market_mod._parse_result(result))
                offset = str(getattr(result, "next_offset", "") or "")
                if not offset:
                    break
            return out

    chunks = await asyncio.gather(
        *(one(g) for g in gift_ids), return_exceptions=True
    )
    now = time.time()
    fresh: list[Lot] = []
    for lots in chunks:
        if isinstance(lots, BaseException):
            continue
        for lot in lots:
            if lot.id in seen:
                continue
            seen[lot.id] = now
            if baseline:
                continue
            if cfg.min_stars <= lot.stars <= cfg.max_stars:
                fresh.append(lot)
    if stats["floods"]:
        logger.warning("FloodWait x%s за проход — снижаю темп", stats["floods"])
    return fresh


async def enrich(m: TelegramMarket, lots: list[Lot]) -> None:
    """Дотянуть username, lvl, статус ЛС — для каждого нового лота."""
    for lot in lots:
        if not lot.seller or not lot.seller_id:
            try:
                await m.resolve_owner(lot, timeout=2.5)
            except Exception:  # noqa: BLE001
                pass
    need_lvl = [lot for lot in lots if lot.seller_id is not None]
    if need_lvl:
        try:
            await m.enrich_profiles(need_lvl, timeout=3.0, parallel=4)
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrich_profiles: %s", exc)
    try:
        await m.check_free_dm(lots, timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("check_free_dm: %s", exc)


def filter_for_post(
    lots: list[Lot],
    seen_sellers: dict[str, float],
    *,
    now: float,
    strict_ru: bool = True,
    strict_free: bool = False,
) -> tuple[list[Lot], dict[str, int]]:
    """RU + бесплатные ЛС + один раз на продавца."""
    out: list[Lot] = []
    used: set[str] = set()
    stats = {
        "no_seller": 0,
        "dup": 0,
        "non_ru": 0,
        "paid": 0,
        "unknown_dm": 0,
    }
    for lot in lots:
        key = lot.seller_key
        if not key:
            stats["no_seller"] += 1
            continue
        if key in used:
            continue
        prev = seen_sellers.get(key)
        if prev is not None and now - float(prev) < SELLER_TTL:
            stats["dup"] += 1
            continue
        if strict_ru and not is_russian_lot(lot):
            stats["non_ru"] += 1
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


_FEMALE_HINT_RE = re.compile(
    r"(девоч|девуш|girl|woman|she/her|👩|💅|💄|🎀)",
    re.IGNORECASE,
)

def _looks_female(lot: Lot) -> bool:
    blob = " ".join(
        x
        for x in (
            lot.first_name or "",
            lot.last_name or "",
            lot.about or "",
            lot.seller or "",
        )
        if x
    ).lower()
    if _FEMALE_HINT_RE.search(blob):
        return True
    fn = (lot.first_name or "").strip().lower()
    if len(fn) >= 3:
        if fn.endswith(("ия", "ья", "на", "та", "са", "ка", "ла", "ра", "ва", "ша")):
            return True
        if fn[-1] in "ая":
            return True
    return False


def _lot_priority(lot: Lot, *, boost_female: bool) -> float:
    """Выше = раньше в очереди. Без TGP в приоритете, девочки — иногда."""
    score = random.random() * 0.3
    if lot.is_premium is False:
        score += 4.0
    elif lot.is_premium is True:
        score -= 3.0
    if lot.free_dm is True:
        score += 1.5
    elif lot.free_dm is False:
        score -= 5.0
    if boost_female and _looks_female(lot):
        score += 2.5
    return score


def rank_for_queue(lots: list[Lot]) -> list[Lot]:
    boost_female = random.random() < 0.35
    ranked = sorted(
        lots, key=lambda lot: -_lot_priority(lot, boost_female=boost_female)
    )
    if boost_female:
        logger.info("очередь: буст «девочки» включён для этого батча")
    return ranked


class PostQueue:
    """Строгий drip: максимум 1 пост каждые N секунд (не пачкой)."""

    def __init__(
        self,
        sender: Sender,
        *,
        interval: float,
        seen_sellers: dict[str, float],
        state: dict,
        state_path: Path,
        runtime: TrackerRuntime,
        lock_path: Path | None = None,
    ) -> None:
        self._sender = sender
        self._interval = max(1.0, float(interval))
        self._seen_sellers = seen_sellers
        self._state = state
        self._state_path = state_path
        self._runtime = runtime
        self._lock_path = lock_path
        self._q: asyncio.Queue[Lot | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._last_post_mono = 0.0

    @property
    def pending(self) -> int:
        return self._q.qsize()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._drip_worker(), name="post-drip")

    async def stop(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            await self._q.put(None)
            try:
                await asyncio.wait_for(self._task, timeout=self._interval + 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None

    def enqueue(self, lots: list[Lot]) -> int:
        if not lots:
            return 0
        for lot in rank_for_queue(lots):
            self._q.put_nowait(lot)
        self._runtime.queue_pending = self.pending
        return len(lots)

    async def _wait_tick(self) -> None:
        """Ровный интервал от предыдущего поста."""
        now = time.monotonic()
        if self._last_post_mono > 0:
            wait = self._interval - (now - self._last_post_mono)
            if wait > 0:
                await asyncio.sleep(wait)
        else:
            await asyncio.sleep(self._interval)

    def _try_file_lock(self) -> Any | None:
        if not self._lock_path:
            return None
        try:
            import fcntl

            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._lock_path.open("w")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            return None

    async def _send_one(self, lot: Lot) -> None:
        async with self._send_lock:
            lock_handle = self._try_file_lock()
            if self._lock_path is not None and lock_handle is None:
                # другой инстанс Bothost постит — вернём в очередь
                self._q.put_nowait(lot)
                logger.warning("другой инстанс постит — лот возвращён в очередь")
                return
            try:
                now = time.time()
                await self._sender.send(lot)
                self._last_post_mono = time.monotonic()
                key = lot.seller_key
                if key:
                    self._seen_sellers[key] = now
                self._state["seen_sellers"] = self._seen_sellers
                save_state(self._state_path, self._state)
                self._runtime.posted_total += 1
                self._runtime.queue_pending = self.pending
                logger.info(
                    "Отправил: %s за %s⭐ (%s) · очередь %s · интервал %ss",
                    lot.title,
                    int(lot.stars),
                    lot_slug(lot),
                    self.pending,
                    int(self._interval),
                )
            finally:
                if lock_handle is not None:
                    try:
                        import fcntl

                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        lock_handle.close()
                    except OSError:
                        pass

    async def _drip_worker(self) -> None:
        logger.info(
            "Drip-постинг: 1 лот каждые %ss (очередь отдельно от парсера)",
            int(self._interval),
        )
        while not self._closed:
            await self._wait_tick()
            if self._closed:
                break
            lot: Lot | None = None
            try:
                lot = self._q.get_nowait()
            except asyncio.QueueEmpty:
                continue
            if lot is None:
                break
            try:
                await self._send_one(lot)
            except Exception as exc:  # noqa: BLE001
                logger.error("Не отправилось (%s): %s", getattr(lot, "id", "?"), exc)


TRACKER_VERSION = "2.6"


@dataclass
class TrackerRuntime:
    """Статистика для /status и логов."""

    passes: int = 0
    posted_total: int = 0
    last_fresh: int = 0
    last_posted: int = 0
    last_skip_ru: int = 0
    last_skip_dm: int = 0
    last_skip_dup: int = 0
    last_skip_noseller: int = 0
    seen_lots: int = 0
    queue_pending: int = 0
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


async def run() -> None:
    _load_dotenv()
    cfg = Config.from_env()
    store = ChannelStore(channel_file_path(data_dir()))
    control_bot: ControlBot | None = None

    client, control_bot = await _get_client(cfg, store)
    me = await client.get_me()
    logger.info(
        "✅ Трекер v%s запущен · %s (id=%s) · фильтры: RU + бесплатные ЛС",
        TRACKER_VERSION,
        me.username or me.first_name,
        me.id,
    )

    state_path = Path(cfg.state_file)
    state = load_state(state_path)
    seen: dict[str, float] = state["seen"]
    seen_sellers: dict[str, float] = state.get("seen_sellers", {})

    chat_id = await obtain_channel_id(client, cfg, state, state_path, store)
    logger.info(
        "Канал для постинга: %s · диапазон %s–%s⭐ · пост /%ss · RU=%s",
        chat_id,
        int(cfg.min_stars),
        int(cfg.max_stars),
        cfg.post_interval,
        "строго" if cfg.strict_ru else "нет",
    )

    runtime = TrackerRuntime(
        channel_id=chat_id,
        cfg=cfg,
        state_path=state_path,
        state=state,
        seen_lots=len(seen),
    )

    sender = Sender(cfg, client)
    sender.chat_id = chat_id
    control_bot.runtime = runtime
    control_bot.sender = sender

    post_queue = PostQueue(
        sender,
        interval=cfg.post_interval,
        seen_sellers=seen_sellers,
        state=state,
        state_path=state_path,
        runtime=runtime,
        lock_path=data_dir() / "tracker_post.lock",
    )
    post_queue.start()
    control_bot.post_queue = post_queue

    m = TelegramMarket(client)
    gift_ids = await m.load_collections(force=True)
    if not gift_ids:
        raise SystemExit("Не удалось загрузить список коллекций подарков")
    logger.info(
        "Коллекций с resale: %s · drip %ss · очередь отдельным потоком",
        len(gift_ids),
        int(cfg.post_interval),
    )

    baseline = not seen and not cfg.post_on_first_run
    if baseline:
        logger.info("Первый запуск: запоминаю текущие лоты без постинга…")
        await poll_once(m, gift_ids, seen, cfg, baseline=True)
        save_state(state_path, state)
        logger.info("Запомнено %s лотов. Слежу за новыми.", len(seen))

    catalog_refreshed = time.monotonic()
    pass_no = 0
    try:
        while True:
            started = time.monotonic()
            pass_no += 1
            runtime.passes = pass_no
            runtime.seen_lots = len(seen)
            try:
                fresh = await poll_once(m, gift_ids, seen, cfg, baseline=False)
            except Exception as exc:  # noqa: BLE001
                logger.error("Проход упал: %s", exc)
                await asyncio.sleep(3)
                continue

            runtime.last_fresh = len(fresh)
            if fresh:
                fresh.sort(key=lambda l: l.stars)
                await enrich(m, fresh)
                now = time.time()
                to_post, fstats = filter_for_post(
                    fresh,
                    seen_sellers,
                    now=now,
                    strict_ru=cfg.strict_ru,
                    strict_free=cfg.strict_free,
                )
                logger.info(
                    "Проход #%s: новых %s → в очередь %s "
                    "(ru−%s dm−%s dup−%s noseller−%s · ждут %s)",
                    pass_no,
                    len(fresh),
                    len(to_post),
                    fstats["non_ru"],
                    fstats["paid"] + fstats["unknown_dm"],
                    fstats["dup"],
                    fstats["no_seller"],
                    post_queue.pending,
                )
                runtime.last_skip_ru = fstats["non_ru"]
                runtime.last_skip_dm = fstats["paid"] + fstats["unknown_dm"]
                runtime.last_skip_dup = fstats["dup"]
                runtime.last_skip_noseller = fstats["no_seller"]
                if to_post:
                    post_queue.enqueue(to_post)
                    runtime.queue_pending = post_queue.pending
                    runtime.last_posted = len(to_post)
            elif pass_no % 15 == 0:
                logger.info(
                    "Проход #%s: новых лотов в %s–%s⭐ нет (seen=%s, posted=%s)",
                    pass_no,
                    int(cfg.min_stars),
                    int(cfg.max_stars),
                    len(seen),
                    runtime.posted_total,
                )

            # каталог коллекций обновляем раз в 10 минут (новые гифты)
            if time.monotonic() - catalog_refreshed > 600:
                try:
                    gift_ids = await m.load_collections(force=True)
                    catalog_refreshed = time.monotonic()
                except Exception:  # noqa: BLE001
                    pass

            spent = time.monotonic() - started
            await asyncio.sleep(max(cfg.poll_interval - spent, 0.2))
    finally:
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
