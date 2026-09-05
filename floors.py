"""Кэш рыночного floor модели/варианта. Только факты из GetResaleStarGifts.

LISTING PRICE — цена конкретного NFT.
MODEL FLOOR — минимальная цена, реально увиденная у этой модели в
price-sorted resale (первая/наименьшая цена листинга модели).

Если API не показал ни одного листинга модели — floor = UNKNOWN.
Никаких сторонних баз и выдуманных цен.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger("floors")


def model_cache_key(gift_id: int, model_id: int) -> str:
    return f"{int(gift_id)}:{int(model_id)}"


def extract_model_attr(attr: Any) -> tuple[str, int | None]:
    """Имя + document id модели. IdModel-фильтр и не-model атрибуты — пусто."""
    cls = type(attr).__name__.lower()
    if "model" not in cls:
        return "", None
    if "idmodel" in cls or "counter" in cls:
        return "", None
    name = str(getattr(attr, "name", "") or getattr(attr, "text", "") or "")
    mid = getattr(attr, "document_id", None)
    if mid is None:
        doc = getattr(attr, "document", None)
        if doc is not None:
            mid = getattr(doc, "id", None)
    try:
        mid_i = int(mid) if mid is not None else None
    except (TypeError, ValueError):
        return name, None
    if mid_i is None or mid_i <= 0:
        return name, None
    return name, mid_i


def listing_price_range(
    min_stars: float | None = None,
    max_stars: float | None = None,
    tolerance: float | None = None,
) -> tuple[float, float]:
    lo = float(config.MIN_STARS if min_stars is None else min_stars)
    hi = float(config.MAX_STARS if max_stars is None else max_stars)
    tol = float(
        config.LISTING_PRICE_TOLERANCE if tolerance is None else tolerance
    )
    return lo - tol, hi + tol


def listing_price_ok(stars: float) -> bool:
    lo, hi = listing_price_range()
    return lo <= float(stars) <= hi


def model_floor_verdict(floor: float | None) -> str:
    """ok | unknown | bad_model_value | above_max. None → unknown, не выдумываем."""
    if floor is None:
        return "unknown"
    try:
        val = float(floor)
    except (TypeError, ValueError):
        return "unknown"
    if val < float(config.MIN_MODEL_FLOOR):
        return "bad_model_value"
    if val > float(config.MAX_MODEL_FLOOR):
        return "above_max"
    return "ok"


def listing_and_floor_reason(*, listing_stars: float, floor: float | None) -> str:
    """Пустая строка = кандидат. Иначе код отказа."""
    if not listing_price_ok(listing_stars):
        return "цена"
    verdict = model_floor_verdict(floor)
    if verdict == "ok":
        return ""
    if verdict == "bad_model_value":
        return "REJECT_BAD_MODEL_VALUE"
    if verdict == "above_max":
        return "floor выше макс"
    # UNKNOWN не выдумываем цену и не режем: listing уже в 5k–25k.
    return ""


class FloorCatalog:
    """persist: {gift_id:model_id → floor | null}."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.models: dict[str, dict[str, Any]] = {}
        self.updated_at: float = 0.0

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        try:
            self.updated_at = float(data.get("updated_at") or 0)
        except (TypeError, ValueError):
            self.updated_at = 0.0
        raw = data.get("models") or {}
        if isinstance(raw, dict):
            self.models = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"updated_at": self.updated_at, "models": self.models}
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)

    def is_fresh(self, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        if self.updated_at <= 0:
            return False
        return (ts - self.updated_at) < float(config.FLOOR_CACHE_TTL)

    def _row(self, gift_id: int, model_id: int, name: str = "") -> dict[str, Any]:
        key = model_cache_key(gift_id, model_id)
        row = self.models.get(key)
        if row is None:
            row = {
                "gift_id": int(gift_id),
                "model_id": int(model_id),
                "name": name or "",
                "floor": None,
                "ts": 0.0,
            }
            self.models[key] = row
        elif name and not row.get("name"):
            row["name"] = name
        return row

    def observe_model(self, gift_id: int, model_id: int, name: str = "") -> None:
        """Модель существует в коллекции. Floor не трогаем."""
        self._row(gift_id, model_id, name)

    def observe_floor(
        self, gift_id: int, model_id: int, price: float, name: str = ""
    ) -> None:
        """Факт: этот listing модели стоит `price`. Floor = min увиденных цен."""
        try:
            val = float(price)
        except (TypeError, ValueError):
            return
        if val <= 0:
            return
        row = self._row(gift_id, model_id, name)
        prev = row.get("floor")
        try:
            prev_f = float(prev) if prev is not None else None
        except (TypeError, ValueError):
            prev_f = None
        if prev_f is None or val < prev_f:
            row["floor"] = val
            row["ts"] = time.time()
            if name:
                row["name"] = name

    def ingest_result(self, gift_id: int, result: Any, lots: list[Any]) -> None:
        for attr in getattr(result, "attributes", None) or []:
            name, mid = extract_model_attr(attr)
            if mid is not None:
                self.observe_model(gift_id, mid, name)
        for lot in lots or []:
            mid = getattr(lot, "model_id", None)
            if mid is None:
                continue
            try:
                mid_i = int(mid)
            except (TypeError, ValueError):
                continue
            self.observe_floor(
                gift_id,
                mid_i,
                float(getattr(lot, "stars", 0) or 0),
                str(getattr(lot, "model", "") or ""),
            )

    def get_floor(self, gift_id: int | None, model_id: int | None) -> float | None:
        if gift_id is None or model_id is None:
            return None
        row = self.models.get(model_cache_key(int(gift_id), int(model_id)))
        if not row:
            return None
        raw = row.get("floor")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def stats(self) -> dict[str, int]:
        total = len(self.models)
        known = 0
        for row in self.models.values():
            if row.get("floor") is not None:
                known += 1
        eligible = len(self.eligible_keys())
        return {
            "models_total": total,
            "model_floor_known": known,
            "model_floor_unknown": total - known,
            "eligible_model_count": eligible,
            "candidate_model_count": known,
        }

    def eligible_keys(self) -> list[str]:
        out: list[str] = []
        for key, row in self.models.items():
            if model_floor_verdict(row.get("floor")) == "ok":
                out.append(key)
        return out

    def eligible_model_ids(self, gift_id: int) -> list[int]:
        gid = int(gift_id)
        ids: list[int] = []
        seen: set[int] = set()
        for row in self.models.values():
            if int(row.get("gift_id") or 0) != gid:
                continue
            if model_floor_verdict(row.get("floor")) != "ok":
                continue
            try:
                mid = int(row["model_id"])
            except (TypeError, ValueError, KeyError):
                continue
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
        return ids

    def scan_collection_ids(self, all_gift_ids: list[int] | None = None) -> list[int]:
        """Коллекции с ≥1 eligible моделью. Приоритет: floor в listing-диапазоне."""
        listing_lo, listing_hi = listing_price_range()
        by_gid: dict[int, list[float]] = {}
        for row in self.models.values():
            floor = row.get("floor")
            if model_floor_verdict(floor) != "ok":
                continue
            try:
                gid = int(row["gift_id"])
                fl = float(floor)
            except (TypeError, ValueError, KeyError):
                continue
            by_gid.setdefault(gid, []).append(fl)
        allowed = set(int(x) for x in (all_gift_ids or [])) if all_gift_ids else None

        def rank(gid: int) -> tuple[int, float, int]:
            floors = by_gid[gid]
            in_band = any(listing_lo <= f <= listing_hi for f in floors)
            return (0 if in_band else 1, min(floors), gid)

        ids = [g for g in by_gid if allowed is None or g in allowed]
        ids.sort(key=rank)
        return ids
