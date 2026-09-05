"""Гифт-трекер: только что выставленные лоты ~5000–25000⭐, пост каждые 4 сек."""

from __future__ import annotations

import asyncio
import fcntl
import html
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from bot import ControlBot
from diagnostics import (
    Diagnostics,
    log_girl_forensics,
    log_male_forensics,
    log_ru_forensics,
)
from filters import (
    classify_skip,
    explain_filters,
    female_reason,
    filter_lot,
    is_girl,
    is_russian,
    looks_male,
    passes_free_dm,
    passes_level,
    passes_nfts,
    seller_keys,
    skip_stats,
    canonical_owner_key,
    owner_is_blocked,
)
from floors import (
    listing_price_ok,
    listing_price_range,
    model_floor_verdict,
)
from market import Lot, TelegramMarket, format_level

logger = logging.getLogger("tracker")

_esc = html.escape
SEEN_TTL = 7 * 24 * 3600
SELLER_TTL = 90 * 24 * 3600
SKIP_SELLER_TTL = 3 * 3600  # только явные мальчики
STATE_SCHEMA = 11
MIN_SNAPSHOT = 0
OBSERVED_TTL = 30 * 24 * 3600
OBSERVED_MAX = 400_000
FORENSIC_KEEP = 20
_OWNER_CLAIM_LOCK = threading.Lock()
_STATE_IO_LOCK = threading.Lock()


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
        return await self.limiter.gated(lambda: self.deliver(lot))

    async def deliver(self, lot: Lot) -> str:
        """Пост в канал без RateLimiter. Вызывать только из gated send-guard."""
        text = format_lot(lot)
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

    async def close(self) -> None:
        if self._owns_bot and self._bot is not None:
            await self._bot.session.close()


def _empty_state() -> dict:
    return {
        "seen": {},
        "seen_sellers": {},
        "skip_sellers": {},
        "market_ids": [],
        "heads": {},
        "pages": {},
        "observed": {},
        "primed_models": {},
        "schema": STATE_SCHEMA,
    }


def normalize_observed_record(raw: Any, now: float | None = None) -> dict[str, Any]:
    ts = time.time() if now is None else float(now)
    if isinstance(raw, dict):
        try:
            first = float(raw.get("first") or raw.get("first_seen_at") or ts)
        except (TypeError, ValueError):
            first = ts
        try:
            last = float(raw.get("last") or raw.get("last_seen_at") or first)
        except (TypeError, ValueError):
            last = first
        return {
            "first": first,
            "last": last,
            "c": raw.get("c", raw.get("collection_id")),
            "m": raw.get("m", raw.get("model_id")),
        }
    try:
        prev = float(raw)
    except (TypeError, ValueError):
        prev = ts
    return {"first": prev, "last": prev, "c": None, "m": None}


def seed_observed_id(
    observed: dict[str, Any],
    listing_id: str,
    *,
    now: float | None = None,
    collection_id: int | None = None,
    model_id: int | None = None,
) -> None:
    lid = str(listing_id or "").strip()
    if not lid:
        return
    ts = time.time() if now is None else float(now)
    rec = observed.get(lid)
    if rec is None:
        observed[lid] = {
            "first": ts,
            "last": ts,
            "c": collection_id,
            "m": model_id,
        }
        return
    rec = normalize_observed_record(rec, ts)
    rec["last"] = ts
    if collection_id is not None:
        rec["c"] = collection_id
    if model_id is not None:
        rec["m"] = model_id
    observed[lid] = rec


def migrate_observed_from_legacy(data: dict, *, now: float | None = None) -> None:
    """Перенести listing id из pages/seen в глобальный observed. Не постим их снова."""
    ts = time.time() if now is None else float(now)
    observed = data.setdefault("observed", {})
    if not isinstance(observed, dict):
        observed = {}
        data["observed"] = observed
    for lid, raw in list(observed.items()):
        observed[str(lid)] = normalize_observed_record(raw, ts)
    for lid, raw in (data.get("seen") or {}).items():
        seed_observed_id(observed, str(lid), now=ts)
        try:
            seen_ts = float(raw)
        except (TypeError, ValueError):
            seen_ts = ts
        rec = observed.get(str(lid))
        if rec and seen_ts < float(rec.get("first") or seen_ts):
            rec["first"] = seen_ts
    pages = data.get("pages") or {}
    if isinstance(pages, dict):
        for ids in pages.values():
            if not isinstance(ids, list):
                continue
            for lid in ids:
                seed_observed_id(observed, str(lid), now=ts)
    data.setdefault("primed_models", {})
    if not isinstance(data["primed_models"], dict):
        data["primed_models"] = {}


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
            data.setdefault("observed", {})
            data.setdefault("primed_models", {})
            schema = 0
            try:
                schema = int(data.get("schema", 0) or 0)
            except (TypeError, ValueError):
                schema = 0
            migrate_observed_from_legacy(data)
            if schema < STATE_SCHEMA:
                logger.warning(
                    "Схема %s→%s: pages collection-key → observed; "
                    "старые listing id не станут fresh",
                    schema,
                    STATE_SCHEMA,
                )
                data["skip_sellers"] = {}
                data["heads"] = {}
                # Старые pages были keyed только collection_id — несовместимы
                # с model-chunk запросами. IDs уже в observed.
                data["pages"] = {}
                data["schema"] = STATE_SCHEMA
            return data
    except (OSError, ValueError):
        pass
    return _empty_state()


def _prune_observed(observed: dict[str, Any], now: float) -> dict[str, Any]:
    if not isinstance(observed, dict):
        return {}
    if len(observed) <= OBSERVED_MAX:
        return observed
    cutoff = now - OBSERVED_TTL
    kept: list[tuple[str, dict[str, Any], float]] = []
    for key, raw in observed.items():
        rec = normalize_observed_record(raw, now)
        last = float(rec.get("last") or now)
        if last >= cutoff:
            kept.append((str(key), rec, last))
    kept.sort(key=lambda x: x[2], reverse=True)
    if len(kept) > OBSERVED_MAX:
        kept = kept[:OBSERVED_MAX]
    return {key: rec for key, rec, _last in kept}


def save_state(path: Path, state: dict) -> None:
    now = time.time()
    with _STATE_IO_LOCK:
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
        observed = state.get("observed", {})
        if isinstance(observed, dict) and len(observed) > OBSERVED_MAX:
            state["observed"] = _prune_observed(observed, now)
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


def empty_funnel() -> dict[str, int]:
    """Последовательная воронка: каждый этап — checked / pass / reject.

    Порядок фактического pipeline:
      fresh_detected → price → seen → dup_listing|dup_seller|work_in
      → dequeued → post-enrich dup_seller (terminal)
      → male → ru → girl → dm → level → nft
      → send_attempt → sent|failed

    duplicate_seller на enqueue и post-enrich НЕ входят в ru/girl/dm/level/nft.
    """
    keys = [
        "fresh_detected",
        "price_checked",
        "price_pass",
        "price_reject",
        "seen_checked",
        "seen_pass",
        "seen_reject",
        "reject_seen",
        "reject_price",
        "reject_ru",
        "reject_girl",
        "reject_dm",
        "reject_level",
        "reject_nft",
        "reject_incomplete",
        "reject_duplicate_seller",
        "reject_duplicate_listing",
        "dup_seller",
        "dup_listing",
        "dup_seller_post_enrich",
        "work_in",
        "dequeued",
        "male_checked",
        "male_pass",
        "male_reject",
        "reject_male",
        "ru_checked",
        "ru_pass",
        "ru_reject",
        "girl_checked",
        "girl_pass",
        "girl_reject",
        "dm_checked",
        "dm_pass",
        "dm_reject",
        "level_checked",
        "level_pass",
        "level_reject",
        "nft_checked",
        "nft_pass",
        "nft_reject",
        "send_attempt",
        "sent",
        "failed",
        "worker_started",
        "worker_processed",
        "worker_filtered",
        "worker_failed",
        "flood_wait",
        "queue_remaining",
        # aliases for old status keys (filled in format_funnel_report)
        "fresh",
        "price",
        "ru",
        "girl",
        "dm",
        "level",
        "nft",
        "duplicate",
        "enqueued",
        "owner_dup_enqueue",
        "owner_dup_post_enrich",
        "owner_dup_send_guard",
        "owner_sent_persisted",
        "owner_id_missing",
        "listing_checked",
        "listing_price_pass",
        "listing_price_reject",
        "bad_model_value",
        "model_floor_pass",
        "model_floor_unknown",
        "owner_duplicate",
        "detection_to_enqueue_n",
        "detection_to_send_n",
        "new_listing_seen",
        "old_listing_seen",
        "listing_page_depth",
        "collections_scanned",
        "eligible_collections_scanned",
        "owner_sent_total",
        "owner_duplicate_total",
        "api_observations",
        "unique_listing_ids",
        "duplicate_listing_ids_same_round",
        "duplicate_listing_ids_across_models",
        "duplicate_listing_ids_across_collections",
        "fresh_unique",
        "fresh_repeated",
        "observed_old",
        "observed_duplicate_same_round",
        "observed_duplicate_cross_model",
        "unprimed_seed",
        "genuine_new",
        "genuine_new_listings",
    ]
    return {k: 0 for k in keys}


def _bump(stats: dict[str, int], key: str, n: int = 1) -> None:
    stats[key] = stats.get(key, 0) + n


def record_fresh_price_seen(
    stats: dict[str, int],
    *,
    fresh: bool,
    price_ok: bool | None,
    already_seen: bool,
) -> None:
    """Один GENUINE_NEW listing (не page-diff). Дальше: price → seen."""
    if not fresh:
        return
    _bump(stats, "fresh_detected")
    _bump(stats, "price_checked")
    if price_ok is True:
        _bump(stats, "price_pass")
        _bump(stats, "seen_checked")
        if already_seen:
            _bump(stats, "seen_reject")
            _bump(stats, "reject_seen")
        else:
            _bump(stats, "seen_pass")
    elif price_ok is False:
        _bump(stats, "price_reject")
        _bump(stats, "reject_price")


def persist_sent_owner(
    seen_sellers: dict[str, float], lot: Lot, now: float | None = None
) -> None:
    """Пишет canonical id: + username alias. Не пишет UNKNOWN без id."""
    ts = time.time() if now is None else now
    canon = canonical_owner_key(lot)
    if canon:
        seen_sellers[canon] = ts
    if lot.seller:
        u = lot.seller.lower().lstrip("@").strip()
        if u:
            seen_sellers[f"u:{u}"] = ts


def claim_owner_for_send(
    lot: Lot, seen_sellers: dict[str, float], now: float | None = None
) -> str:
    """Атомарный claim id:<user_id> ДО deliver. '' = можно слать.

    LOCK внутри: reload уже сделан вызывающим. Check+persist неразрывны,
    два worker'а на одном seen_sellers не могут оба получить ''.
    UNKNOWN без seller_id не claim'ится и не сливается с другими UNKNOWN.
    """
    ts = time.time() if now is None else now
    if lot.seller_id is None:
        return "нет продавца"
    with _OWNER_CLAIM_LOCK:
        blocked = {
            k for k, t in seen_sellers.items() if ts - float(t) < SELLER_TTL
        }
        if owner_is_blocked(lot, blocked):
            return "дубль продавца"
        persist_sent_owner(seen_sellers, lot, ts)
        return ""


def reload_seen_sellers(path: Path | None, dest: dict[str, float]) -> None:
    """Подтянуть seen_sellers с диска. Не затирает более новые in-memory ts."""
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    incoming = data.get("seen_sellers") if isinstance(data, dict) else None
    if not isinstance(incoming, dict):
        return
    for key, ts in incoming.items():
        try:
            val = float(ts)
        except (TypeError, ValueError):
            continue
        dest_key = str(key)
        prev = dest.get(dest_key)
        if prev is None or val > prev:
            dest[dest_key] = val


def owner_dup_after_enrich(
    lot: Lot, seen_sellers: dict[str, float], now: float | None = None
) -> str:
    """'' = можно send. Иначе 'нет продавца' / 'дубль продавца'."""
    if lot.seller_id is None:
        return "нет продавца"
    ts = time.time() if now is None else now
    blocked = {
        k for k, t in seen_sellers.items() if ts - float(t) < SELLER_TTL
    }
    if owner_is_blocked(lot, blocked):
        return "дубль продавца"
    return ""


def owner_dup_send_guard(
    lot: Lot, seen_sellers: dict[str, float], now: float | None = None
) -> str:
    """Тот же id-check, но внутри RateLimiter после reload с диска."""
    return owner_dup_after_enrich(lot, seen_sellers, now)


def apply_listing_floor_filters(
    lots: list[Lot],
    catalog: FloorCatalog,
    stats: dict[str, int],
) -> list[Lot]:
    """MODEL FLOOR поверх уже прошедших listing-price fresh лотов.

    UNKNOWN не выдаём за известную цену. Дешёвая модель + дорогой listing
    → REJECT_BAD_MODEL_VALUE.
    """
    kept: list[Lot] = []
    for lot in lots:
        gid = lot.collection_id
        mid = lot.model_id
        floor = (
            catalog.get_floor(gid, mid)
            if gid is not None and mid is not None
            else None
        )
        lot.model_floor = floor
        verdict = model_floor_verdict(floor)
        if verdict == "ok":
            _bump(stats, "model_floor_pass")
            kept.append(lot)
            continue
        if verdict == "bad_model_value":
            _bump(stats, "bad_model_value")
            logger.info(
                "[pipeline] REJECT_BAD_MODEL_VALUE %s · MODEL FLOOR=%s⭐ LISTING=%s⭐",
                lot.slug or lot.id,
                int(floor) if floor is not None else 0,
                int(lot.stars),
            )
            continue
        if verdict == "unknown":
            _bump(stats, "model_floor_unknown")
            logger.info(
                "[pipeline] floor UNKNOWN %s · LISTING=%s⭐",
                lot.slug or lot.id,
                int(lot.stars),
            )
            continue
        logger.info(
            "[pipeline] floor above_max %s · MODEL FLOOR=%s⭐ LISTING=%s⭐",
            lot.slug or lot.id,
            int(floor) if floor is not None else 0,
            int(lot.stars),
        )
    return kept


def record_enqueue_dup(
    stats: dict[str, int],
    kind: str,
    diag: Diagnostics | None = None,
    *,
    seen_kind: str | None = None,
) -> None:
    """kind: listing | seller. Только на enqueue, до worker-фильтров.

    diag/seen_kind — только диагностика, воронка не меняется.
    """
    if kind == "listing":
        _bump(stats, "dup_listing")
        _bump(stats, "reject_duplicate_listing")
        if diag is not None:
            diag.record_seen_reason(seen_kind or "other")
    elif kind == "seller":
        _bump(stats, "dup_seller")
        _bump(stats, "reject_duplicate_seller")
        _bump(stats, "owner_dup_enqueue")
        _bump(stats, "owner_duplicate")
        _bump(stats, "owner_duplicate_total")
        if diag is not None:
            diag.record_seen_reason("owner")
            diag.note_owner_dup_stage("enqueue")


def record_work_in(stats: dict[str, int]) -> None:
    _bump(stats, "work_in")


def record_worker_filter(stats: dict[str, int], lot: Lot, reason: str) -> None:
    """Один раз на dequeued лот.

    Порядок:
      1) post-enrich seller dup → terminal (НЕ ru/girl/dm/level/nft)
      2) male → male stage (НЕ ru/girl)
      3) ru → girl → dm → level → nft
    Unknown dm/level/nft (None) = pass.
    """
    # BUG#1 fix: post-enrich seller dup — отдельная terminal ветка до фильтров
    if reason in {"дубль", "дубль продавца"}:
        _bump(stats, "dup_seller")
        _bump(stats, "reject_duplicate_seller")
        _bump(stats, "dup_seller_post_enrich")
        _bump(stats, "owner_dup_post_enrich")
        _bump(stats, "owner_duplicate")
        _bump(stats, "owner_duplicate_total")
        return

    if reason == "REJECT_BAD_MODEL_VALUE":
        _bump(stats, "bad_model_value")
        return
    if reason == "floor неизвестен":
        _bump(stats, "model_floor_unknown")
        return
    if reason == "floor выше макс":
        return

    # BUG#2 fix: male — отдельная стадия, не girl / не ru
    _bump(stats, "male_checked")
    if looks_male(lot):
        _bump(stats, "male_reject")
        _bump(stats, "reject_male")
        return
    _bump(stats, "male_pass")

    _bump(stats, "ru_checked")
    ru = is_russian(lot)
    if ru is False:
        _bump(stats, "ru_reject")
        _bump(stats, "reject_ru")
        return
    if ru is None:
        _bump(stats, "reject_incomplete")
        return
    _bump(stats, "ru_pass")

    _bump(stats, "girl_checked")
    girl = female_reason(lot)
    if girl:
        _bump(stats, "girl_reject")
        _bump(stats, "reject_girl")
        return
    _bump(stats, "girl_pass")

    _bump(stats, "dm_checked")
    dm = passes_free_dm(lot)
    if dm is False:
        _bump(stats, "dm_reject")
        _bump(stats, "reject_dm")
        return
    _bump(stats, "dm_pass")

    _bump(stats, "level_checked")
    lvl = passes_level(lot, config.MAX_ACCOUNT_LEVEL)
    if lvl is False:
        _bump(stats, "level_reject")
        _bump(stats, "reject_level")
        return
    _bump(stats, "level_pass")

    _bump(stats, "nft_checked")
    nfts = passes_nfts(lot, config.MAX_NFTS)
    if nfts is False:
        _bump(stats, "nft_reject")
        _bump(stats, "reject_nft")
        return
    _bump(stats, "nft_pass")

    if reason:
        return
    # готов к send — send_attempt/sent считает worker отдельно


def funnel_invariants(stats: dict[str, int]) -> list[str]:
    """Мягкие проверки: вернуть список нарушений (пусто = ок)."""
    errors: list[str] = []

    def chk(name: str, checked: int, passed: int, rejected: int, *, exact: bool) -> None:
        if passed > checked:
            errors.append(f"{name}: pass({passed}) > checked({checked})")
        if rejected > checked:
            errors.append(f"{name}: reject({rejected}) > checked({checked})")
        if exact and passed + rejected != checked:
            errors.append(
                f"{name}: pass+reject({passed + rejected}) != checked({checked})"
            )

    chk(
        "price",
        stats.get("price_checked", 0),
        stats.get("price_pass", 0),
        stats.get("price_reject", 0),
        exact=True,
    )
    chk(
        "seen",
        stats.get("seen_checked", 0),
        stats.get("seen_pass", 0),
        stats.get("seen_reject", 0),
        exact=True,
    )
    chk(
        "male",
        stats.get("male_checked", 0),
        stats.get("male_pass", 0),
        stats.get("male_reject", 0),
        exact=True,
    )
    # ru: reject_incomplete тоже «уходит» из checked без ru_pass/ru_reject
    ru_c = stats.get("ru_checked", 0)
    ru_p = stats.get("ru_pass", 0)
    ru_r = stats.get("ru_reject", 0)
    inc = stats.get("reject_incomplete", 0)
    if ru_p + ru_r + inc != ru_c:
        errors.append(
            f"ru: pass+reject+incomplete({ru_p + ru_r + inc}) != checked({ru_c})"
        )
    for name in ("girl", "dm", "level", "nft"):
        chk(
            name,
            stats.get(f"{name}_checked", 0),
            stats.get(f"{name}_pass", 0),
            stats.get(f"{name}_reject", 0),
            exact=True,
        )
    # girl.checked == ru.pass; ru.checked == male.pass
    if stats.get("girl_checked", 0) != stats.get("ru_pass", 0):
        errors.append(
            f"girl.checked({stats.get('girl_checked', 0)}) != ru.pass({stats.get('ru_pass', 0)})"
        )
    if stats.get("ru_checked", 0) != stats.get("male_pass", 0):
        errors.append(
            f"ru.checked({stats.get('ru_checked', 0)}) != male.pass({stats.get('male_pass', 0)})"
        )
    # post-enrich dup не должен раздувать nft_pass относительно send
    # (проверяется тестами; здесь только согласованность stages)
    return errors


def format_funnel_report(stats: dict[str, int]) -> str:
    """Человекочитаемый отчёт последовательной воронки."""
    lines = [
        "PIPELINE",
        f"GENUINE_NEW_LISTINGS: {stats.get('genuine_new_listings', stats.get('genuine_new', 0))}",
        f"api_observations: {stats.get('api_observations', 0)}",
        f"unique_listing_ids: {stats.get('unique_listing_ids', 0)}",
        f"observed_old: {stats.get('observed_old', 0)}",
        f"unprimed_seed: {stats.get('unprimed_seed', 0)}",
        f"fresh_detected: {stats.get('fresh_detected', 0)}",
        "",
        "price:",
        f"  checked: {stats.get('price_checked', 0)}",
        f"  passed: {stats.get('price_pass', 0)}",
        f"  rejected: {stats.get('price_reject', 0)}",
        "",
        "seen:",
        f"  checked: {stats.get('seen_checked', 0)}",
        f"  passed: {stats.get('seen_pass', 0)}",
        f"  rejected: {stats.get('seen_reject', 0)}",
        "",
        "duplicates:",
        f"  seller: {stats.get('dup_seller', 0)}",
        f"  listing: {stats.get('dup_listing', 0)}",
        f"  work_in: {stats.get('work_in', 0)}",
        f"  dequeued: {stats.get('dequeued', 0)}",
        f"  post_enrich_seller: {stats.get('dup_seller_post_enrich', 0)}",
        "",
        "male:",
        f"  checked: {stats.get('male_checked', 0)}",
        f"  passed: {stats.get('male_pass', 0)}",
        f"  rejected: {stats.get('male_reject', 0)}",
        "",
        "ru:",
        f"  checked: {stats.get('ru_checked', 0)}",
        f"  passed: {stats.get('ru_pass', 0)}",
        f"  rejected: {stats.get('ru_reject', 0)}",
        f"  incomplete: {stats.get('reject_incomplete', 0)}",
        "",
        "girl:",
        f"  checked: {stats.get('girl_checked', 0)}",
        f"  passed: {stats.get('girl_pass', 0)}",
        f"  rejected: {stats.get('girl_reject', 0)}",
        "",
        "dm:",
        f"  checked: {stats.get('dm_checked', 0)}",
        f"  passed: {stats.get('dm_pass', 0)}",
        f"  rejected: {stats.get('dm_reject', 0)}",
        "",
        "level:",
        f"  checked: {stats.get('level_checked', 0)}",
        f"  passed: {stats.get('level_pass', 0)}",
        f"  rejected: {stats.get('level_reject', 0)}",
        "",
        "nft:",
        f"  checked: {stats.get('nft_checked', 0)}",
        f"  passed: {stats.get('nft_pass', 0)}",
        f"  rejected: {stats.get('nft_reject', 0)}",
        "",
        f"send_attempt: {stats.get('send_attempt', 0)}",
        f"sent: {stats.get('sent', 0)}",
        f"failed: {stats.get('failed', 0)}",
    ]
    return "\n".join(lines)


def sync_funnel_aliases(stats: dict[str, int]) -> None:
    """Короткие ключи для /status одной строкой."""
    stats["fresh"] = stats.get("fresh_detected", 0)
    stats["genuine_new_listings"] = stats.get("genuine_new", 0)
    stats["price"] = stats.get("price_pass", 0)
    stats["ru"] = stats.get("ru_pass", 0)
    stats["girl"] = stats.get("girl_pass", 0)
    stats["dm"] = stats.get("dm_pass", 0)
    stats["level"] = stats.get("level_pass", 0)
    stats["nft"] = stats.get("nft_pass", 0)
    stats["duplicate"] = stats.get("dup_seller", 0) + stats.get("dup_listing", 0)
    stats["enqueued"] = stats.get("nft_pass", 0)  # прошли все фильтры → к send
    stats["owner_sent_total"] = stats.get("owner_sent_persisted", 0)
    stats["owner_duplicate_total"] = stats.get("owner_duplicate", 0)


def debug_lot_line(lot: Lot, stage: str, reason: str) -> str:
    exp = explain_filters(
        lot,
        min_stars=config.MIN_STARS,
        max_stars=config.MAX_STARS,
        max_level=config.MAX_ACCOUNT_LEVEL,
        max_nfts=config.MAX_NFTS,
    )
    bio = (lot.about or "").replace("\n", " ")[:80]
    return (
        f"[DEBUG] {stage} {lot.slug or lot.id} gift_id={lot.id} "
        f"price={int(lot.stars)} seller_id={lot.seller_id} "
        f"user=@{lot.seller or '—'} first={lot.first_name!r} "
        f"last={lot.last_name!r} bio={bio!r} lang={lot.lang_code!r} "
        f"lvl={lot.account_level} dm={lot.free_dm} nfts={lot.gifts_count} | "
        f"price={'ok' if exp['price'] else 'fail'} "
        f"ru={exp['ru']} ({exp['ru_why']}) "
        f"girl={exp['girl']} male={exp['male']} "
        f"dm={exp['dm']} level={exp['level']} nfts={exp['nfts']} | "
        f"skip={reason or 'ok'}"
    )


def _record_filter_diagnostics(diag: Diagnostics, lot: Lot, reason: str) -> None:
    """Логи/счётчики причин. Не влияет на решения фильтра."""
    if reason in {"дубль", "дубль продавца"}:
        diag.record_seen_reason("owner")
        return
    if reason in {"REJECT_BAD_MODEL_VALUE", "floor неизвестен", "floor выше макс"}:
        return
    if reason == "цена":
        diag.record_price_reject(float(lot.stars))
        return

    # Same stage order as record_worker_filter / filter_lot. Counters only.
    if looks_male(lot) or reason == "мужской":
        log_male_forensics(lot, rejected=True)
        diag.record_male_reject(lot)
        return
    log_male_forensics(lot, rejected=False)
    ru = is_russian(lot)
    if ru is False or reason == "не русский":
        log_ru_forensics(lot, passed=False)
        diag.record_ru_reject(lot)
        return
    if ru is None or reason == "нет данных":
        log_ru_forensics(lot, passed=None)
        return
    log_ru_forensics(lot, passed=True)
    girl_r = female_reason(lot)
    if girl_r or reason == "нет женских признаков":
        log_girl_forensics(lot, passed=False)
        diag.record_girl_outcome(lot, passed=False)
        return
    log_girl_forensics(lot, passed=True)
    diag.record_girl_outcome(lot, passed=True)

    dm = passes_free_dm(lot)
    if dm is False or reason == "платные ЛС":
        diag.record_dm_reject()
        return
    lvl = passes_level(lot, config.MAX_ACCOUNT_LEVEL)
    if lvl is False or reason == "level":
        diag.record_level_outcome(lot, rejected=True)
        return
    # Observational: unknown level still passes the filter.
    diag.record_level_outcome(lot, rejected=False)
    nfts = passes_nfts(lot, config.MAX_NFTS)
    if nfts is False or reason == "много NFT":
        diag.record_nft_reject(lot)


class Runtime:
    def __init__(self) -> None:
        self.passes = 0
        self.posted = 0
        self.queue = 0
        self.last_fresh = 0
        self.last_found = 0
        self.last_skip: dict[str, int] = skip_stats()
        self.skip_total: dict[str, int] = skip_stats()
        self.funnel: dict[str, int] = empty_funnel()
        self.last_funnel: dict[str, int] = empty_funnel()
        self.snapshot = 0
        self.collections = 0
        self.last_error = ""
        self.post_via = ""
        self.snapshot_ready = False
        self.diag: Diagnostics = Diagnostics()
        self.models_total = 0
        self.models_eligible = 0
        self.floor_known = 0
        self.floor_unknown = 0
        self.collections_eligible = 0
        self.catalog_updated_at = 0.0


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
                hit = []
                if lot.id in self.seen:
                    hit.append("seen:id")
                if lot.slug and lot.slug in self.seen:
                    hit.append("seen:slug")
                if lot.id in self._queued:
                    hit.append("queued")
                if lot.id in self._inflight:
                    hit.append("inflight")
                classify_skip("дубль лота", self.runtime.skip_total)
                seen_kind = (
                    "listing"
                    if (lot.id in self.seen or (lot.slug and lot.slug in self.seen))
                    else "other"
                )
                record_enqueue_dup(
                    self.runtime.funnel,
                    "listing",
                    getattr(self.runtime, "diag", None),
                    seen_kind=seen_kind,
                )
                if config.DEBUG_FILTERS:
                    logger.info(
                        debug_lot_line(
                            lot,
                            "enqueue",
                            f"дубль лота ({','.join(hit) or 'seen'})",
                        )
                    )
                continue
            keys = seller_keys(lot)
            if owner_is_blocked(lot, blocked) or (keys and keys & batch_owners):
                overlap = sorted(keys & (blocked | batch_owners))
                # продавец уже опубликован / в очереди — НЕ пишем лот в seen
                logger.info(
                    "[pipeline] seller-dup skip %s · seller=%s keys=%s",
                    lot.slug or lot.id,
                    (lot.seller or str(lot.seller_id) or "?")[:24],
                    ",".join(overlap)[:80],
                )
                classify_skip("дубль продавца", self.runtime.skip_total)
                record_enqueue_dup(
                    self.runtime.funnel,
                    "seller",
                    getattr(self.runtime, "diag", None),
                )
                diag = getattr(self.runtime, "diag", None)
                if diag is not None:
                    diag.record_owner_dup(overlap)
                if config.DEBUG_FILTERS:
                    logger.info(
                        debug_lot_line(
                            lot,
                            "enqueue",
                            f"дубль продавца ({','.join(overlap)})",
                        )
                    )
                continue
            self._queued.add(lot.id)
            self._items.append(lot)
            batch_owners |= keys
            added += 1
            record_work_in(self.runtime.funnel)
            diag = getattr(self.runtime, "diag", None)
            if diag is not None:
                diag.record_enqueue_latency(lot)
            logger.info(
                "[pipeline] work_in %s · %s⭐ · q=%s",
                lot.slug or lot.id,
                int(lot.stars),
                len(self._items),
            )
            if config.DEBUG_FILTERS:
                logger.info(debug_lot_line(lot, "work_in", "price+seen ok, filters later"))
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
        self.runtime.funnel["worker_started"] += 1
        logger.info(
            "PostQueue._worker START task=%s interval=%sс — один worker на всё время",
            id(self._task),
            int(config.POST_INTERVAL),
        )
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
                _bump(self.runtime.funnel, "dequeued")
                _bump(self.runtime.funnel, "worker_processed")
                self.runtime.funnel["queue_remaining"] = len(self._items)
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
                    "REJECT_BAD_MODEL_VALUE",
                    "floor неизвестен",
                    "floor выше макс",
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
                    if config.DEBUG_FILTERS:
                        logger.info(debug_lot_line(lot, "pre-filter", pre))
                    _record_filter_diagnostics(self.runtime.diag, lot, pre)
                    record_worker_filter(self.runtime.funnel, lot, pre)
                    _bump(self.runtime.funnel, "worker_filtered")
                    self.seen[lot.id] = now
                    self.market_ids.add(lot.id)
                    self.state["market_ids"] = list(self.market_ids)
                    save_state(self.state_file, self.state)
                    continue
                had_username = bool(lot.seller)
                enrich_t0 = time.monotonic()
                enrich_ok = True
                try:
                    await self.market.enrich_lot(lot, timeout=config.ENRICH_TIMEOUT)
                except Exception:  # noqa: BLE001
                    enrich_ok = False
                    raise
                finally:
                    self.runtime.diag.record_enrich(
                        (time.monotonic() - enrich_t0) * 1000.0,
                        ok=enrich_ok,
                    )
                    self.runtime.diag.record_username(
                        lot, had_before_enrich=had_username
                    )
                logger.info(
                    "[pipeline] enriched %s · name=%s seller=%s lvl=%s dm=%s nfts=%s lang=%s",
                    tag,
                    (lot.first_name or "—")[:24],
                    (lot.seller or "?")[:24],
                    lot.account_level if lot.account_level is not None else "none",
                    lot.free_dm if lot.free_dm is not None else "none",
                    lot.gifts_count if lot.gifts_count is not None else "none",
                    lot.lang_code or "—",
                )
                self._inflight_sellers = seller_keys(lot)
                now = time.time()
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
                    reason = owner_dup_after_enrich(lot, self.seen_sellers, now)
                    if reason == "дубль продавца":
                        diag = getattr(self.runtime, "diag", None)
                        if diag is not None:
                            diag.record_owner_dup(seller_keys(lot))
                            diag.note_owner_dup_stage("post_enrich")
                    elif reason == "нет продавца":
                        _bump(self.runtime.funnel, "owner_id_missing")
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
                    if config.DEBUG_FILTERS:
                        logger.info(debug_lot_line(lot, "final-filter", reason))
                    _record_filter_diagnostics(self.runtime.diag, lot, reason)
                    record_worker_filter(self.runtime.funnel, lot, reason)
                    _bump(self.runtime.funnel, "worker_filtered")
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
                if config.DEBUG_FILTERS:
                    logger.info(debug_lot_line(lot, "final-filter", "ok"))
                _record_filter_diagnostics(self.runtime.diag, lot, "")
                record_worker_filter(self.runtime.funnel, lot, "")
                logger.info("[pipeline] send %s …", lot.slug or lot.id)

                async def _gated_send() -> tuple[str, str]:
                    reload_seen_sellers(self.state_file, self.seen_sellers)
                    guard = claim_owner_for_send(lot, self.seen_sellers)
                    if guard:
                        return ("reject", guard)
                    self.state["seen_sellers"] = self.seen_sellers
                    save_state(self.state_file, self.state)
                    _bump(self.runtime.funnel, "send_attempt")
                    via_inner = await self.sender.deliver(lot)
                    now_sent = time.time()
                    persist_sent_owner(self.seen_sellers, lot, now_sent)
                    _bump(self.runtime.funnel, "owner_sent_persisted")
                    _bump(self.runtime.funnel, "owner_sent_total")
                    self.runtime.diag.note_owner_sent()
                    self.seen[lot.id] = now_sent
                    if lot.slug:
                        self.seen[lot.slug] = now_sent
                    self.market_ids.add(lot.id)
                    self.state["seen_sellers"] = self.seen_sellers
                    self.state["market_ids"] = list(self.market_ids)
                    save_state(self.state_file, self.state)
                    return ("ok", via_inner)

                outcome, payload = await self.sender.limiter.gated(_gated_send)
                if outcome == "reject":
                    _bump(self.runtime.funnel, "owner_dup_send_guard")
                    _bump(self.runtime.funnel, "owner_duplicate")
                    _bump(self.runtime.funnel, "owner_duplicate_total")
                    if payload == "нет продавца":
                        _bump(self.runtime.funnel, "owner_id_missing")
                    else:
                        diag = getattr(self.runtime, "diag", None)
                        if diag is not None:
                            diag.record_owner_dup(seller_keys(lot))
                            diag.record_seen_reason("owner")
                            diag.note_owner_dup_stage("send_guard")
                    classify_skip(payload, self.runtime.skip_total)
                    _bump(self.runtime.funnel, "worker_filtered")
                    logger.info(
                        "[pipeline] REJECT_OWNER_ALREADY_SENT send_guard %s · %s",
                        lot.slug or lot.id,
                        payload,
                    )
                    if payload == "нет продавца":
                        continue
                    self.seen[lot.id] = now
                    if lot.slug:
                        self.seen[lot.slug] = now
                    self.market_ids.add(lot.id)
                    save_state(self.state_file, self.state)
                    continue
                via = payload
                self.runtime.post_via = via
                self.runtime.posted += 1
                _bump(self.runtime.funnel, "sent")
                self._last_title = lot.title or ""
                self.runtime.diag.record_send_latency(lot)
                logger.info(
                    "[pipeline] send ok %s за %s⭐ · lvl %s · via=%s · очередь %s",
                    lot.title,
                    int(lot.stars),
                    format_level(lot),
                    via,
                    len(self._items),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка worker/send %s", lot.id)
                self.runtime.last_error = str(exc)
                _bump(self.runtime.funnel, "failed")
                _bump(self.runtime.funnel, "worker_failed")
                if "FloodWait" in type(exc).__name__ or "flood" in str(exc).lower():
                    _bump(self.runtime.funnel, "flood_wait")
                    sec = 0.0
                    try:
                        sec = float(getattr(exc, "seconds", 0) or 0)
                    except (TypeError, ValueError):
                        sec = 0.0
                    self.runtime.diag.note_flood("send", sec)
            finally:
                self._inflight.discard(lot.id)
                self._inflight_sellers = set()
                self.runtime.queue = len(self._items)
                self.runtime.funnel["queue_remaining"] = len(self._items)


def model_request_key(collection_id: int, model_ids: list[int] | None) -> str:
    """Ключ запроса: collection_id + отсортированные model_id этого RPC."""
    mids = sorted({int(x) for x in (model_ids or []) if int(x) > 0})
    return f"{int(collection_id)}:{','.join(str(x) for x in mids)}"


def observed_contains(observed: dict[str, Any], lot: Lot) -> bool:
    if lot.id and lot.id in observed:
        return True
    if lot.slug and lot.slug in observed:
        return True
    return False


def observed_record(observed: dict[str, Any], lot: Lot) -> dict[str, Any] | None:
    raw = None
    if lot.id and lot.id in observed:
        raw = observed.get(lot.id)
    elif lot.slug and lot.slug in observed:
        raw = observed.get(lot.slug)
    if raw is None:
        return None
    return normalize_observed_record(raw)


def remember_listing(
    observed: dict[str, Any],
    lot: Lot,
    *,
    now: float,
    collection_id: int | None = None,
) -> None:
    cid = collection_id if collection_id is not None else lot.collection_id
    seed_observed_id(
        observed,
        lot.id,
        now=now,
        collection_id=cid,
        model_id=lot.model_id,
    )
    if lot.slug and lot.slug != lot.id:
        seed_observed_id(
            observed,
            lot.slug,
            now=now,
            collection_id=cid,
            model_id=lot.model_id,
        )


def freshness_verdict_dict(
    lot: Lot,
    *,
    reason: str,
    genuine_new: bool,
    first_seen_at: float | None,
    previous_seen_at: float | None,
    snapshot_contains_before: bool,
    seen_contains_before: bool,
    page_number: int,
    offset: str,
    source_request: str,
    collection_id: int | None,
) -> dict[str, Any]:
    return {
        "collection_id": collection_id,
        "model_id": lot.model_id,
        "listing_id": lot.id,
        "listing_price": float(lot.stars),
        "first_seen_at": first_seen_at,
        "previous_seen_at": previous_seen_at,
        "snapshot_contains_before": snapshot_contains_before,
        "seen_contains_before": seen_contains_before,
        "page_number": page_number,
        "offset": offset,
        "source_request": source_request,
        "reason": reason,
        "genuine_new": genuine_new,
    }


def log_freshness_forensic(row: dict[str, Any]) -> None:
    logger.info(
        "FRESHNESS listing_id=%s collection_id=%s model_id=%s price=%s "
        "first_seen_at=%s previous_seen_at=%s snapshot_contains_before=%s "
        "seen_contains_before=%s page_number=%s offset=%s source=%s reason=%s",
        row.get("listing_id"),
        row.get("collection_id"),
        row.get("model_id"),
        row.get("listing_price"),
        row.get("first_seen_at"),
        row.get("previous_seen_at"),
        row.get("snapshot_contains_before"),
        row.get("seen_contains_before"),
        row.get("page_number"),
        row.get("offset") or "0",
        row.get("source_request"),
        row.get("reason"),
    )


def detect_fresh_lots(
    lots: list[Lot],
    *,
    observed: dict[str, Any],
    primed: dict[str, float],
    pages: dict[str, list[str]],
    seen: dict[str, float] | set[str],
    collection_id: int,
    model_ids: list[int] | None,
    round_hits: dict[str, list[tuple[int, int | None]]],
    now: float | None = None,
    min_stars: float | None = None,
    max_stars: float | None = None,
    stats: dict[str, int] | None = None,
    forensic: list[dict[str, Any]] | None = None,
    request_ok: bool = True,
) -> tuple[list[Lot], list[dict[str, Any]]]:
    """Классификация freshness: GENUINE_NEW только если listing_id никогда не видели
    И запрос (collection+models) уже primed.

    Не «нет в текущей page/snapshot». Первый визит ключа — UNPRIMED_SEED, без постов.
    """
    ts = time.time() if now is None else float(now)
    lo = config.MIN_STARS if min_stars is None else float(min_stars)
    hi = config.MAX_STARS if max_stars is None else float(max_stars)
    req_key = model_request_key(collection_id, model_ids)
    primed_before = req_key in primed
    snapshot_ids = set(pages.get(req_key) or [])
    verdicts: list[dict[str, Any]] = []
    genuine: list[Lot] = []
    page_ids: list[str] = []

    for lot in lots:
        if stats is not None:
            _bump(stats, "api_observations")
        lid = str(lot.id)
        if lid:
            page_ids.append(lid)
        rec_before = observed_record(observed, lot)
        observed_before = rec_before is not None
        snapshot_before = lid in snapshot_ids
        seen_before = bool(lid and lid in seen) or bool(lot.slug and lot.slug in seen)
        first_seen = float(rec_before["first"]) if rec_before else None
        prev_seen = float(rec_before["last"]) if rec_before else None
        mid = lot.model_id
        prior = list(round_hits.get(lid) or [])
        page_no = int(getattr(lot, "scan_page", 0) or 0)
        offset = str(getattr(lot, "scan_offset", "") or "")
        source = str(getattr(lot, "scan_source", "") or "") or (
            f"scan:collection={collection_id}:models={req_key.split(':', 1)[-1]}"
            f":page={page_no or 1}:offset={offset or '0'}"
        )

        dup_round = bool(prior)
        if dup_round and stats is not None:
            _bump(stats, "duplicate_listing_ids_same_round")
            _bump(stats, "observed_duplicate_same_round")
            if any(p_mid != mid for _gid, p_mid in prior):
                _bump(stats, "duplicate_listing_ids_across_models")
                _bump(stats, "observed_duplicate_cross_model")
            if any(int(p_gid) != int(collection_id) for p_gid, _mid in prior):
                _bump(stats, "duplicate_listing_ids_across_collections")

        if lid:
            round_hits.setdefault(lid, []).append((int(collection_id), mid))

        if dup_round:
            reason = "OLD"
            is_new = False
            if stats is not None:
                _bump(stats, "fresh_repeated")
                _bump(stats, "observed_old")
        elif observed_before or seen_before:
            reason = "OLD"
            is_new = False
            if stats is not None:
                _bump(stats, "unique_listing_ids")
                _bump(stats, "observed_old")
                if snapshot_before is False:
                    _bump(stats, "fresh_repeated")
        elif not primed_before:
            reason = "UNPRIMED_SEED"
            is_new = False
            if stats is not None:
                _bump(stats, "unique_listing_ids")
                _bump(stats, "unprimed_seed")
        else:
            reason = "NEW"
            is_new = True
            if stats is not None:
                _bump(stats, "unique_listing_ids")
                _bump(stats, "genuine_new")
                _bump(stats, "genuine_new_listings")
                _bump(stats, "fresh_unique")

        row = freshness_verdict_dict(
            lot,
            reason=reason,
            genuine_new=is_new,
            first_seen_at=first_seen if first_seen is not None else ts,
            previous_seen_at=prev_seen,
            snapshot_contains_before=snapshot_before,
            seen_contains_before=seen_before,
            page_number=page_no,
            offset=offset,
            source_request=source,
            collection_id=collection_id,
        )
        verdicts.append(row)
        if forensic is not None:
            forensic.append(row)
            if len(forensic) > FORENSIC_KEEP:
                del forensic[:-FORENSIC_KEEP]
        remember_listing(observed, lot, now=ts, collection_id=collection_id)

        if is_new:
            price_ok = lo <= float(lot.stars) <= hi
            if stats is not None:
                record_fresh_price_seen(
                    stats,
                    fresh=True,
                    price_ok=price_ok,
                    already_seen=seen_before,
                )
            if price_ok and not seen_before:
                genuine.append(lot)

    if request_ok:
        primed[req_key] = ts
        if page_ids:
            pages[req_key] = merge_page_snapshot(
                pages.get(req_key),
                page_ids,
                keep=int(config.PAGE_SNAPSHOT_KEEP),
            )
    return genuine, verdicts


def merge_page_snapshot(
    prev: list[str] | None, page_ids: list[str], *, keep: int = 80
) -> list[str]:
    """Накопленные listing id запроса collection+models. Порядок: текущая страница."""
    out: list[str] = []
    seen: set[str] = set()
    for item in list(page_ids) + list(prev or []):
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= max(1, int(keep)):
            break
    return out


def fresh_from_page(
    prev_ids: list[str] | None,
    lots: list[Lot],
    seen: dict[str, float] | set[str],
    min_stars: float,
    max_stars: float,
) -> tuple[list[str], list[Lot]]:
    """LEGACY page-diff. Scanner больше не использует это как единственный freshness.

    GENUINE_NEW считается в detect_fresh_lots: listing_id никогда не observed
    И (collection+models) уже primed. Пустой prev здесь = «не постить», но
    отсутствие id только в текущей page/snapshot НЕ делает listing новым.
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
    observed: dict[str, Any] | None = None,
    primed: dict[str, float] | None = None,
) -> None:
    """Прогрев observed. НЕ затирает накопленный snapshot и НЕ постит."""
    ids = list(gift_ids)
    random.shuffle(ids)
    store = observed if observed is not None else {}
    primed_store = primed if primed is not None else {}
    logger.info(
        "Синхрон observed · %s eligible коллекций — merge only, без постов",
        len(ids),
    )
    parallel = max(2, int(config.SCAN_PARALLEL))
    sem = asyncio.Semaphore(parallel)

    async def one(gid: int) -> tuple[int, list[Lot], list[int]]:
        async with sem:
            try:
                model_ids = market.floors.eligible_model_ids(gid)
                if not model_ids:
                    return gid, [], []
                lots, _meta = await market.fetch_newest_until_known(
                    gid,
                    model_ids=model_ids,
                    known_ids=set(store),
                    seen={},
                    max_pages=max(1, int(config.SCAN_MAX_PAGES)),
                    limit=config.PAGE_LIMIT,
                    timeout=config.REQUEST_TIMEOUT,
                    gap=config.REQUEST_GAP,
                )
            except Exception as exc:  # noqa: BLE001
                runtime.last_error = str(exc)
                lots = []
                model_ids = []
            return gid, lots, list(model_ids or [])

    now = time.time()
    chunk = max(parallel * 4, 16)
    for i in range(0, len(ids), chunk):
        batch = ids[i : i + chunk]
        parts = await asyncio.gather(*[one(g) for g in batch], return_exceptions=True)
        for part in parts:
            if not isinstance(part, tuple):
                runtime.last_error = str(part)
                continue
            gid, lots, model_ids = part
            if not lots:
                continue
            for lot in lots:
                remember_listing(store, lot, now=now, collection_id=gid)
            # All-models warmup ≠ per-chunk prime: не помечаем chunk-ключи.
            req_key = model_request_key(gid, model_ids)
            primed_store[req_key] = now
            page_ids = [lot.id for lot in lots if lot.id]
            if page_ids:
                pages[req_key] = merge_page_snapshot(
                    pages.get(req_key),
                    page_ids,
                    keep=int(config.PAGE_SNAPSHOT_KEEP),
                )
        runtime.snapshot = len(store)
        logger.info(
            "Observed %s/%s · %s ids",
            min(i + len(batch), len(ids)),
            len(ids),
            len(store),
        )
    logger.info("Observed готов: %s id. Жду только genuinely new", len(store))


def apply_catalog_stats(runtime: Runtime, market: TelegramMarket) -> None:
    st = market.floors.stats()
    runtime.models_total = int(st.get("models_total") or 0)
    runtime.models_eligible = int(st.get("eligible_model_count") or 0)
    runtime.floor_known = int(st.get("model_floor_known") or 0)
    runtime.floor_unknown = int(st.get("model_floor_unknown") or 0)
    runtime.collections_eligible = len(market.scan_ids)
    runtime.catalog_updated_at = float(market.floors.updated_at or 0)
    runtime.diag.note_catalog(st, runtime.collections_eligible)


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
    observed: dict[str, Any] | None = None,
    primed: dict[str, float] | None = None,
) -> None:
    logger.info(
        "Сканер: allowlist models · batch=%s (0=все eligible) "
        "parallel=%s rpc=%s page=%s · listing %s–%s⭐ ±%s · "
        "floor %s–%s⭐ · girl_score≥%s · post/%sс",
        config.SCAN_BATCH,
        config.SCAN_PARALLEL,
        config.RPC_CONCURRENCY,
        config.PAGE_LIMIT,
        config.MIN_STARS,
        config.MAX_STARS,
        int(config.LISTING_PRICE_TOLERANCE),
        config.MIN_MODEL_FLOOR,
        config.MAX_MODEL_FLOOR,
        config.GIRL_MIN_SCORE,
        int(config.POST_INTERVAL),
    )
    observed_store = observed if observed is not None else state.setdefault("observed", {})
    primed_store = primed if primed is not None else state.setdefault("primed_models", {})
    pass_no = 0
    while True:
        round_started_at = time.time()
        started = time.monotonic()
        pass_no += 1
        runtime.passes = pass_no
        diag = runtime.diag
        flood_before = diag.scan_floodwait_count
        flood_s_before = diag.scan_floodwait_seconds
        timeout_before = diag.scan_timeout_count
        fn_before = dict(runtime.funnel)
        if not market.floors.is_fresh():
            try:
                await market.refresh_model_floors(gift_ids)
                apply_catalog_stats(runtime, market)
            except Exception as exc:  # noqa: BLE001
                logger.warning("floor refresh: %s", exc)
        scan_pool = list(market.scan_ids) or market.floors.scan_collection_ids(gift_ids)
        market.scan_ids = scan_pool
        n_coll = max(len(scan_pool), 1)
        batch_n = n_coll if config.SCAN_BATCH <= 0 else min(config.SCAN_BATCH, n_coll)
        if pass_no == 1:
            waves = max(1, (batch_n + config.SCAN_PARALLEL - 1) // config.SCAN_PARALLEL)
            ring = max(1, (n_coll + batch_n - 1) // batch_n)
            logger.info(
                "Кольцо eligible: %s колл · %s/проход · %s волн · круг ≈%s · "
                "model_chunk=%s max_pages=%s · total collections=%s · "
                "post interval %sс ≠ scan round",
                n_coll,
                batch_n,
                waves,
                ring,
                int(config.SCAN_MODEL_CHUNK),
                int(config.SCAN_MAX_PAGES),
                len(gift_ids),
                int(config.POST_INTERVAL),
            )
        batch = market.next_batch(config.SCAN_BATCH, pool=scan_pool)
        if not gift_ids or len(gift_ids) < config.MIN_COLLECTIONS:
            try:
                fresh_ids = await market.load_collections(force=True, bot=bot)
                if fresh_ids:
                    gift_ids[:] = list(fresh_ids)
                    runtime.collections = len(gift_ids)
                    await market.refresh_model_floors(gift_ids, force=True)
                    apply_catalog_stats(runtime, market)
                    scan_pool = list(market.scan_ids)
                if market.last_error and len(gift_ids) < config.MIN_COLLECTIONS:
                    runtime.last_error = market.last_error
            except Exception as exc:  # noqa: BLE001
                logger.error("коллекции: %s", exc)
                runtime.last_error = str(exc)
            if not gift_ids:
                await asyncio.sleep(5)
                continue
            batch = market.next_batch(config.SCAN_BATCH, pool=scan_pool)
        if not batch:
            await asyncio.sleep(5)
            continue
        listing_lo, listing_hi = listing_price_range()
        sem = asyncio.Semaphore(config.SCAN_PARALLEL)

        async def one(gid: int) -> tuple[int, list[Lot], bool, dict[str, Any]]:
            async with sem:
                ok = True
                meta: dict[str, Any] = {
                    "pages": 0,
                    "new": 0,
                    "old": 0,
                    "depths": {},
                    "models": 0,
                    "model_ids": [],
                    "request_key": model_request_key(gid, []),
                }
                try:
                    model_ids = market.floors.eligible_model_ids(gid)
                    if not model_ids:
                        return gid, [], True, meta
                    chunk_ids = market.next_model_chunk(
                        gid, model_ids, int(config.SCAN_MODEL_CHUNK)
                    )
                    used_models = chunk_ids or model_ids
                    req_key = model_request_key(gid, used_models)
                    prev_ids = pages.get(req_key) or []
                    # known = глобальный observed, не «текущая page коллекции»
                    known = set(observed_store) | set(prev_ids)
                    with market.rpc_kind("scan"):
                        lots, meta = await market.fetch_newest_until_known(
                            gid,
                            model_ids=used_models,
                            known_ids=known,
                            seen=seen,
                            max_pages=max(1, int(config.SCAN_MAX_PAGES)),
                            limit=config.PAGE_LIMIT,
                            timeout=config.REQUEST_TIMEOUT,
                            gap=config.REQUEST_GAP,
                        )
                    meta["model_ids"] = list(used_models)
                    meta["request_key"] = req_key
                    if not market.last_fetch_ok:
                        ok = False
                except Exception as exc:  # noqa: BLE001
                    runtime.last_error = str(exc)
                    lots = []
                    ok = False
                return gid, lots, ok, meta

        found = 0
        queued = 0
        api_n = 0
        api_fetch_count = 0
        collections_success = 0
        collections_failed = 0

        seen_skip = 0
        seller_skip = 0
        round_hits: dict[str, list[tuple[int, int | None]]] = {}

        def absorb(
            gid: int,
            lots: list[Lot],
            meta: dict[str, Any] | None = None,
            ok: bool = True,
        ) -> None:
            nonlocal found, queued, api_n, seen_skip, seller_skip
            info = meta or {}
            api_n += len(lots)
            _bump(runtime.funnel, "collections_scanned")
            if market.floors.eligible_model_ids(gid):
                _bump(runtime.funnel, "eligible_collections_scanned")
            new_n = int(info.get("new") or 0)
            old_n = int(info.get("old") or 0)
            _bump(runtime.funnel, "new_listing_seen", new_n)
            _bump(runtime.funnel, "old_listing_seen", old_n)
            depths = info.get("depths") or {}
            depth_map = depths if isinstance(depths, dict) else {}
            if depth_map:
                try:
                    deepest = max(int(x) for x in depth_map.values())
                except (TypeError, ValueError):
                    deepest = 0
                runtime.funnel["listing_page_depth"] = max(
                    int(runtime.funnel.get("listing_page_depth") or 0),
                    deepest,
                )
            runtime.diag.record_scan_discovery(
                gid,
                new_n=new_n,
                old_n=old_n,
                pages=int(info.get("pages") or 0),
                models=int(info.get("models") or 0),
                fresh_candidates=0,
                depths=depth_map or None,
                eligible=bool(market.floors.eligible_model_ids(gid)),
            )
            for lot in lots:
                _bump(runtime.funnel, "listing_checked")
                if listing_price_ok(float(lot.stars)):
                    _bump(runtime.funnel, "listing_price_pass")
                else:
                    _bump(runtime.funnel, "listing_price_reject")
            used_models = list(info.get("model_ids") or [])
            chunk, verdicts = detect_fresh_lots(
                lots,
                observed=observed_store,
                primed=primed_store,
                pages=pages,
                seen=seen,
                collection_id=gid,
                model_ids=used_models,
                round_hits=round_hits,
                min_stars=listing_lo,
                max_stars=listing_hi,
                stats=runtime.funnel,
                request_ok=ok,
            )
            for row in verdicts:
                runtime.diag.record_freshness_verdict(row)
                if row.get("genuine_new"):
                    log_freshness_forensic(row)
            for row in verdicts:
                if row.get("genuine_new") and row.get("listing_price") is not None:
                    price_ok = listing_lo <= float(row["listing_price"]) <= listing_hi
                    already = bool(row.get("seen_contains_before"))
                    if price_ok is False:
                        runtime.diag.record_price_reject(float(row["listing_price"]))
                    elif already:
                        runtime.diag.record_seen_reason("listing")
                        seen_skip += 1
            runtime.diag.note_new_candidates(gid, len(chunk))
            if chunk:
                slugs = ",".join((x.slug or x.id)[:22] for x in chunk[:8])
                logger.info(
                    "[pipeline] GENUINE_NEW gid=%s n=%s pages=%s models=%s %s",
                    gid,
                    len(chunk),
                    info.get("pages", 0),
                    info.get("models", 0),
                    slugs,
                )
                for lot in chunk:
                    lot.discovery_round = pass_no
                    diag.record_detection(lot, pass_no=pass_no)
            if not chunk:
                return
            chunk = apply_listing_floor_filters(
                chunk, market.floors, runtime.funnel
            )
            if not chunk:
                return
            found += len(chunk)
            enqueued = queue.enqueue(chunk)
            seller_skip += len(chunk) - enqueued
            queued += enqueued

        tasks = [asyncio.create_task(one(g)) for g in batch]
        for fut in asyncio.as_completed(tasks):
            try:
                gid, lots, ok, meta = await fut
            except Exception as exc:  # noqa: BLE001
                runtime.last_error = str(exc)
                collections_failed += 1
                continue
            api_fetch_count += 1
            if ok:
                collections_success += 1
            else:
                collections_failed += 1
            absorb(gid, lots, meta, ok)
        runtime.last_found = found
        runtime.last_fresh = queued
        runtime.snapshot = len(observed_store)
        fn = runtime.funnel
        sync_funnel_aliases(fn)
        runtime.last_funnel = dict(fn)
        inv = funnel_invariants(fn)
        if inv:
            logger.warning("[pipeline] FUNNEL invariants: %s", "; ".join(inv))
        round_ms = (time.monotonic() - started) * 1000.0
        round_finished_at = time.time()
        flood_count = diag.scan_floodwait_count - flood_before
        flood_seconds = diag.scan_floodwait_seconds - flood_s_before
        timeout_count = diag.scan_timeout_count - timeout_before
        fresh_delta = int(fn.get("fresh_detected", 0)) - int(
            fn_before.get("fresh_detected", 0)
        )
        dup_s_delta = int(fn.get("dup_seller", 0)) - int(fn_before.get("dup_seller", 0))
        dup_l_delta = int(fn.get("dup_listing", 0)) - int(
            fn_before.get("dup_listing", 0)
        )
        diag.record_scan_round(
            {
                "pass": pass_no,
                "round_started_at": round_started_at,
                "round_finished_at": round_finished_at,
                "round_ms": round_ms,
                "collections_checked": len(batch),
                "collections_success": collections_success,
                "collections_failed": collections_failed,
                "api_fetch_count": api_fetch_count,
                "found_in_range": found,
                "fresh_detected": fresh_delta,
                "queued": queued,
                "duplicate_seller": dup_s_delta,
                "duplicate_listing": dup_l_delta,
                "flood_wait_count": flood_count,
                "flood_wait_seconds": flood_seconds,
                "timeout_count": timeout_count,
            }
        )
        logger.info(
            "[pipeline] pass #%s API=%s unique=%s GENUINE_NEW=%s "
            "fresh=%s price_pass=%s seen_pass=%s "
            "observed_old=%s unprimed=%s dup_round=%s "
            "dup_seller=%s dup_listing=%s work_in=%s dequeued=%s "
            "ru_pass=%s girl_pass=%s nft_pass=%s send=%s/%s q=%s",
            pass_no,
            fn.get("api_observations", api_n),
            fn.get("unique_listing_ids", 0),
            fn.get("genuine_new", 0),
            fn.get("fresh_detected", 0),
            fn.get("price_pass", 0),
            fn.get("seen_pass", 0),
            fn.get("observed_old", 0),
            fn.get("unprimed_seed", 0),
            fn.get("duplicate_listing_ids_same_round", 0),
            fn.get("dup_seller", 0),
            fn.get("dup_listing", 0),
            fn.get("work_in", 0),
            fn.get("dequeued", 0),
            fn.get("ru_pass", 0),
            fn.get("girl_pass", 0),
            fn.get("nft_pass", 0),
            fn.get("sent", 0),
            fn.get("send_attempt", 0),
            len(queue._items),
        )
        # Полный отчёт — раз в 8 проходов или когда есть sent
        if pass_no == 1 or pass_no % 8 == 0 or fn.get("sent", 0):
            for line in format_funnel_report(fn).splitlines():
                logger.info("[pipeline] %s", line)
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
        state["observed"] = observed_store
        state["primed_models"] = primed_store
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
    observed: dict[str, Any] = state.setdefault("observed", {})
    if not isinstance(observed, dict):
        observed = {}
        state["observed"] = observed
    primed: dict[str, float] = state.setdefault("primed_models", {})
    if not isinstance(primed, dict):
        primed = {}
        state["primed_models"] = primed
    migrate_observed_from_legacy(state)

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
    market.diag = runtime.diag
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
    if gift_ids:
        try:
            await market.refresh_model_floors(gift_ids)
            apply_catalog_stats(runtime, market)
        except Exception as exc:  # noqa: BLE001
            logger.error("floor catalog: %s", exc)
            runtime.last_error = str(exc)
    if not gift_ids:
        logger.error("Коллекций нет — бот жив, сканер будет пробовать снова")
        runtime.last_error = market.last_error or "нет коллекций"
    queue = PostQueue(
        market, sender, seen, seen_sellers, state, state_file, runtime, market_ids
    )
    queue.start()
    control.queue = queue

    sync_ids = list(market.scan_ids) or list(gift_ids)
    if sync_ids:
        try:
            await sync_pages(
                market, sync_ids, pages, runtime, observed=observed, primed=primed
            )
            state["pages"] = pages
            state["observed"] = observed
            state["primed_models"] = primed
            save_state(state_file, state)
        except Exception as exc:  # noqa: BLE001
            logger.error("страницы: %s", exc)
            runtime.last_error = str(exc)
    runtime.snapshot_ready = True
    runtime.snapshot = len(observed)

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
                    observed=observed,
                    primed=primed,
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
