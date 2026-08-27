"""Тест детекта смены головы коллекции (#1 resale)."""

from __future__ import annotations

import time

from market import Lot
from tracker import _collect_fresh_lot, Config


def _cfg() -> Config:
    return Config(
        api_id=1,
        api_hash="x",
        session_string="",
        bot_token="",
        target_channel="",
        hot_limit=1,
        min_stars=500,
        max_stars=2000,
    )


def _lot(lid: str) -> Lot:
    return Lot(
        id=lid,
        title="G",
        number=1,
        stars=1000.0,
        slug=lid,
        seller="u",
        seller_id=1,
    )


def test_head_change_only() -> None:
    seen: dict[str, float] = {}
    heads: dict[str, str] = {}
    cfg = _cfg()
    now = time.time()

    assert (
        _collect_fresh_lot(_lot("a"), 0, 99, seen, heads, cfg, baseline=False, now=now)
        is not None
    )
    assert heads["99"] == "a"

    # тот же #1 — не новый листинг
    assert (
        _collect_fresh_lot(_lot("a"), 0, 99, seen, heads, cfg, baseline=False, now=now)
        is None
    )

    # сменился #1 — новый
    assert (
        _collect_fresh_lot(_lot("b"), 0, 99, seen, heads, cfg, baseline=False, now=now)
        is not None
    )
    assert heads["99"] == "b"

    # #2 игнор
    assert (
        _collect_fresh_lot(_lot("c"), 1, 99, seen, heads, cfg, baseline=False, now=now)
        is None
    )


if __name__ == "__main__":
    test_head_change_only()
    print("all ok")
