"""Гифт-трекер: только что выставленные лоты ~5000–25000⭐, пост каждые 4 сек."""

from __future__ import annotations

import asyncio
import fcntl
import html
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from bot import ControlBot
from filters import classify_skip, filter_lot, is_girl, seller_keys, skip_stats
from market import Lot, TelegramMarket, format_level

logger = logging.getLogger("tracker")

_esc = html.escape
SEEN_TTL = 7 * 24 * 3600
SELLER_TTL = 90 * 24 * 3600
SKIP_SELLER_TTL = 3 * 3600  # только явные мальчики
STATE_SCHEMA = 9
MIN_SNAPSHOT = 0


def format_lot(lot: Lot, ts: float | None = None) -> str:
    stars = int(lot.stars) if float(lot.stars).is_integer() else lot.stars
    ton = lot.stars * config.TON_RATE
    tz = timezone(timedelta(hours=config.TZ_OFFSET))
    when = datetime.fromtimestamp(ts or time.time(), tz=tz).strftime("%d.%m.%Y %H:%M:%S")
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
    slug = lot.slug or (
        f"{''.join(ch for ch in lot.title if ch.isalnum())}-{lot.number}"
        if lot.number is not None
        else lot.title
    )
    display_name = " ".join(
        x for x in (lot.first_name, lot.last_name) if x
    ).strip() or "—"
    return "\n".join(
        [
            "🎉 <b>НОВЫЙ ЛИСТИНГ</b>",
            "",
            f"🎁 Гифт: <b>{_esc(lot.title)}</b>",
            f"💰 Цена: <b>{stars} Stars / {ton:.2f} TON</b>",
            f"🏷 Модель: <b>{_esc(lot.model) or '—'}</b>",
            f"👤 Продавец: {seller}",
            f"👧 Имя: {_esc(display_name)}",
            f"📊 Level: {format_level(lot)}",
            f"📢 Сообщение: {dm}",
            f"💃 Статус: {status}",
            f'🔗 <a href="{lot.nft_url}">{_esc(slug)}</a>',
            f"🕒 {when}",
        ]
    )


def lot_keyboard_aiogram(lot: Lot):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    second = [InlineKeyboardButton(text="🎁 Открыть лот", url=lot.nft_url)]
    if lot.seller:
        second.append(
            InlineKeyboardButton(text="👤 Продавец", url=f"https://t.me/{lot.seller}")
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✔️ Занять", url=lot.nft_url)],
            second,
        ]
    )


def lot_keyboard_telethon(lot: Lot) -> list[list[Any]]:
    from telethon import Button

    row2 = [Button.url("🎁 Открыть лот", lot.nft_url)]
    if lot.seller:
        row2.append(Button.url("👤 Продавец", f"https://t.me/{lot.seller}"))
    return [[Button.url("✔️ Занять", lot.nft_url)], row2]


class RateLimiter:
    """Строгий интервал между постами."""

    def __init__(self, interval: float, lock_path: Path, ts_path: Path) -> None:
        self.interval = max(0.5, float(interval))
        self._lock_path = lock_path
        self._ts_path = ts_path
        self._async_lock = asyncio.Lock()
        self._handle: Any | None = None

    def _read(self) -> float:
        try:
            raw = self._ts_path.read_text(encoding="utf-8").strip()
            return float(raw) if raw else 0.0
        except (OSError, ValueError):
            return 0.0

    def _write(self, ts: float) -> None:
        self._ts_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._ts_path.with_suffix(".tmp")
        tmp.write_text(f"{ts:.6f}", encoding="utf-8")
        tmp.replace(self._ts_path)

    def _acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        last = self._read()
        if last > 0:
            wait = self.interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        self._handle = handle

    def _release(self) -> None:
        try:
            self._write(time.time())
        finally:
            if self._handle is not None:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                    self._handle.close()
                except OSError:
                    pass
                self._handle = None

    async def gated(self, action: Any) -> Any:
        async with self._async_lock:
            await asyncio.to_thread(self._acquire)
            try:
                return await action()
            finally:
                await asyncio.to_thread(self._release)


class Sender:
    def __init__(
        self,
        client: TelegramClient,
        chat_id: int,
        limiter: RateLimiter,
        *,
        bot: Any | None = None,
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.limiter = limiter
        self.last_via = ""
        self._bot = bot
        self._owns_bot = False
        if self._bot is None:
            token = config.bot_token()
            if token:
                from aiogram import Bot

                self._bot = Bot(token=token)
                self._owns_bot = True

    async def send(self, lot: Lot) -> str:
        text = format_lot(lot)

        async def _do() -> str:
            if self._bot is not None:
                try:
                    from aiogram.types import LinkPreviewOptions

                    await self._bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=lot_keyboard_aiogram(lot),
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                    self.last_via = "bot"
                    return "bot"
                except Exception as exc:  # noqa: BLE001
                    err = str(exc).lower()
                    if not any(
                        x in err
                        for x in (
                            "chat not found",
                            "bot is not a member",
                            "not enough rights",
                            "have no rights",
                            "forbidden",
                        )
                    ):
                        raise
                    logger.warning("Бот не может постить (%s) — шлю от аккаунта", exc)
            await self.client.send_message(
                self.chat_id,
                text,
                parse_mode="html",
                link_preview=False,
                buttons=lot_keyboard_telethon(lot),
            )
            self.last_via = "user"
            return "user"

        return await self.limiter.gated(_do)

    async def close(self) -> None:
        if self._owns_bot and self._bot is not None:
            await self._bot.session.close()


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("seen", {})
            data.setdefault("seen_sellers", {})
            data.setdefault("skip_sellers", {})
            data.setdefault("market_ids", [])
            data.setdefault("heads", {})
            data.setdefault("pages", {})
            schema = 0
            try:
                schema = int(data.get("schema", 0) or 0)
            except (TypeError, ValueError):
                schema = 0
            if schema < STATE_SCHEMA:
                logger.warning(
                    "Схема %s→%s: сброс страниц — старые #1 больше не постим",
                    schema,
                    STATE_SCHEMA,
                )
                data["skip_sellers"] = {}
                data["heads"] = {}
                data["pages"] = {}
                data["schema"] = STATE_SCHEMA
            return data
    except (OSError, ValueError):
        pass
    return {
        "seen": {},
        "seen_sellers": {},
        "skip_sellers": {},
        "market_ids": [],
        "heads": {},
        "pages": {},
        "schema": STATE_SCHEMA,
    }


def save_state(path: Path, state: dict) -> None:
    now = time.time()
    state["schema"] = STATE_SCHEMA
    seen = state.get("seen", {})
    if len(seen) > 200_000:
        state["seen"] = {k: v for k, v in seen.items() if now - float(v) < SEEN_TTL}
    sellers = state.get("seen_sellers", {})
    if len(sellers) > 100_000:
        state["seen_sellers"] = {
            k: v for k, v in sellers.items() if now - float(v) < SELLER_TTL
        }
    skipped = state.get("skip_sellers", {})
    state["skip_sellers"] = {
        k: v for k, v in skipped.items() if now - float(v) < SKIP_SELLER_TTL
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def load_session() -> str:
    env = (os.environ.get("SESSION_STRING") or "").strip()
    if env:
        return env
    path = config.session_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def acquire_lock() -> Any:
    path = config.data_dir() / "tracker_singleton.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SystemExit(
            "Уже запущен другой трекер. На Bothost нужен один инстанс."
        ) from exc
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


class Runtime:
    def __init__(self) -> None:
        self.passes = 0
        self.posted = 0
        self.queue = 0
        self.last_fresh = 0
        self.last_found = 0
        self.last_skip: dict[str, int] = skip_stats()
        self.skip_total: dict[str, int] = skip_stats()
        self.snapshot = 0
        self.collections = 0
        self.last_error = ""
        self.post_via = ""
        self.snapshot_ready = False


async def _client_and_bot() -> tuple[TelegramClient, ControlBot]:
    session = load_session()
    client = TelegramClient(
        StringSession(session) if session else StringSession(),
        config.api_id(),
        config.api_hash(),
    )
    await client.connect()
    authorized = False
    if session:
        try:
            authorized = bool(
                await asyncio.wait_for(client.is_user_authorized(), timeout=12.0)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("is_user_authorized: %s", exc)
            authorized = False
        if not authorized:
            await client.disconnect()
            logger.warning("Сессия недействительна — нужен /start")
            client = TelegramClient(StringSession(), config.api_id(), config.api_hash())
            await client.connect()
    bot = ControlBot(client, str(config.session_path()))
    if authorized:
        name = "аккаунт"
        try:
            me = await asyncio.wait_for(client.get_me(), timeout=8.0)
            name = me.username or me.first_name or str(me.id)
        except Exception:  # noqa: BLE001
            pass
        bot.mark_authorized(str(name))
    await bot.start()
    if not bot.authorized:
        logger.warning("Нет сессии — войди через @%s /start", config.BOT_USERNAME)
        await bot.wait_login()
    return client, bot


class PostQueue:
    def __init__(
        self,
        market: TelegramMarket,
        sender: Sender,
        seen: dict[str, float],
        seen_sellers: dict[str, float],
        state: dict,
        state_file: Path,
        runtime: Runtime,
        market_ids: set[str] | None = None,
    ) -> None:
        self.market = market
        self.sender = sender
        self.seen = seen
        self.seen_sellers = seen_sellers
        self.skip_sellers = state.setdefault("skip_sellers", {})
        self.state = state
        self.state_file = state_file
        self.runtime = runtime
        self.market_ids = market_ids if market_ids is not None else set()
        self._items: list[Lot] = []
        self._queued: set[str] = set()
        self._inflight: set[str] = set()
        self._inflight_sellers: set[str] = set()
        self._task: asyncio.Task | None = None
        self._retries: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._stop = False
        self._last_title = ""

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker(), name="post-drip")

    async def stop(self) -> None:
        self._stop = True
        self._event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    def _blocked_sellers(self) -> set[str]:
        now = time.time()
        blocked = {
            k
            for k, ts in self.seen_sellers.items()
            if now - float(ts) < SELLER_TTL
        }
        blocked |= self._inflight_sellers
        for lot in self._items:
            blocked |= seller_keys(lot)
        return blocked

    def enqueue(self, lots: list[Lot]) -> int:
        added = 0
        now = time.time()
        blocked = self._blocked_sellers()
        incoming = list(lots)
        batch_owners: set[str] = set()
        for lot in incoming:
            if (
                lot.id in self.seen
                or lot.id in self._queued
                or lot.id in self._inflight
                or (lot.slug and lot.slug in self.seen)
            ):
                continue
            keys = seller_keys(lot)
            if keys and (keys & blocked or keys & batch_owners):
                # продавец уже в очереди или только что опубликован —
                # НЕ пишем в seen, чтобы лот подхватился на следующем проходе
                logger.info(
                    "[pipeline] seller-dup skip %s · seller=%s",
                    lot.slug or lot.id,
                    (lot.seller or str(lot.seller_id) or "?")[:24],
                )
                classify_skip("дубль", self.runtime.skip_total)
                continue
            self._queued.add(lot.id)
            self._items.append(lot)
            batch_owners |= keys
            added += 1
            logger.info(
                "[pipeline] queued %s · %s⭐ · q=%s",
                lot.slug or lot.id,
                int(lot.stars),
                len(self._items),
            )
        if added:
            self._event.set()
        self.runtime.queue = len(self._items)
        return added

    def _pick(self) -> Lot | None:
        if not self._items:
            return None
        last = self._last_title
        pool = [x for x in self._items if (x.title or "") != last] if last else list(self._items)
        if not pool:
            pool = list(self._items)
        girls = [x for x in pool if is_girl(x)]
        cand = girls or pool
        lot = max(cand, key=lambda x: float(getattr(x, "discovered_at", 0) or 0))
        self._items.remove(lot)
        self._queued.discard(lot.id)
        self._inflight.add(lot.id)
        self._inflight_sellers = seller_keys(lot)
        return lot

    async def _worker(self) -> None:
        logger.info("Очередь: только что выставленные · пауза %s сек · без повтора владельца", int(config.POST_INTERVAL))
        while not self._stop:
            async with self._lock:
                lot = self._pick()
            if lot is None:
                self._event.clear()
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                tag = lot.slug or lot.id
                pre = filter_lot(
                    lot,
                    min_stars=config.MIN_STARS,
                    max_stars=config.MAX_STARS,
                    max_level=config.MAX_ACCOUNT_LEVEL,
                    max_nfts=config.MAX_NFTS,
                )
                logger.info(
                    "[pipeline] pre-filter %s: %s · name=%s seller=%s",
                    tag,
                    pre or "ok",
                    (lot.first_name or "—")[:24],
                    (lot.seller or "?")[:24],
                )
                hard = {
                    "мужской",
                    "цена",
                    "платные ЛС",
                    "level",
                    "много NFT",
                    "дубль",
                }
                if pre in hard:
                    now = time.time()
                    keys = seller_keys(lot)
                    stats = skip_stats()
                    classify_skip(pre, stats)
                    classify_skip(pre, self.runtime.skip_total)
                    self.runtime.last_skip = stats
                    logger.info(
                        "[pipeline] skip %s без RPC: %s · %s",
                        tag,
                        pre,
                        (lot.first_name or lot.seller or "?")[:24],
                    )
                    self.seen[lot.id] = now
                    self.market_ids.add(lot.id)
                    self.state["market_ids"] = list(self.market_ids)
                    save_state(self.state_file, self.state)
                    continue
                await self.market.enrich_lot(lot, timeout=config.ENRICH_TIMEOUT)
                logger.info(
                    "[pipeline] enriched %s · name=%s seller=%s lvl=%s dm=%s nfts=%s ru=%s",
                    tag,
                    (lot.first_name or "—")[:24],
                    (lot.seller or "?")[:24],
                    lot.account_level if lot.account_level is not None else "none",
                    lot.free_dm if lot.free_dm is not None else "none",
                    lot.gifts_count if lot.gifts_count is not None else "none",
                    (lot.lang_code or "—")[:8],
                )
                now = time.time()
                keys = seller_keys(lot)
                reason = filter_lot(
                    lot,
                    min_stars=config.MIN_STARS,
                    max_stars=config.MAX_STARS,
                    max_level=config.MAX_ACCOUNT_LEVEL,
                    max_nfts=config.MAX_NFTS,
                )
                logger.info(
                    "[pipeline] final-filter %s: %s · lvl=%s",
                    tag,
                    reason or "ok",
                    lot.account_level if lot.account_level is not None else "none",
                )
                if not reason:
                    if lot.seller_id is None:
                        reason = "нет продавца"
                    else:
                        blocked = {
                            k
                            for k, ts in self.seen_sellers.items()
                            if now - float(ts) < SELLER_TTL
                        }
                        if keys & blocked:
                            reason = "дубль"
                stats = skip_stats()
                if reason:
                    classify_skip(reason, stats)
                    classify_skip(reason, self.runtime.skip_total)
                    self.runtime.last_skip = stats
                    logger.info(
                        "[pipeline] skip %s (%s⭐ @%s): %s · %s · lvl=%s gifts=%s",
                        lot.slug or lot.id,
                        int(lot.stars),
                        lot.seller or "?",
                        reason,
                        (lot.first_name or "—")[:24],
                        lot.account_level if lot.account_level is not None else "—",
                        lot.gifts_count if lot.gifts_count is not None else "—",
                    )
                    incomplete = reason in {
                        "нет данных",
                        "нет продавца",
                    }
                    if incomplete:
                        logger.info(
                            "Мало данных %s — не сжигаю, следующий проход подхватит",
                            lot.slug or lot.id,
                        )
                        continue
                    self.seen[lot.id] = now
                    if lot.slug:
                        self.seen[lot.slug] = now
                    self.market_ids.add(lot.id)
                    self._retries.pop(lot.id, None)
                    self.state["market_ids"] = list(self.market_ids)
                    save_state(self.state_file, self.state)
                    continue
                logger.info("[pipeline] send %s …", lot.slug or lot.id)
                via = await self.sender.send(lot)
                self.runtime.post_via = via
                self.seen[lot.id] = now
                if lot.slug:
                    self.seen[lot.slug] = now
                self.market_ids.add(lot.id)
                for k in keys:
                    self.seen_sellers[k] = now
                self.state["seen_sellers"] = self.seen_sellers
                self.state["market_ids"] = list(self.market_ids)
                save_state(self.state_file, self.state)
                self.runtime.posted += 1
                self._last_title = lot.title or ""
                logger.info(
                    "[pipeline] send ok %s за %s⭐ · lvl %s · via=%s · очередь %s",
                    lot.title,
                    int(lot.stars),
                    format_level(lot),
                    via,
                    len(self._items),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка поста %s: %s", lot.id, exc)
                self.runtime.last_error = str(exc)
            finally:
                self._inflight.discard(lot.id)
                self._inflight_sellers = set()
                self.runtime.queue = len(self._items)


def fresh_from_page(
    prev_ids: list[str] | None,
    lots: list[Lot],
    seen: dict[str, float] | set[str],
    min_stars: float,
    max_stars: float,
) -> tuple[list[str], list[Lot]]:
    """Новые id = разность с предыдущей страницей и seen.

    Порядок ответа API не обязан быть newest→oldest: известный id
    больше не обрывает страницу. Всплытие старого #2 после покупки #1
    не постим — id уже был в prev_ids. Первый снимок (prev пустой) — без постов.
    """
    page = [lot.id for lot in lots]
    if not lots or not prev_ids:
        return page, []
    known = set(prev_ids)
    out: list[Lot] = []
    for lot in lots:
        if lot.id in known:
            continue
        if lot.id in seen or (lot.slug and lot.slug in seen):
            continue
        if min_stars <= float(lot.stars) <= max_stars:
            out.append(lot)
    return page, out


async def sync_pages(
    market: TelegramMarket,
    gift_ids: list[int],
    pages: dict[str, list[str]],
    runtime: Runtime,
) -> None:
    """Запомнить верх newest каждой коллекции. В канал не постим."""
    ids = list(gift_ids)
    random.shuffle(ids)
    logger.info("Синхрон страниц · %s коллекций — дальше только новые id сверху", len(ids))
    parallel = max(2, int(config.SCAN_PARALLEL))
    sem = asyncio.Semaphore(parallel)

    async def one(gid: int) -> tuple[int, list[Lot]]:
        async with sem:
            try:
                lots = await market.fetch_page(
                    gid,
                    limit=config.PAGE_LIMIT,
                    timeout=config.REQUEST_TIMEOUT,
                    gap=config.REQUEST_GAP,
                    sort_by_price=False,
                )
            except Exception as exc:  # noqa: BLE001
                runtime.last_error = str(exc)
                lots = []
            return gid, lots

    chunk = max(parallel * 4, 16)
    for i in range(0, len(ids), chunk):
        batch = ids[i : i + chunk]
        parts = await asyncio.gather(*[one(g) for g in batch], return_exceptions=True)
        for part in parts:
            if not isinstance(part, tuple):
                runtime.last_error = str(part)
                continue
            gid, lots = part
            if lots:
                pages[str(gid)] = [lot.id for lot in lots]
        runtime.snapshot = len(pages)
        logger.info("Страницы %s/%s · %s", min(i + len(batch), len(ids)), len(ids), len(pages))
    logger.info("Страницы готовы: %s. Жду только что выставленные", len(pages))


async def scanner_loop(
    market: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    pages: dict[str, list[str]],
    queue: PostQueue,
    runtime: Runtime,
    state: dict,
    state_file: Path,
    bot: Any | None = None,
) -> None:
    logger.info(
        "Сканер: новые id сверху · %s–%s⭐ · русские девочки · ≤%s дорогих NFT · lvl≤%s · free ЛС · пост/%sс",
        config.MIN_STARS,
        config.MAX_STARS,
        config.MAX_NFTS,
        config.MAX_ACCOUNT_LEVEL,
        int(config.POST_INTERVAL),
    )
    pass_no = 0
    while True:
        started = time.monotonic()
        pass_no += 1
        runtime.passes = pass_no
        batch = market.next_batch(config.SCAN_BATCH)
        if not batch or len(gift_ids) < config.MIN_COLLECTIONS:
            try:
                fresh_ids = await market.load_collections(force=True, bot=bot)
                if fresh_ids:
                    gift_ids[:] = list(fresh_ids)
                    runtime.collections = len(gift_ids)
                if market.last_error and len(gift_ids) < config.MIN_COLLECTIONS:
                    runtime.last_error = market.last_error
            except Exception as exc:  # noqa: BLE001
                logger.error("коллекции: %s", exc)
                runtime.last_error = str(exc)
            if not gift_ids:
                await asyncio.sleep(5)
                continue
            batch = market.next_batch(config.SCAN_BATCH)
            if not batch:
                await asyncio.sleep(5)
                continue
        sem = asyncio.Semaphore(config.SCAN_PARALLEL)

        async def one(gid: int) -> tuple[int, list[Lot]]:
            async with sem:
                try:
                    lots = await market.fetch_page(
                        gid,
                        limit=config.PAGE_LIMIT,
                        timeout=config.REQUEST_TIMEOUT,
                        gap=config.REQUEST_GAP,
                        sort_by_price=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    runtime.last_error = str(exc)
                    lots = []
                return gid, lots

        found = 0
        queued = 0
        api_n = 0

        seen_skip = 0
        seller_skip = 0

        def absorb(gid: int, lots: list[Lot]) -> None:
            nonlocal found, queued, api_n, seen_skip, seller_skip
            api_n += len(lots)
            key = str(gid)
            prev = pages.get(key)
            new_page, chunk = fresh_from_page(
                prev,
                lots,
                seen,
                config.MIN_STARS,
                config.MAX_STARS,
            )
            # сколько отфильтровал fresh_from_page по seen/цене
            seen_skip += len([
                lot for lot in lots
                if lot.id not in set(prev or [])
                and (
                    lot.id in seen
                    or (lot.slug and lot.slug in seen)
                )
            ])
            if new_page:
                pages[key] = new_page
            if chunk:
                slugs = ",".join((x.slug or x.id)[:22] for x in chunk[:8])
                logger.info(
                    "[pipeline] fresh detected gid=%s n=%s %s",
                    gid,
                    len(chunk),
                    slugs,
                )
            if not chunk:
                return
            found += len(chunk)
            before = len(queue._items)
            enqueued = queue.enqueue(chunk)
            seller_skip += len(chunk) - enqueued
            queued += enqueued

        tasks = [asyncio.create_task(one(g)) for g in batch]
        for fut in asyncio.as_completed(tasks):
            try:
                gid, lots = await fut
            except Exception as exc:  # noqa: BLE001
                runtime.last_error = str(exc)
                continue
            absorb(gid, lots)
        runtime.last_found = found
        runtime.last_fresh = queued
        runtime.snapshot = len(pages)
        logger.info(
            "[pipeline] pass #%s API=%s fresh=%s seen_skip=%s seller_dup=%s queued=%s pages=%s q=%s skip=%s",
            pass_no,
            api_n,
            found,
            seen_skip,
            seller_skip,
            queued,
            len(pages),
            len(queue._items),
            dict(queue.runtime.skip_total),
        )
        if queued:
            logger.info("⚡ выставили %s → очередь %s", queued, len(queue._items))
        elif pass_no % 16 == 0:
            logger.info(
                "Проход #%s: %s колл · страниц %s · новых 0",
                pass_no,
                len(batch),
                len(pages),
            )
        state["pages"] = pages
        save_state(state_file, state)
        spent = time.monotonic() - started
        await asyncio.sleep(max(config.POLL_INTERVAL - spent, 0.02))


async def run() -> None:
    _lock = acquire_lock()
    state_file = config.state_path()
    state = load_state(state_file)
    seen: dict[str, float] = state["seen"]
    seen_sellers: dict[str, float] = state.setdefault("seen_sellers", {})
    market_ids: set[str] = set(state.get("market_ids") or [])
    pages: dict[str, list[str]] = state.setdefault("pages", {})
    if not isinstance(pages, dict):
        pages = {}
        state["pages"] = pages

    client, control = await _client_and_bot()
    chat_id = config.channel_id()
    runtime = Runtime()
    control.runtime = runtime
    logger.warning(
        "=== Трекер v%s · %s · канал %s · %s–%s⭐ ===",
        config.TRACKER_VERSION,
        control.account_name,
        chat_id,
        config.MIN_STARS,
        config.MAX_STARS,
    )

    dd = config.data_dir()
    limiter = RateLimiter(
        config.POST_INTERVAL,
        dd / "tracker_post.lock",
        dd / "tracker_last_post.txt",
    )
    sender = Sender(client, chat_id, limiter, bot=control.aiogram_bot)
    market = TelegramMarket(client, config.catalog_path())
    gift_ids: list[int] = []
    for attempt in range(8):
        try:
            gift_ids = list(
                await market.load_collections(
                    force=attempt > 0, bot=control.aiogram_bot
                )
            )
        except Exception as exc:  # noqa: BLE001
            runtime.last_error = str(exc)
            logger.error("коллекции (%s): %s", attempt + 1, exc)
        if gift_ids:
            break
        if market.last_error:
            runtime.last_error = market.last_error
        await asyncio.sleep(2.5)
    if len(gift_ids) < config.MIN_COLLECTIONS:
        logger.warning(
            "Коллекций %s — мало, нужно ≥%s. Пробуем ещё раз",
            len(gift_ids),
            config.MIN_COLLECTIONS,
        )
        try:
            gift_ids = list(
                await market.load_collections(force=True, bot=control.aiogram_bot)
            )
        except Exception as exc:  # noqa: BLE001
            runtime.last_error = str(exc)
    runtime.collections = len(gift_ids)
    if not gift_ids:
        logger.error("Коллекций нет — бот жив, сканер будет пробовать снова")
        runtime.last_error = market.last_error or "нет коллекций"
    queue = PostQueue(
        market, sender, seen, seen_sellers, state, state_file, runtime, market_ids
    )
    queue.start()
    control.queue = queue

    if gift_ids:
        try:
            await sync_pages(market, gift_ids, pages, runtime)
            state["pages"] = pages
            save_state(state_file, state)
        except Exception as exc:  # noqa: BLE001
            logger.error("страницы: %s", exc)
            runtime.last_error = str(exc)
    runtime.snapshot_ready = True
    runtime.snapshot = len(pages)

    try:
        while True:
            try:
                await scanner_loop(
                    market,
                    gift_ids,
                    seen,
                    pages,
                    queue,
                    runtime,
                    state,
                    state_file,
                    bot=control.aiogram_bot,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("сканер упал — рестарт")
                runtime.last_error = str(exc)
                await asyncio.sleep(5.0)
    finally:
        await queue.stop()
        await sender.close()
        await control.stop()
        await client.disconnect()
        _lock.close()


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
