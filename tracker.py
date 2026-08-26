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

logger = logging.getLogger("tracker")

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_BOT_TOKEN = "8807847926:AAF5Ej4HyZNhCh76cIUKvoJCuis9q1fi-nM"
# Канал tracker market — можно переопределить CHANNEL_ID в env Bothost
DEFAULT_CHANNEL_ID = -1004384888475
DEFAULT_TARGET_CHANNEL = ""
CHANNEL_NAME_HINTS = ("tracker market", "tracker", "market")


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
    """Кэш коллекций: gifts.db → tracker_catalog.json → сеть."""

    def _load() -> tuple[list[int], int] | None:
        cached = _load_catalog_file()
        if cached:
            return cached
        try:
            from db import GiftDB

            return GiftDB().load_gift_catalog()
        except Exception:  # noqa: BLE001
            return None

    def _save(ids: list[int], h: int) -> None:
        _save_catalog_file(ids, h)
        try:
            from db import GiftDB

            GiftDB().save_gift_catalog(ids, h)
        except Exception:  # noqa: BLE001
            pass

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
    min_stars: float = 500.0
    max_stars: float = 5000.0
    poll_interval: float = 2.0
    page_limit: int = 12  # только верх resale-листа
    parallel: int = 4  # не выше 6 — иначе Telegram кикает сессию
    gap: float = 0.25
    timeout: float = 10.0
    enrich_cap: int = 60  # legacy; сканер больше не ждёт enrich
    enrich_parallel: int = 4
    scan_pages: int = 1  # только 1-я страница resale = самые свежие
    scan_batch: int = 50  # ротация: не долбим все 149 колл за раз
    hot_limit: int = 4  # топ-N = только что выставленные
    max_account_level: int = 2  # level <= 2 или отрицательный рейтинг
    noob_mode: bool = True  # низкий lvl, мало gifts; TGP ок для девочек
    max_gifts_count: int = 5  # gifts_count > N → профи (кроме женских)
    female_mix_target: float = 0.30  # ~30% постов — «богатые» женские профили
    post_interval: float = 4.0  # сек между постами в канал (строгий тикер)
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
        channel_id: int | None = DEFAULT_CHANNEL_ID
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
            page_limit=int(_f("PAGE_LIMIT", 12)),
            parallel=min(6, int(_f("PARALLEL", 4))),
            gap=_f("REQUEST_GAP", 0.25),
            timeout=_f("REQUEST_TIMEOUT", 10.0),
            enrich_cap=max(10, int(_f("ENRICH_CAP", 60))),
            enrich_parallel=max(2, min(4, int(_f("ENRICH_PARALLEL", 4)))),
            scan_pages=max(1, int(_f("SCAN_PAGES", 1))),
            scan_batch=int(_f("SCAN_BATCH", 50)),
            hot_limit=max(1, int(_f("HOT_LIMIT", 4))),
            max_account_level=int(_f("MAX_ACCOUNT_LEVEL", 2)),
            noob_mode=os.environ.get("TRACKER_NOOB_MODE", "1") == "1",
            max_gifts_count=max(0, int(_f("MAX_GIFTS_COUNT", 5))),
            female_mix_target=min(1.0, max(0.0, _f("FEMALE_MIX_TARGET", 0.30))),
            post_interval=_f("POST_INTERVAL", 4.0),
            ton_rate=_f("TON_RATE", 0.0102),
            tz_offset=_f("TZ_OFFSET", 3.0),
            session_file=session_file,
            state_file=state_file,
            post_on_first_run=os.environ.get("POST_ON_FIRST_RUN", "0") == "1",
            channel_id=channel_id,
            strict_ru=os.environ.get("TRACKER_STRICT_RU", "1") == "1",
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


class PostRateLimiter:
    """Один пост на весь Bothost: блокирующий flock + общий timestamp-файл."""

    def __init__(
        self, interval: float, lock_path: Path, timestamp_path: Path
    ) -> None:
        self._interval = max(1.0, float(interval))
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
    return batch


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
    if result is None:
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
    m._remember_users(market_mod._extract_users(result))
    parsed = market_mod._parse_result(result)
    if parsed:
        stats["parsed"] += len(parsed)
        stats["ok"] += 1
    return parsed


def _collect_fresh_lot(
    lot: Lot,
    i: int,
    seen: dict[str, float],
    cfg: Config,
    *,
    baseline: bool,
    now: float,
) -> Lot | None:
    """Проверка одного лота со страницы resale; None = не в очередь."""
    if i >= cfg.hot_limit:
        if lot.id not in seen:
            seen[lot.id] = now
        return None
    if lot.id in seen:
        return None
    if baseline:
        seen[lot.id] = now
        return None
    if not (cfg.min_stars <= lot.stars <= cfg.max_stars):
        seen[lot.id] = now
        return None
    lot.discovered_at = now
    return lot


async def poll_once(
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
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
        for i, lot in enumerate(result):
            accepted = _collect_fresh_lot(
                lot, i, seen, cfg, baseline=baseline, now=now
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
    """Быстрый enrich одного лота (только недостающие поля)."""
    if not lot.seller or lot.seller_id is None:
        try:
            await m.resolve_owner(lot, timeout=2.5)
        except Exception:  # noqa: BLE001
            pass
    need_profile = lot.seller_id is not None and (
        lot.account_level is None
        or lot.free_dm is None
        or lot.is_premium is None
        or lot.has_photo is None
        or lot.has_personal_channel is None
        or (cfg.noob_mode and lot.gifts_count is None)
        or (cfg.strict_ru and not lot.lang_code)
    )
    if need_profile:
        try:
            await m.enrich_profiles([lot], timeout=2.5, parallel=1)
        except Exception:  # noqa: BLE001
            pass
    if lot.free_dm is None and lot.seller_id is not None:
        try:
            await m.check_free_dm([lot], timeout=2.5)
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


def passes_persona_filter(lot: Lot, *, max_gifts: int) -> str | None:
    """Персоны: девочки (TGP ок) · пустые мужики без TGP · остальные нубы."""
    female = _looks_female(lot)
    gifts = lot.gifts_count
    if gifts is not None and gifts > max_gifts:
        cap = max_gifts * 3 if female else max_gifts
        if gifts > cap:
            return "pro"

    if female:
        return None

    if lot.is_premium is True:
        about = (lot.about or "").strip()
        sparse = (
            lot.has_photo is not True
            and not lot.has_personal_channel
            and len(about) < 24
        )
        if sparse or _matches_male_empty(lot):
            return "premium"

    if _matches_male_empty(lot):
        return None

    return None


def filter_for_post(
    lots: list[Lot],
    seen_sellers: dict[str, float],
    *,
    now: float,
    strict_ru: bool = True,
    strict_free: bool = False,
    max_account_level: int = 2,
    noob_mode: bool = True,
    max_gifts_count: int = 5,
) -> tuple[list[Lot], dict[str, int]]:
    """RU + level + бесплатные ЛС + один раз на продавца."""
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
        if not passes_account_level(
            lot, max_account_level, require_known=noob_mode
        ):
            stats["level"] += 1
            continue
        if noob_mode:
            skip = passes_persona_filter(lot, max_gifts=max_gifts_count)
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


_FEMALE_HINT_RE = re.compile(
    r"(девоч|девуш|girl|woman|she/her|👩|💅|💄|🎀|милаш|princess|queen|baby)",
    re.IGNORECASE,
)
_MOTO_RE = re.compile(
    r"(мото|motor|bike|biker|байк|квадро|drive|тачк|авто|yamaha|kawasaki|harley)",
    re.IGNORECASE,
)
_CRINGE_NICK_RE = re.compile(
    r"(💕|🌸|✨|🎀|🌷|💋|kitty|angel|babe|xxx|ххх|милаш|princess|queen|baby|"
    r"солныш|зайка|киса|милая|душа|love|sweet)",
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
        if fn.endswith(
            ("ия", "ья", "ина", "ена", "ана", "юля", "уля", "оля", "еля", "ня", "ша")
        ):
            return True
        if fn.endswith(("та", "са", "ка", "ла", "ра", "ва")) and not fn.endswith(
            ("ася", "ося")
        ):
            return True
        if fn.endswith("ая"):
            return True
    return False


def _cringe_nick(lot: Lot) -> bool:
    blob = f"{lot.seller or ''} {lot.first_name or ''}".strip()
    if not blob:
        return False
    if _CRINGE_NICK_RE.search(blob):
        return True
    if re.search(r"[_\.]{2,}|[0-9]{3,}", blob):
        return True
    if any(ch in blob for ch in "💕🌸✨🎀🌷💋👑"):
        return True
    return False


def _matches_male_empty(lot: Lot) -> bool:
    """Пустой мужской акк: без авы, без TGP, мото/пустое био."""
    if _looks_female(lot):
        return False
    if lot.is_premium is True:
        return False
    if lot.has_photo is True:
        return False
    if lot.gifts_count is not None and lot.gifts_count > 3:
        return False
    if lot.has_personal_channel is True:
        return False
    about = (lot.about or "").strip()
    if _MOTO_RE.search(about):
        return True
    if about:
        return len(about) < 48
    return True


def _female_rich_score(lot: Lot) -> float:
    if not _looks_female(lot):
        return 0.0
    score = 0.0
    if lot.has_photo:
        score += 2.5
    if (lot.about or "").strip():
        score += 1.5
    if lot.has_personal_channel:
        score += 2.0
    if lot.gifts_count and lot.gifts_count > 0:
        score += 1.0
    if _cringe_nick(lot):
        score += 2.0
    if lot.is_premium:
        score += 1.0
    return score


def is_female_rich(lot: Lot, *, threshold: float = 4.0) -> bool:
    return _female_rich_score(lot) >= threshold


def _queue_priority(lot: Lot, cfg: Config, runtime: "TrackerRuntime") -> float:
    """Свежее первым; ~30% очереди — женские «кринж»-профили."""
    prio = -float(lot.discovered_at or time.time())
    if not is_female_rich(lot):
        return prio
    total = max(runtime.posted_total, 1)
    ratio = runtime.posted_female_rich / total
    target = cfg.female_mix_target
    if ratio < target:
        prio -= 800.0
    elif ratio > target + 0.12:
        prio += 400.0
    return prio


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
    """Сначала самые свежие (только что увидели на маркете)."""
    return sorted(lots, key=lambda lot: -float(lot.discovered_at or 0))


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
        self._interval = max(1.0, float(post_interval))
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
        logger.info(
            "Drip: свежие первые · enrich+send /%ss (сканер параллельно)",
            int(self._interval),
        )
        while not self._closed:
            _, _, lot = await self._pq.get()
            if lot is None:
                break
            try:
                await enrich_one(self._m, lot, self._cfg)
                now = time.time()
                to_post, fstats = filter_for_post(
                    [lot],
                    self._seen_sellers,
                    now=now,
                    strict_ru=self._cfg.strict_ru,
                    strict_free=self._cfg.strict_free,
                    max_account_level=self._cfg.max_account_level,
                    noob_mode=self._cfg.noob_mode,
                    max_gifts_count=self._cfg.max_gifts_count,
                )
                self._runtime.last_skip_ru = fstats["non_ru"]
                self._runtime.last_skip_dm = fstats["paid"] + fstats["unknown_dm"]
                self._runtime.last_skip_dup = fstats["dup"]
                self._runtime.last_skip_noseller = fstats["no_seller"]
                self._runtime.last_skip_level = fstats["level"]
                self._runtime.last_skip_premium = fstats["premium"]
                self._runtime.last_skip_pro = fstats["pro"]
                if not to_post:
                    if lot.seller_key:
                        self._seen[lot.id] = now
                    continue
                lot = to_post[0]
                await self._sender.send(lot)
                self._seen[lot.id] = now
                key = lot.seller_key
                if key:
                    self._seen_sellers[key] = now
                self._state["seen_sellers"] = self._seen_sellers
                save_state(self._state_path, self._state)
                self._runtime.posted_total += 1
                if is_female_rich(lot):
                    self._runtime.posted_female_rich += 1
                self._runtime.queue_pending = self.pending
                logger.info(
                    "Отправил: %s за %s⭐ (%s) · очередь %s · lvl %s",
                    lot.title,
                    int(lot.stars),
                    lot_slug(lot),
                    self.pending,
                    format_account_level(lot),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Не отправилось (%s): %s", getattr(lot, "id", "?"), exc)
            finally:
                self._pq.task_done()


TRACKER_VERSION = "3.6"


@dataclass
class TrackerRuntime:
    """Статистика для /status и логов."""

    passes: int = 0
    posted_total: int = 0
    posted_female_rich: int = 0
    last_fresh: int = 0
    last_posted: int = 0
    last_skip_ru: int = 0
    last_skip_dm: int = 0
    last_skip_dup: int = 0
    last_skip_noseller: int = 0
    last_skip_level: int = 0
    last_skip_premium: int = 0
    last_skip_pro: int = 0
    scan_parallel: int = 8
    seen_lots: int = 0
    queue_pending: int = 0
    collections_total: int = 0
    last_scan_batch: int = 0
    last_scan_parsed: int = 0
    last_scan_errors: int = 0
    last_scan_elapsed: float = 0.0
    last_api_error: str = ""
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


async def scanner_loop(
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    cfg: Config,
    post_queue: PostQueue,
    runtime: TrackerRuntime,
    state_path: Path,
    state: dict,
) -> None:
    """Скан коллекций — лот в очередь сразу при нахождении (не ждём весь batch)."""
    catalog_refreshed = time.monotonic()
    pass_no = 0

    def _on_fresh(lot: Lot) -> None:
        post_queue.enqueue([lot])

    while True:
        started = time.monotonic()
        pass_no += 1
        runtime.passes = pass_no
        runtime.seen_lots = len(seen)
        try:
            fresh, scan = await poll_once(
                m,
                gift_ids,
                seen,
                cfg,
                baseline=False,
                on_lot=_on_fresh,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Проход упал: %s", exc)
            await asyncio.sleep(2)
            continue

        runtime.last_scan_batch = int(scan.get("batch_size", 0))
        runtime.last_scan_parsed = int(scan.get("parsed", 0))
        runtime.last_scan_errors = int(scan.get("errors", 0))
        runtime.last_scan_elapsed = float(scan.get("elapsed", 0))
        if runtime.last_scan_errors:
            runtime.last_api_error = m.last_error or ""

        runtime.last_fresh = len(fresh)
        if fresh:
            runtime.queue_pending = post_queue.pending
            runtime.last_posted = len(fresh)
            logger.info(
                "Проход #%s: +%s свежих → очередь %s (ждут %s · %ss)",
                pass_no,
                len(fresh),
                post_queue.pending,
                post_queue.pending,
                scan.get("elapsed", "?"),
            )
        elif pass_no % 10 == 0:
            logger.info(
                "Проход #%s: скан %s/%s колл · %s лотов · +0 "
                "(err=%s · %ss)",
                pass_no,
                scan.get("batch_size", "?"),
                scan.get("collections_total", "?"),
                scan.get("parsed", 0),
                scan.get("errors", 0),
                scan.get("elapsed", "?"),
            )

        save_state(state_path, state)

        if time.monotonic() - catalog_refreshed > 600:
            try:
                gift_ids[:] = await m.load_collections(force=True)
                runtime.collections_total = len(gift_ids)
                catalog_refreshed = time.monotonic()
            except Exception:  # noqa: BLE001
                pass

        spent = time.monotonic() - started
        scanned = int(scan.get("scanned", 0) or 0)
        errors = int(scan.get("errors", 0) or 0)
        if scanned > 0:
            ratio = errors / scanned
            if ratio > 0.35 and runtime.scan_parallel > 4:
                runtime.scan_parallel -= 1
                cfg.parallel = runtime.scan_parallel
                logger.warning(
                    "Много ошибок API (%s/%s) — parallel=%s",
                    errors,
                    scanned,
                    runtime.scan_parallel,
                )
            elif ratio < 0.12 and runtime.scan_parallel < 6:
                runtime.scan_parallel += 1
                cfg.parallel = runtime.scan_parallel

        await asyncio.sleep(max(cfg.poll_interval - spent, 0.02))


async def run() -> None:
    _load_dotenv()
    cfg = Config.from_env()
    _singleton_lock = acquire_singleton_lock()
    store = ChannelStore(channel_file_path(data_dir()))
    control_bot: ControlBot | None = None

    client, control_bot = await _get_client(cfg, store)
    me = await client.get_me()
    logger.info(
        "✅ Трекер v%s · %s · RU=%s · персоны · mix≈%s%% жен · lvl≤%s · parallel≤%s",
        TRACKER_VERSION,
        me.username or me.first_name,
        "да" if cfg.strict_ru else "нет",
        int(cfg.female_mix_target * 100),
        cfg.max_account_level,
        cfg.parallel,
    )
    logger.warning(
        "Сессия: один инстанс Bothost; не жми /start повторно; "
        "не запускай parser+tracker на одном аккаунте"
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
        "Коллекций: %s · scan page1 hot=%s · все колл/проход · drip %ss · lvl≤%s",
        len(gift_ids),
        cfg.hot_limit,
        int(cfg.post_interval),
        cfg.max_account_level,
    )

    runtime.collections_total = len(gift_ids)

    baseline = not seen and not cfg.post_on_first_run
    if baseline:
        logger.info("Первый запуск: запоминаю текущие лоты без постинга…")
        _, baseline_stats = await poll_once(
            m, gift_ids, seen, cfg, baseline=True
        )
        save_state(state_path, state)
        logger.info(
            "Запомнено %s лотов (скан %s колл · %ss). Слежу за новыми.",
            len(seen),
            baseline_stats.get("scanned", "?"),
            baseline_stats.get("elapsed", "?"),
        )

    scan_task = asyncio.create_task(
        scanner_loop(
            m, gift_ids, seen, cfg, post_queue, runtime, state_path, state
        ),
        name="scanner",
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
