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
    """Один новый id относительно prev page (вызывается один раз на объект).

    fresh=True → объект обнаружен как новый относительно предыдущей страницы.
    Дальше: price → (если price_ok) seen.
    """
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
    """После успешного send: canonical id: + username alias. Не пишет UNKNOWN."""
    ts = time.time() if now is None else now
    canon = canonical_owner_key(lot)
    if canon:
        seen_sellers[canon] = ts
    if lot.seller:
        u = lot.seller.lower().lstrip("@").strip()
        if u:
            seen_sellers[f"u:{u}"] = ts


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


def record_enqueue_dup(stats: dict[str, int], kind: str) -> None:
    """kind: listing | seller. Только на enqueue, до worker-фильтров."""
    if kind == "listing":
        _bump(stats, "dup_listing")
        _bump(stats, "reject_duplicate_listing")
    elif kind == "seller":
        _bump(stats, "dup_seller")
        _bump(stats, "reject_duplicate_seller")


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
    stats["price"] = stats.get("price_pass", 0)
    stats["ru"] = stats.get("ru_pass", 0)
    stats["girl"] = stats.get("girl_pass", 0)
    stats["dm"] = stats.get("dm_pass", 0)
    stats["level"] = stats.get("level_pass", 0)
    stats["nft"] = stats.get("nft_pass", 0)
    stats["duplicate"] = stats.get("dup_seller", 0) + stats.get("dup_listing", 0)
    stats["enqueued"] = stats.get("nft_pass", 0)  # прошли все фильтры → к send


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
    """Логи/счётчики male·ru·girl. Не влияет на решения фильтра."""
    if reason in {"дубль", "дубль продавца"}:
        return
    if looks_male(lot) or reason == "мужской":
        log_male_forensics(lot, rejected=True)
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
                record_enqueue_dup(self.runtime.funnel, "listing")
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
                record_enqueue_dup(self.runtime.funnel, "seller")
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
                    if pre == "мужской":
                        log_male_forensics(lot, rejected=True)
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
                _bump(self.runtime.funnel, "send_attempt")
                logger.info("[pipeline] send %s …", lot.slug or lot.id)
                via = await self.sender.send(lot)
                self.runtime.post_via = via
                self.seen[lot.id] = now
                if lot.slug:
                    self.seen[lot.slug] = now
                self.market_ids.add(lot.id)
                persist_sent_owner(self.seen_sellers, lot, now)
                self.state["seen_sellers"] = self.seen_sellers
                self.state["market_ids"] = list(self.market_ids)
                save_state(self.state_file, self.state)
                self.runtime.posted += 1
                _bump(self.runtime.funnel, "sent")
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
        "Сканер: detection≠post · batch=%s (0=все, иначе кольцо) "
        "parallel=%s rpc=%s page=%s · %s–%s⭐ · girl_score≥%s · post/%sс",
        config.SCAN_BATCH,
        config.SCAN_PARALLEL,
        config.RPC_CONCURRENCY,
        config.PAGE_LIMIT,
        config.MIN_STARS,
        config.MAX_STARS,
        config.GIRL_MIN_SCORE,
        int(config.POST_INTERVAL),
    )
    n_coll = max(len(gift_ids), 1)
    batch_n = n_coll if config.SCAN_BATCH <= 0 else min(config.SCAN_BATCH, n_coll)
    waves = max(1, (batch_n + config.SCAN_PARALLEL - 1) // config.SCAN_PARALLEL)
    ring = max(1, (n_coll + batch_n - 1) // batch_n)
    logger.info(
        "Кольцо: %s колл/проход · %s волн/проход · полный круг ≈%s проход(а) · "
        "post interval %sс ≠ scan round",
        batch_n,
        waves,
        ring,
        int(config.POST_INTERVAL),
    )
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

        async def one(gid: int) -> tuple[int, list[Lot], bool]:
            async with sem:
                ok = True
                try:
                    with market.rpc_kind("scan"):
                        lots = await market.fetch_page(
                            gid,
                            limit=config.PAGE_LIMIT,
                            timeout=config.REQUEST_TIMEOUT,
                            gap=config.REQUEST_GAP,
                            sort_by_price=False,
                        )
                    if not market.last_fetch_ok:
                        ok = False
                except Exception as exc:  # noqa: BLE001
                    runtime.last_error = str(exc)
                    lots = []
                    ok = False
                return gid, lots, ok

        found = 0
        queued = 0
        api_n = 0
        api_fetch_count = 0
        collections_success = 0
        collections_failed = 0

        seen_skip = 0
        seller_skip = 0

        def absorb(gid: int, lots: list[Lot]) -> None:
            nonlocal found, queued, api_n, seen_skip, seller_skip
            api_n += len(lots)
            key = str(gid)
            prev = pages.get(key)
            if prev:
                known = set(prev)
                for lot in lots:
                    if lot.id in known:
                        continue
                    price_ok = config.MIN_STARS <= float(lot.stars) <= config.MAX_STARS
                    already = lot.id in seen or bool(lot.slug and lot.slug in seen)
                    record_fresh_price_seen(
                        runtime.funnel,
                        fresh=True,
                        price_ok=price_ok,
                        already_seen=already,
                    )
                    if already:
                        seen_skip += 1
            new_page, chunk = fresh_from_page(
                prev,
                lots,
                seen,
                config.MIN_STARS,
                config.MAX_STARS,
            )
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
                for lot in chunk:
                    lot.discovery_round = pass_no
                    diag.record_detection(lot, pass_no=pass_no)
            if not chunk:
                return
            found += len(chunk)
            enqueued = queue.enqueue(chunk)
            seller_skip += len(chunk) - enqueued
            queued += enqueued

        tasks = [asyncio.create_task(one(g)) for g in batch]
        for fut in asyncio.as_completed(tasks):
            try:
                gid, lots, ok = await fut
            except Exception as exc:  # noqa: BLE001
                runtime.last_error = str(exc)
                collections_failed += 1
                continue
            api_fetch_count += 1
            if ok:
                collections_success += 1
            else:
                collections_failed += 1
            absorb(gid, lots)
        runtime.last_found = found
        runtime.last_fresh = queued
        runtime.snapshot = len(pages)
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
            "[pipeline] pass #%s API=%s fresh=%s price_pass=%s seen_pass=%s "
            "dup_seller=%s dup_listing=%s work_in=%s dequeued=%s "
            "ru_pass=%s girl_pass=%s nft_pass=%s send=%s/%s q=%s",
            pass_no,
            api_n,
            fn.get("fresh_detected", 0),
            fn.get("price_pass", 0),
            fn.get("seen_pass", 0),
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
