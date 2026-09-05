"""Instrumentation only: scan/enrich/floodwait/RU/girl/username forensics.

Не меняет бизнес-фильтры и detection. Только counters + structured logs.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
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
    female_confident,
    female_reason,
    female_score,
    girl_reject_reason,
    has_female_identity,
    is_cyrillic_female_name,
    is_latin_female_name,
    is_russian,
    looks_male,
    male_reject_reason,
    russian_why,
)
from floors import listing_price_range
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

_MALE_REASON_KEYS = (
    "male_hint",
    "male_emoji",
    "male_name_translit",
    "male_name",
    "male_username",
    "male_other",
)


def price_reject_side(stars: float) -> str:
    """below | above | other. Не меняет фильтр — только классификация."""
    lo, hi = listing_price_range()
    val = float(stars)
    if val < lo:
        return "below"
    if val > hi:
        return "above"
    return "other"


def nft_reject_details(lot: Lot, max_nfts: int | None = None) -> dict[str, Any]:
    """Разбор NFT-отказа. Логика = passes_nfts, решение фильтра не меняем."""
    limit = int(config.MAX_NFTS if max_nfts is None else max_nfts)
    n = lot.gifts_count
    if n is None:
        return {
            "rejects": False,
            "reason": "unknown_count",
            "nft_count": None,
            "nft_limit": limit,
            "condition": "gifts_count_is_None",
            "passes": None,
        }
    if n > limit:
        return {
            "rejects": True,
            "reason": "count_above_limit",
            "nft_count": int(n),
            "nft_limit": limit,
            "condition": "gifts_count_gt_MAX_NFTS",
            "passes": False,
        }
    return {
        "rejects": False,
        "reason": "within_limit",
        "nft_count": int(n),
        "nft_limit": limit,
        "condition": "gifts_count_lte_MAX_NFTS",
        "passes": True,
    }


def log_nft_reject(lot: Lot, details: dict[str, Any] | None = None) -> None:
    """INFO без username/PII. listing = цена лота, не id."""
    info = details or nft_reject_details(lot)
    floor = getattr(lot, "model_floor", None)
    if floor is None:
        floor_s = "UNKNOWN"
    else:
        try:
            floor_s = str(int(floor)) if float(floor).is_integer() else str(floor)
        except (TypeError, ValueError):
            floor_s = "UNKNOWN"
    mid = getattr(lot, "model_id", None)
    model_s = str(int(mid)) if mid is not None else "UNKNOWN"
    logger.info(
        "NFT_REJECT reason=%s listing=%s floor=%s model=%s owner_known=%s "
        "nft_count=%s nft_limit=%s cond=%s",
        info.get("reason") or "unknown",
        int(lot.stars) if lot.stars is not None else 0,
        floor_s,
        model_s,
        "true" if lot.seller_id is not None else "false",
        info["nft_count"] if info.get("nft_count") is not None else "none",
        info.get("nft_limit", config.MAX_NFTS),
        info.get("condition") or "—",
    )


def girl_reject_code(lot: Lot) -> str:
    """Код girl-reject = girl_reject_reason (тот же gate)."""
    code = girl_reject_reason(lot)
    if code == "ok":
        return "ok"
    if code == "male":
        return "male"
    if code == "ambiguous":
        return "ambiguous"
    return "no_identity"


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
        "female_confident": female_confident(lot),
        "male": looks_male(lot),
        "male_reason": male_reject_reason(lot) or male_diag_reason(lot),
        "girl_reject_reason": girl_reject_reason(lot),
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
        self.username_fallback_attempted = 0
        self.owner_id_known = 0
        self.owner_id_missing = 0
        self.dup_owner_by_id = 0
        self.dup_owner_by_alias = 0
        self.owner_dup_enqueue = 0
        self.owner_dup_post_enrich = 0
        self.owner_dup_send_guard = 0
        self.owner_sent_persisted = 0

        self.listing_checked = 0
        self.listing_price_pass = 0
        self.bad_model_value = 0
        self.owner_duplicate = 0
        self.candidate_model_count = 0
        self.eligible_model_count = 0
        self.model_floor_known = 0
        self.model_floor_unknown = 0
        self.collections_eligible = 0

        self.enqueue_ms_samples: deque[float] = deque(maxlen=MS_SAMPLES_KEEP)
        self.send_ms_samples: deque[float] = deque(maxlen=MS_SAMPLES_KEEP)

        self.ru_reject_foreign_lang = 0
        self.ru_reject_no_cyrillic = 0
        self.ru_reject_no_russian_signal = 0
        self.ru_reject_other = 0

        # Aggregated rejection reasons (instrumentation only — does not change filters)
        self.price_reject_below = 0
        self.price_reject_above = 0
        self.seen_listing = 0
        self.seen_owner = 0
        self.seen_other = 0
        self.male_reject_reasons: Counter[str] = Counter()
        self.dm_paid = 0
        self.level_above_limit = 0
        self.level_unknown = 0
        self.nft_reject_reasons: Counter[str] = Counter()
        self.nft_reject_counts: Counter[int] = Counter()
        self.nft_reject_conditions: Counter[str] = Counter()
        self.nft_limit_last: int | None = None

        self.girl_pass = 0
        self.girl_reject = 0
        self.girl_identity_true = 0
        self.girl_identity_false = 0
        self.girl_reject_no_identity = 0
        self.girl_reject_score_lt_min = 0
        self.girl_reject_male = 0
        self.girl_reject_other = 0
        self.female_pass = 0
        self.female_reject = 0
        self.male_explicit_reject = 0
        self.male_name_reject = 0
        self.male_username_reject = 0
        self.male_bio_reject = 0
        self.ambiguous_gender_reject = 0
        self.no_identity_reject = 0

        self.new_listing_seen = 0
        self.old_listing_seen = 0
        self.genuine_new = 0
        self.unprimed_seed = 0
        self.freshness_forensics: deque[dict[str, Any]] = deque(maxlen=20)
        self.listing_page_depth = 0
        self.listing_page_depth_max = 0
        self.collections_scanned = 0
        self.eligible_collections_scanned = 0
        self.new_candidates_per_collection: Counter[str] = Counter()
        self.owner_sent_total = 0
        self.owner_duplicate_total = 0

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
        if lot.seller_id is not None:
            self.owner_id_known += 1
        else:
            self.owner_id_missing += 1
        if not had_before_enrich:
            self.username_fallback_attempted += 1
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

    def record_owner_dup(self, overlap: list[str] | set[str]) -> None:
        if any(str(x).startswith("id:") for x in overlap):
            self.dup_owner_by_id += 1
        else:
            self.dup_owner_by_alias += 1
        self.owner_duplicate_total += 1

    def note_owner_dup_stage(self, stage: str) -> None:
        if stage == "enqueue":
            self.owner_dup_enqueue += 1
        elif stage == "post_enrich":
            self.owner_dup_post_enrich += 1
        elif stage == "send_guard":
            self.owner_dup_send_guard += 1

    def note_owner_sent(self) -> None:
        self.owner_sent_total += 1
        self.owner_sent_persisted += 1

    def record_scan_discovery(
        self,
        gid: int,
        *,
        new_n: int = 0,
        old_n: int = 0,
        pages: int = 0,
        models: int = 0,
        fresh_candidates: int = 0,
        depths: dict[str, int] | None = None,
        eligible: bool = True,
    ) -> None:
        """Где теряются новые listings: new vs old vs page depth."""
        self.new_listing_seen += int(new_n)
        self.old_listing_seen += int(old_n)
        self.collections_scanned += 1
        if eligible:
            self.eligible_collections_scanned += 1
        if depths:
            try:
                deepest = max(int(x) for x in depths.values())
            except (TypeError, ValueError):
                deepest = 0
            self.listing_page_depth = deepest
            if deepest > self.listing_page_depth_max:
                self.listing_page_depth_max = deepest
        elif pages:
            guess = max(0, int(pages) - 1) * max(1, int(config.PAGE_LIMIT))
            if guess > self.listing_page_depth_max:
                self.listing_page_depth_max = guess
            self.listing_page_depth = max(self.listing_page_depth, guess)
        if fresh_candidates:
            self.note_new_candidates(gid, int(fresh_candidates))
        _ = models  # logged via SCAN pass; kept for call-site compatibility

    def record_freshness_verdict(self, row: dict[str, Any]) -> None:
        self.freshness_forensics.append(dict(row))
        if row.get("genuine_new"):
            self.genuine_new += 1
        if row.get("reason") == "UNPRIMED_SEED":
            self.unprimed_seed += 1

    def freshness_forensics_lines(self) -> list[str]:
        if not self.freshness_forensics:
            return ["FRESHNESS last20: —"]
        lines = ["FRESHNESS last20"]
        for row in list(self.freshness_forensics)[-20:]:
            lines.append(
                "id={listing_id} c={collection_id} m={model_id} "
                "p={listing_price} first={first_seen_at} prev={previous_seen_at} "
                "snap={snapshot_contains_before} seen={seen_contains_before} "
                "page={page_number} off={offset} src={source_request} "
                "reason={reason}".format(
                    listing_id=row.get("listing_id"),
                    collection_id=row.get("collection_id"),
                    model_id=row.get("model_id"),
                    listing_price=row.get("listing_price"),
                    first_seen_at=row.get("first_seen_at"),
                    previous_seen_at=row.get("previous_seen_at"),
                    snapshot_contains_before=row.get("snapshot_contains_before"),
                    seen_contains_before=row.get("seen_contains_before"),
                    page_number=row.get("page_number"),
                    offset=row.get("offset") or "0",
                    source_request=row.get("source_request"),
                    reason=row.get("reason"),
                )
            )
        return lines

    def note_new_candidates(self, collection_id: int, n: int) -> None:
        if n:
            self.new_candidates_per_collection[str(int(collection_id))] += int(n)

    def new_candidates_summary(self) -> str:
        total = int(sum(self.new_candidates_per_collection.values()))
        if not total:
            return "n=0 top=—"
        top = ",".join(
            f"{k}:{v}" for k, v in self.new_candidates_per_collection.most_common(5)
        )
        return f"n={total} top={top}"

    def note_catalog(self, stats: dict[str, Any], collections_eligible: int) -> None:
        self.candidate_model_count = int(stats.get("models_total") or 0)
        self.eligible_model_count = int(stats.get("eligible_model_count") or 0)
        self.model_floor_known = int(stats.get("model_floor_known") or 0)
        self.model_floor_unknown = int(stats.get("model_floor_unknown") or 0)
        self.collections_eligible = int(collections_eligible)

    def record_enqueue_latency(self, lot: Lot) -> None:
        started = float(getattr(lot, "discovered_at", 0) or 0)
        if started <= 0:
            return
        ms = max(0.0, (time.time() - started) * 1000.0)
        self.enqueue_ms_samples.append(ms)

    def record_send_latency(self, lot: Lot) -> None:
        started = float(getattr(lot, "discovered_at", 0) or 0)
        if started <= 0:
            return
        ms = max(0.0, (time.time() - started) * 1000.0)
        self.send_ms_samples.append(ms)

    def enqueue_p50(self) -> float | None:
        return percentile(list(self.enqueue_ms_samples), 50)

    def send_p50(self) -> float | None:
        return percentile(list(self.send_ms_samples), 50)

    def record_price_reject(self, stars: float) -> None:
        side = price_reject_side(stars)
        if side == "below":
            self.price_reject_below += 1
        elif side == "above":
            self.price_reject_above += 1

    def record_seen_reason(self, kind: str) -> None:
        if kind == "listing":
            self.seen_listing += 1
        elif kind == "owner":
            self.seen_owner += 1
        else:
            self.seen_other += 1

    def record_male_reject(self, lot: Lot) -> None:
        code = male_reject_reason(lot) or male_diag_reason(lot)
        if code == "pass":
            code = "male_other"
        self.male_reject_reasons[code] += 1
        if code in {"male_explicit", "male_emoji"}:
            self.male_explicit_reject += 1
        elif code in {"male_name", "male_name_translit"}:
            self.male_name_reject += 1
        elif code == "male_username":
            self.male_username_reject += 1
        elif code in {"male_bio", "male_hint"}:
            self.male_bio_reject += 1
        else:
            self.male_explicit_reject += 1

    def record_dm_reject(self) -> None:
        self.dm_paid += 1

    def record_level_outcome(self, lot: Lot, *, rejected: bool) -> None:
        """Observational. unknown_level does not mean the filter rejected."""
        if rejected:
            self.level_above_limit += 1
            return
        if lot.account_level is None:
            self.level_unknown += 1

    def record_nft_reject(self, lot: Lot) -> None:
        info = nft_reject_details(lot)
        reason = str(info.get("reason") or "other")
        self.nft_reject_reasons[reason] += 1
        n = info.get("nft_count")
        if n is not None:
            self.nft_reject_counts[int(n)] += 1
        limit = int(info.get("nft_limit") or config.MAX_NFTS)
        self.nft_limit_last = limit
        cond = str(info.get("condition") or "other")
        self.nft_reject_conditions[cond] += 1
        log_nft_reject(lot, info)

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
            self.female_pass += 1
            return
        self.girl_reject += 1
        self.female_reject += 1
        code = fx["reject_reason"]
        if code == "no_identity":
            self.girl_reject_no_identity += 1
            self.no_identity_reject += 1
        elif code == "score_lt_min":
            self.girl_reject_score_lt_min += 1
            self.no_identity_reject += 1
        elif code == "ambiguous":
            self.ambiguous_gender_reject += 1
            self.girl_reject_other += 1
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
                f"MODEL CATALOG models_total={self.candidate_model_count} "
                f"models_eligible={self.eligible_model_count} "
                f"floor_known={self.model_floor_known} "
                f"floor_unknown={self.model_floor_unknown}"
            ),
            (
                f"detection_latency: UNKNOWN={self.detection_latency_unknown} "
                f"known={self.detection_latency_known} "
                f"(API listing time: none)"
            ),
            (
                f"detection_to_enqueue: {_fmt_ms(self.enqueue_p50())} "
                f"detection_to_send: {_fmt_ms(self.send_p50())}"
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
                f"female pass={self.female_pass} reject={self.female_reject} "
                f"male_explicit={self.male_explicit_reject} "
                f"male_name={self.male_name_reject} "
                f"male_username={self.male_username_reject} "
                f"male_bio={self.male_bio_reject} "
                f"ambiguous={self.ambiguous_gender_reject} "
                f"no_identity={self.no_identity_reject}"
            ),
            (
                f"scan new={self.new_listing_seen} old={self.old_listing_seen} "
                f"genuine_new={self.genuine_new} unprimed={self.unprimed_seed} "
                f"depth={self.listing_page_depth_max} "
                f"cols={self.collections_scanned} "
                f"elig={self.eligible_collections_scanned} "
                f"cand={self.new_candidates_summary()}"
            ),
            (
                f"owner sent={self.owner_sent_total} "
                f"dup={self.owner_duplicate_total} "
                f"enq={self.owner_dup_enqueue} "
                f"enrich={self.owner_dup_post_enrich} "
                f"guard={self.owner_dup_send_guard} "
                f"no_id={self.owner_id_missing}"
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
                f"owner_id: known={self.owner_id_known} "
                f"missing={self.owner_id_missing} "
                f"fallback_try={self.username_fallback_attempted} "
                f"dup_id={self.dup_owner_by_id} "
                f"dup_alias={self.dup_owner_by_alias}"
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
        lines.extend(self.freshness_forensics_lines())
        return lines

    def rejection_reason_lines(self) -> list[str]:
        """HTML-safe lines for /status REJECTION REASONS. No raw '<'."""
        male_s = " ".join(
            f"{k}={self.male_reject_reasons.get(k, 0)}" for k in _MALE_REASON_KEYS
        )
        nft_reason_s = (
            " ".join(
                f"{k}={v}"
                for k, v in sorted(self.nft_reject_reasons.items())
            )
            or "none=0"
        )
        if self.nft_reject_counts:
            nft_count_s = ",".join(
                f"{n}x{c}" for n, c in sorted(self.nft_reject_counts.items())
            )
        else:
            nft_count_s = "—"
        nft_limit_s = (
            str(self.nft_limit_last)
            if self.nft_limit_last is not None
            else str(int(config.MAX_NFTS))
        )
        if self.nft_reject_conditions:
            nft_cond_s = ",".join(
                f"{k}={v}"
                for k, v in sorted(self.nft_reject_conditions.items())
            )
        else:
            nft_cond_s = "—"
        return [
            f"price: below={self.price_reject_below} above={self.price_reject_above}",
            (
                f"seen: listing={self.seen_listing} "
                f"owner={self.seen_owner} other={self.seen_other}"
            ),
            f"male: {male_s}",
            (
                f"ru: no_cyrillic={self.ru_reject_no_cyrillic} "
                f"no_russian_signal={self.ru_reject_no_russian_signal} "
                f"foreign={self.ru_reject_foreign_lang} "
                f"other={self.ru_reject_other}"
            ),
            (
                f"girl: no_identity={self.girl_reject_no_identity} "
                f"score_below_threshold={self.girl_reject_score_lt_min} "
                f"male={self.girl_reject_male} "
                f"other={self.girl_reject_other} "
                f"female_pass={self.female_pass} female_reject={self.female_reject} "
                f"male_explicit={self.male_explicit_reject} "
                f"male_name={self.male_name_reject} "
                f"male_user={self.male_username_reject} "
                f"male_bio={self.male_bio_reject} "
                f"ambig={self.ambiguous_gender_reject} "
                f"no_id={self.no_identity_reject}"
            ),
            f"dm: paid_dm={self.dm_paid}",
            (
                f"level: above_limit={self.level_above_limit} "
                f"unknown_level={self.level_unknown}"
            ),
            (
                f"nft: {nft_reason_s} nft_count={nft_count_s} "
                f"nft_limit={nft_limit_s} cond={nft_cond_s}"
            ),
        ]
