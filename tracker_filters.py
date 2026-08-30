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
        "min_stars": float(cfg.min_stars),
        "max_stars": float(cfg.max_stars),
        "strict_ru": bool(cfg.strict_ru),
        "strict_free": bool(cfg.strict_free),
        "max_account_level": int(cfg.max_account_level),
        "post_interval": float(cfg.post_interval),
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
    if "post_interval" in data:
        cfg.post_interval = max(1.0, float(data["post_interval"]))


def load_filters_into_config(cfg: Any, path: Path) -> None:
    apply_filters_to_config(cfg, load_filters(path))


def persist_config_filters(cfg: Any, path: Path) -> None:
    save_filters(path, config_to_filters(cfg))


def filters_summary(cfg: Any) -> str:
    rid = current_preset_id(cfg.min_stars, cfg.max_stars)
    preset = _preset_by_id(rid)
    price = preset[1] if preset else f"{int(cfg.min_stars):,}–{int(cfg.max_stars):,}⭐"
    return (
        f"Цена: <b>{price}</b>\n"
        f"RU: <b>{'да' if cfg.strict_ru else 'нет'}</b> · "
        f"ЛС free: <b>{'строго' if cfg.strict_free else 'не платные'}</b>\n"
        f"Level ≤ <b>{int(cfg.max_account_level)}</b> · "
        f"Пост / <b>{int(cfg.post_interval)}</b>с\n"
        f"Профиль: <b>только девочки</b> · без рекламы"
    )
