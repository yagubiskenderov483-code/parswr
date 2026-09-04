"""Instrumentation only: scan/enrich/floodwait/RU/girl/username forensics.

Не меняет бизнес-фильтры и detection. Только counters + structured logs.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

import config
from filters import (
    _CYR_RE,
    _FEMALE_EMOJI_RE,
    _FEMALE_GIFTS,
    _FEMALE_HINT_RE,
    _FOREIGN_LANG,
    _MALE_EMOJI_RE,
    _MALE_HINT_RE,
    _MALE_NAMES_RE,
    _MALE_TRANSLIT_RE,
    _blob,
    _first_token,
    _latin_only,
    _norm,
    _username_female,
    _username_female_name,
    female_reason,
    female_score,
    has_female_identity,
    is_cyrillic_female_name,
    is_latin_female_name,
    is_russian,
    looks_male,
    russian_why,
)
from market import Lot

logger = logging.getLogger("diagnostics")

SCAN_ROUNDS_KEEP = 48
MS_SAMPLES_KEEP = 96
DETECT_KEEP = 32


def percentile(samples: list[float], p: float) -> float | None:
    if not samples:
        return None
    if len(samples) == 1:
        return float(samples[0])
    ordered = sorted(float(x) for x in samples)
    if p <= 0:
        return ordered[0]
    if p >= 100:
        return ordered[-1]
    k = (len(ordered) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    w = k - lo
    return ordered[lo] * (1.0 - w) + ordered[hi] * w


def _fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms >= 1000:
        return f"{ms / 1000.0:.1f}s"
    return f"{ms:.0f}ms"


def russian_reject_code(lot: Lot) -> str:
    """Код причины is_russian is False. Не меняет фильтр."""
    lang = (lot.lang_code or "").strip().lower()
    if lang in _FOREIGN_LANG:
        return "foreign_lang"
    name_bio = " ".join(
        x for x in (lot.first_name, lot.last_name, lot.about) if x
    ).strip()
    seller = lot.seller or ""
    if _CYR_RE.search(name_bio) or _CYR_RE.search(seller):
        # кириллица есть, но фильтр False — не должно случаться; запасной код
        return "other"
    if name_bio or seller or lang:
        return "no_cyrillic"
    return "no_russian_signal"


def male_diag_reason(lot: Lot) -> str:
    """Короткий код male reject без PII."""
    if not looks_male(lot):
        return "pass"
    fn = _first_token(lot.first_name)
    blob = _blob(lot)
    if _MALE_HINT_RE.search(blob):
        return "male_hint"
    if _MALE_EMOJI_RE.search(blob):
        return "male_emoji"
    if fn:
        latin = _latin_only(fn)
        if latin and _MALE_TRANSLIT_RE.match(latin):
            return "male_name_translit"
        if _MALE_NAMES_RE.search(fn):
            return "male_name"
    seller = _norm(lot.seller)
    if seller and (_MALE_NAMES_RE.search(seller) or _MALE_TRANSLIT_RE.search(seller)):
        return "male_username"
    return "male_other"


def identity_type(lot: Lot) -> str:
    fn = _first_token(lot.first_name)
    if is_cyrillic_female_name(fn) or is_latin_female_name(fn):
        return "female_first_name"
    ln = (lot.last_name or "").strip().lower()
    if ln.endswith(("овна", "евна", "ична")):
        return "patronymic"
    if _username_female_name(lot.seller):
        return "female_username_token"
    if _username_female(lot.seller):
        return "female_username_hint"
    return "none"


def girl_signal_list(lot: Lot) -> list[str]:
    """Те же веса, что female_score — только для лога, фильтр не трогаем."""
    import re

    signals: list[str] = []
    fn = _first_token(lot.first_name)
    ln = (lot.last_name or "").strip().lower()
    scored = 0
    if fn and (is_cyrillic_female_name(fn) or is_latin_female_name(fn)):
        signals.append("name:+5")
        scored += 5
    if ln.endswith(("овна", "евна", "ична")):
        signals.append("patronymic:+4")
        scored += 4
    if _username_female_name(lot.seller):
        signals.append("username_name:+5")
        scored += 5
    elif _username_female(lot.seller):
        signals.append("username_hint:+3")
        scored += 3
    blob = _blob(lot)
    if _FEMALE_HINT_RE.search(blob):
        signals.append("bio_female:+3")
        scored += 3
    if _FEMALE_EMOJI_RE.search(blob) or _FEMALE_EMOJI_RE.search(lot.first_name or ""):
        signals.append("emoji:+2")
        scored += 2
    if lot.emoji_status and _FEMALE_EMOJI_RE.search(lot.emoji_status):
        signals.append("emoji_status:+1")
        scored += 1
    if lot.personal_channel and _FEMALE_HINT_RE.search(lot.personal_channel):
        signals.append("channel:+1")
        scored += 1
    gifts = (lot.gifts_text or "").lower()
    if gifts:
        words = set(re.findall(r"[a-zа-яё]{3,}", gifts))
        if words & _FEMALE_GIFTS:
            signals.append("gifts:+2")
            scored += 2
    stories = (lot.stories_text or "").lower()
    if stories and (_FEMALE_HINT_RE.search(stories) or _FEMALE_EMOJI_RE.search(stories)):
        signals.append("stories:+2")
        scored += 2
    if lot.has_photo:
        if scored > 0:
            signals.append("photo:+1")
        else:
            signals.append("photo:flag_only")
    else:
        signals.append("photo:none")
    if lot.stories_text:
        if not any(s.startswith("stories:") for s in signals):
            signals.append("stories:no_signal")
    else:
        signals.append("stories:unavailable")
    if lot.personal_channel:
        if not any(s.startswith("channel:") for s in signals):
            signals.append("channel:no_signal")
    else:
        signals.append("channel:unavailable")
    return signals

def girl_reject_code(lot: Lot) -> str:
    """Код girl-reject. Порог/логика = female_reason (без изменений)."""
    if looks_male(lot):
        return "male"
    identity = has_female_identity(lot)
    score = female_score(lot)
    require_id = bool(getattr(config, "GIRL_REQUIRE_IDENTITY", True))
    min_score = int(getattr(config, "GIRL_MIN_SCORE", 5))
    if require_id and not identity:
        return "no_identity"
    if score < min_score:
        return "score_lt_min"
    return "ok"


def girl_forensics(lot: Lot) -> dict[str, Any]:
    score = female_score(lot)
    identity = has_female_identity(lot)
    reason = female_reason(lot)
    code = girl_reject_code(lot) if reason else "ok"
    return {
        "score": score,
        "identity": identity,
        "identity_type": identity_type(lot),
        "signals": girl_signal_list(lot),
        "reject_reason": code if reason else "ok",
        "filter_reason": reason or "ok",
        "male": looks_male(lot),
        "male_reason": male_diag_reason(lot),
        "ru": is_russian(lot),
        "ru_why": russian_why(lot),
        "ru_reject_code": (
            russian_reject_code(lot) if is_russian(lot) is False else ""
        ),
    }


def log_girl_forensics(lot: Lot, *, passed: bool) -> None:
    fx = girl_forensics(lot)
    sig = ",".join(fx["signals"][:12]) or "—"
    tag = "GIRL_PASS" if passed else "GIRL_REJECT"
    logger.info(
        "%s score=%s identity=%s identity_type=%s signals=%s reason=%s",
        tag,
        fx["score"],
        str(fx["identity"]).lower(),
        fx["identity_type"],
        sig,
        fx["reject_reason"],
    )


def log_ru_forensics(lot: Lot, *, passed: bool | None) -> None:
    if passed is True:
        logger.info("RU pass reason=%s", russian_why(lot).split(" ", 1)[0])
        return
    if passed is None:
        logger.info("RU incomplete")
        return
    code = russian_reject_code(lot)
    logger.info("RU reject reason=%s", code)


def log_male_forensics(lot: Lot, *, rejected: bool) -> None:
    if rejected:
        logger.info("MALE reject reason=%s", male_diag_reason(lot))
    else:
        logger.info("MALE pass")


class Diagnostics:
    """Кумулятивные + rolling метрики для /status и SCAN-логов."""

    def __init__(self) -> None:
        self.scan_rounds: deque[dict[str, Any]] = deque(maxlen=SCAN_ROUNDS_KEEP)
        self.scan_ms_samples: deque[float] = deque(maxlen=MS_SAMPLES_KEEP)
        self.enrich_ms_samples: deque[float] = deque(maxlen=MS_SAMPLES_KEEP)
        self.detections: deque[dict[str, Any]] = deque(maxlen=DETECT_KEEP)

        self.scan_floodwait_count = 0
        self.scan_floodwait_seconds = 0.0
        self.enrich_floodwait_count = 0
        self.enrich_floodwait_seconds = 0.0
        self.send_floodwait_count = 0
        self.send_floodwait_seconds = 0.0

        self.scan_timeout_count = 0
        self.enrich_timeout_count = 0

        self.enrich_count = 0
        self.enrich_success = 0
        self.enrich_failed = 0

        self.username_from_page = 0
        self.username_from_resale_user = 0
        self.username_from_get_entity = 0
        self.username_from_full_user = 0
        self.username_from_unique_gift = 0
        self.username_unknown = 0
        self.username_checked = 0

        self.ru_reject_foreign_lang = 0
        self.ru_reject_no_cyrillic = 0
        self.ru_reject_no_russian_signal = 0
        self.ru_reject_other = 0

        self.girl_pass = 0
        self.girl_reject = 0
        self.girl_identity_true = 0
        self.girl_identity_false = 0
        self.girl_reject_no_identity = 0
        self.girl_reject_score_lt_min = 0
        self.girl_reject_male = 0
        self.girl_reject_other = 0

        self.detection_latency_unknown = 0
        self.detection_latency_known = 0
        self.detections_recorded = 0

        self.last_round: dict[str, Any] = {}
        self._rpc_kind = "scan"

    def note_flood(self, kind: str, seconds: float) -> None:
        sec = float(seconds)
        if kind == "enrich":
            self.enrich_floodwait_count += 1
            self.enrich_floodwait_seconds += sec
        elif kind == "send":
            self.send_floodwait_count += 1
            self.send_floodwait_seconds += sec
        else:
            self.scan_floodwait_count += 1
            self.scan_floodwait_seconds += sec

    def note_timeout(self, kind: str) -> None:
        if kind == "enrich":
            self.enrich_timeout_count += 1
        else:
            self.scan_timeout_count += 1

    def record_scan_round(self, info: dict[str, Any]) -> None:
        self.scan_rounds.append(dict(info))
        self.last_round = dict(info)
        ms = float(info.get("round_ms") or 0)
        if ms > 0:
            self.scan_ms_samples.append(ms)
        logger.info(
            "SCAN pass=%s collections=%s round_ms=%s api_fetch=%s "
            "fresh=%s in_range=%s queued=%s "
            "dup_seller=%s dup_listing=%s "
            "flood_wait=%s flood_s=%.1f timeouts=%s "
            "ok=%s fail=%s",
            info.get("pass"),
            info.get("collections_checked"),
            int(info.get("round_ms") or 0),
            info.get("api_fetch_count"),
            info.get("fresh_detected"),
            info.get("found_in_range"),
            info.get("queued"),
            info.get("duplicate_seller"),
            info.get("duplicate_listing"),
            info.get("flood_wait_count"),
            float(info.get("flood_wait_seconds") or 0),
            info.get("timeout_count"),
            info.get("collections_success"),
            info.get("collections_failed"),
        )

    def record_detection(
        self,
        lot: Lot,
        *,
        pass_no: int,
    ) -> None:
        detected_at = float(getattr(lot, "discovered_at", 0) or time.time())
        created = getattr(lot, "listing_created_at", None)
        latency: float | None
        if created is not None:
            try:
                latency = detected_at - float(created)
                self.detection_latency_known += 1
            except (TypeError, ValueError):
                latency = None
                self.detection_latency_unknown += 1
        else:
            latency = None
            self.detection_latency_unknown += 1
        self.detections_recorded += 1
        row = {
            "detected_at": detected_at,
            "collection": lot.collection_id,
            "listing_id": lot.id,
            "discovery_round": pass_no,
            "listing_created_at": created,
            "detection_latency": latency,
        }
        self.detections.append(row)
        lat_s = "UNKNOWN" if latency is None else f"{latency:.2f}s"
        logger.info(
            "DETECT id=%s coll=%s round=%s detected_at=%.0f "
            "listing_created_at=%s latency=%s",
            (lot.slug or lot.id)[:40],
            lot.collection_id,
            pass_no,
            detected_at,
            created if created is not None else "None",
            lat_s,
        )

    def record_enrich(self, ms: float, *, ok: bool) -> None:
        self.enrich_count += 1
        if ok:
            self.enrich_success += 1
        else:
            self.enrich_failed += 1
        if ms >= 0:
            self.enrich_ms_samples.append(float(ms))

    def record_username(self, lot: Lot, *, had_before_enrich: bool) -> None:
        self.username_checked += 1
        if had_before_enrich and lot.seller:
            self.username_from_page += 1
            src = getattr(lot, "username_source", "") or "resale_user"
            if src == "resale_user":
                self.username_from_resale_user += 1
            return
        if not lot.seller:
            self.username_unknown += 1
            return
        src = getattr(lot, "username_source", "") or ""
        if src == "resale_user":
            self.username_from_resale_user += 1
        elif src == "get_entity":
            self.username_from_get_entity += 1
        elif src == "full_user":
            self.username_from_full_user += 1
        elif src == "unique_gift":
            self.username_from_unique_gift += 1
        elif src == "page":
            self.username_from_page += 1
        else:
            self.username_unknown += 1

    def record_ru_reject(self, lot: Lot) -> None:
        code = russian_reject_code(lot)
        if code == "foreign_lang":
            self.ru_reject_foreign_lang += 1
        elif code == "no_cyrillic":
            self.ru_reject_no_cyrillic += 1
        elif code == "no_russian_signal":
            self.ru_reject_no_russian_signal += 1
        else:
            self.ru_reject_other += 1

    def record_girl_outcome(self, lot: Lot, *, passed: bool) -> None:
        fx = girl_forensics(lot)
        if fx["identity"]:
            self.girl_identity_true += 1
        else:
            self.girl_identity_false += 1
        if passed:
            self.girl_pass += 1
            return
        self.girl_reject += 1
        code = fx["reject_reason"]
        if code == "no_identity":
            self.girl_reject_no_identity += 1
        elif code == "score_lt_min":
            self.girl_reject_score_lt_min += 1
        elif code == "male":
            self.girl_reject_male += 1
        else:
            self.girl_reject_other += 1

    def scan_p50(self) -> float | None:
        return percentile(list(self.scan_ms_samples), 50)

    def scan_p95(self) -> float | None:
        return percentile(list(self.scan_ms_samples), 95)

    def enrich_p50(self) -> float | None:
        return percentile(list(self.enrich_ms_samples), 50)

    def enrich_p95(self) -> float | None:
        return percentile(list(self.enrich_ms_samples), 95)

    def status_lines(self) -> list[str]:
        last = self.last_round or {}
        last_ms = last.get("round_ms")
        lines = [
            f"scan round: {_fmt_ms(float(last_ms) if last_ms is not None else None)}",
            f"scan p50: {_fmt_ms(self.scan_p50())} · p95: {_fmt_ms(self.scan_p95())}",
            (
                f"last round: pass={last.get('pass', '—')} "
                f"fresh={last.get('fresh_detected', 0)} "
                f"in_range={last.get('found_in_range', 0)} "
                f"queued={last.get('queued', 0)} "
                f"flood={last.get('flood_wait_count', 0)} "
                f"to={last.get('timeout_count', 0)}"
            ),
            (
                f"detection_latency: UNKNOWN={self.detection_latency_unknown} "
                f"known={self.detection_latency_known} "
                f"(API listing time: none)"
            ),
            (
                f"RU reject: foreign_lang={self.ru_reject_foreign_lang} "
                f"no_cyrillic={self.ru_reject_no_cyrillic} "
                f"no_russian_signal={self.ru_reject_no_russian_signal} "
                f"other={self.ru_reject_other}"
            ),
            (
                f"girl diagnostics: pass={self.girl_pass} reject={self.girl_reject} "
                f"identity={self.girl_identity_true}/{self.girl_identity_true + self.girl_identity_false} "
                f"no_identity={self.girl_reject_no_identity} "
                f"score_lt_5={self.girl_reject_score_lt_min}"
            ),
            (
                f"username: page={self.username_from_page} "
                f"resale={self.username_from_resale_user} "
                f"entity={self.username_from_get_entity} "
                f"full={self.username_from_full_user} "
                f"unique={self.username_from_unique_gift} "
                f"unknown={self.username_unknown} "
                f"(n={self.username_checked})"
            ),
            (
                f"enrich: n={self.enrich_count} ok={self.enrich_success} "
                f"fail={self.enrich_failed} "
                f"to={self.enrich_timeout_count} "
                f"flood={self.enrich_floodwait_count} "
                f"p50={_fmt_ms(self.enrich_p50())} "
                f"p95={_fmt_ms(self.enrich_p95())}"
            ),
            (
                f"floodwait: scan={self.scan_floodwait_count}/{self.scan_floodwait_seconds:.0f}s "
                f"enrich={self.enrich_floodwait_count}/{self.enrich_floodwait_seconds:.0f}s "
                f"send={self.send_floodwait_count}/{self.send_floodwait_seconds:.0f}s"
            ),
        ]
        return lines
