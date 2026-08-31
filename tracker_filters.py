"""Сохранённые фильтры гифт-трекера (меняются через бота)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRICE_PRESETS: list[tuple[str, str, int, int]] = [
    ("easy", "🟢 2k–5k", 2000, 5000),
    ("wide", "🔵 5k–25k", 5000, 25000),
    ("mid", "🟡 5k–15k", 5000, 15000),
    ("hard", "🔴 15k–30k", 15000, 30000),
    ("impos", "💀 30k–60k", 30000, 60000),
]


def filters_file_path(data_dir: Path) -> Path:
    return data_dir / "tracker_filters.json"


def _preset_by_id(rid: str) -> tuple[str, str, int, int] | None:
    for item in PRICE_PRESETS:
        if item[0] == rid:
            return item
    return None


def current_preset_id(min_stars: float, max_stars: float) -> str:
    for rid, _label, mn, mx in PRICE_PRESETS:
        if abs(min_stars - mn) < 1 and abs(max_stars - mx) < 1:
            return rid
    return ""


def load_filters(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("load tracker filters: %s", exc)
    return {}


def save_filters(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def config_to_filters(cfg: Any) -> dict[str, Any]:
    return {
        "filter_schema": FILTER_SCHEMA,
        "min_stars": float(cfg.min_stars),
        "max_stars": float(cfg.max_stars),
        "strict_ru": bool(cfg.strict_ru),
        "strict_free": bool(cfg.strict_free),
        "max_account_level": int(cfg.max_account_level),
        "max_gifts": int(getattr(cfg, "max_gifts", 5)),
        "post_interval": float(cfg.post_interval),
        "female_only": bool(getattr(cfg, "female_only", True)),
        "strict_fair_price": bool(getattr(cfg, "strict_fair_price", True)),
        "fair_price_ratio": float(getattr(cfg, "fair_price_ratio", 1.55)),
    }


def apply_filters_to_config(cfg: Any, data: dict[str, Any]) -> None:
    if not data:
        return
    if "min_stars" in data:
        cfg.min_stars = float(data["min_stars"])
    if "max_stars" in data:
        cfg.max_stars = float(data["max_stars"])
    if "strict_ru" in data:
        cfg.strict_ru = bool(data["strict_ru"])
    if "strict_free" in data:
        cfg.strict_free = bool(data["strict_free"])
    if "max_account_level" in data:
        cfg.max_account_level = int(data["max_account_level"])
    if "max_gifts" in data:
        cfg.max_gifts = max(1, int(data["max_gifts"]))
    if "post_interval" in data:
        cfg.post_interval = max(0.5, float(data["post_interval"]))
    if "female_only" in data:
        cfg.female_only = bool(data["female_only"])
    if "strict_fair_price" in data:
        cfg.strict_fair_price = bool(data["strict_fair_price"])
    if "fair_price_ratio" in data:
        cfg.fair_price_ratio = max(1.1, float(data["fair_price_ratio"]))


FILTER_SCHEMA = 5

DEFAULT_FILTER_DATA: dict[str, Any] = {
    "filter_schema": FILTER_SCHEMA,
    "min_stars": 5000.0,
    "max_stars": 25000.0,
    "strict_ru": True,
    "strict_free": False,
    "max_account_level": 2,
    "max_gifts": 20,
    "post_interval": 1.5,
    "female_only": True,
    "strict_fair_price": True,
    "fair_price_ratio": 1.55,
}


def ensure_default_filters(path: Path) -> None:
    """Первый запуск или старый schema — прописать актуальные фильтры."""
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            schema = int((raw or {}).get("filter_schema", 0) or 0)
            if schema >= FILTER_SCHEMA:
                return
        except (OSError, ValueError, TypeError):
            pass
    save_filters(path, dict(DEFAULT_FILTER_DATA))


def migrate_legacy_filters(data: dict[str, Any]) -> dict[str, Any]:
    """Старый пресет 2k–5k → 5k–25k; schema<5 → девочки + рынок коллекции."""
    if not data:
        return dict(DEFAULT_FILTER_DATA)
    out = dict(data)
    try:
        mn = float(out.get("min_stars", 0))
        mx = float(out.get("max_stars", 0))
    except (TypeError, ValueError):
        mn = mx = 0.0
    if abs(mn - 2000) < 1 and abs(mx - 5000) < 1:
        out["min_stars"] = 5000.0
        out["max_stars"] = 25000.0
    schema = int(out.get("filter_schema", 0) or 0)
    if schema < 2:
        out["post_interval"] = min(float(out.get("post_interval", 3.0) or 3.0), 1.5)
    if schema < 4:
        out["strict_ru"] = True
        out["max_account_level"] = min(int(out.get("max_account_level", 2) or 2), 2)
        try:
            prev_gifts = int(out.get("max_gifts", 5) or 5)
        except (TypeError, ValueError):
            prev_gifts = 5
        if prev_gifts <= 5:
            out["max_gifts"] = 20
    if schema < FILTER_SCHEMA:
        out["filter_schema"] = FILTER_SCHEMA
        out["female_only"] = True
        out["strict_fair_price"] = True
    return out


def load_filters_into_config(cfg: Any, path: Path) -> None:
    ensure_default_filters(path)
    raw = load_filters(path)
    migrated = migrate_legacy_filters(raw)
    if migrated != raw:
        save_filters(path, migrated)
    apply_filters_to_config(cfg, migrated or dict(DEFAULT_FILTER_DATA))


def persist_config_filters(cfg: Any, path: Path) -> None:
    save_filters(path, config_to_filters(cfg))


def filters_summary(cfg: Any) -> str:
    rid = current_preset_id(cfg.min_stars, cfg.max_stars)
    preset = _preset_by_id(rid)
    price = preset[1] if preset else f"{int(cfg.min_stars):,}–{int(cfg.max_stars):,}⭐"
    female = "девочки" if getattr(cfg, "female_only", False) else "все"
    fair = "да" if getattr(cfg, "strict_fair_price", False) else "нет"
    return (
        f"Цена: <b>{price}</b>\n"
        f"RU: <b>{'да' if cfg.strict_ru else 'нет'}</b> · "
        f"ЛС free: <b>{'строго' if cfg.strict_free else 'не платные'}</b>\n"
        f"Level ≤ <b>{int(cfg.max_account_level)}</b> · "
        f"gifts ≤ <b>{int(getattr(cfg, 'max_gifts', 5))}</b> · "
        f"Пост / <b>{int(cfg.post_interval)}</b>с\n"
        f"Профиль: <b>{female}</b> · рынок коллекции: <b>{fair}</b>"
    )
