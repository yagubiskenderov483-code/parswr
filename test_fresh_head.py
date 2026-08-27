"""Тест: пост только при смене #1, не при первом скане."""

from __future__ import annotations

import time

from market import Lot
from tracker import HEAD_EMPTY, _collect_fresh_lot, Config


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


def test_no_post_on_first_scan() -> None:
    seen: dict[str, float] = {}
    heads: dict[str, str] = {}
    cfg = _cfg()
    now = time.time()

    # первый скан — часовой #1, не постим
    assert (
        _collect_fresh_lot(_lot("old"), 0, 1, seen, heads, cfg, baseline=False, now=now)
        is None
    )
    assert heads["1"] == "old"
    assert "old" in seen

    # тот же #1
    assert (
        _collect_fresh_lot(_lot("old"), 0, 1, seen, heads, cfg, baseline=False, now=now)
        is None
    )


def test_post_on_head_change() -> None:
    seen: dict[str, float] = {}
    heads: dict[str, str] = {"2": "a"}
    cfg = _cfg()
    now = time.time()

    out = _collect_fresh_lot(_lot("b"), 0, 2, seen, heads, cfg, baseline=False, now=now)
    assert out is not None
    assert out.id == "b"
    assert out.listed_at == now


def test_post_empty_to_new() -> None:
    seen: dict[str, float] = {}
    heads: dict[str, str] = {"3": HEAD_EMPTY}
    cfg = _cfg()
    now = time.time()

    out = _collect_fresh_lot(_lot("n"), 0, 3, seen, heads, cfg, baseline=False, now=now)
    assert out is not None
    assert out.id == "n"


def test_old_lot_rising_to_head_is_not_new() -> None:
    """#2 стал #1 после продажи верха — это не новый листинг."""
    seen: dict[str, float] = {"old2": time.time()}
    heads: dict[str, str] = {"4": "old1"}
    cfg = _cfg()
    now = time.time()
    assert (
        _collect_fresh_lot(_lot("old2"), 0, 4, seen, heads, cfg, baseline=False, now=now)
        is None
    )
    assert heads["4"] == "old2"


def test_error_must_not_look_like_empty_then_post() -> None:
    """После первого успешного скана неизвестный #1 без prev не постим."""
    seen: dict[str, float] = {}
    heads: dict[str, str] = {}
    cfg = _cfg()
    now = time.time()
    # нет prev_head (ошибка API не записала empty) — только запомнить
    assert (
        _collect_fresh_lot(_lot("stale"), 0, 5, seen, heads, cfg, baseline=False, now=now)
        is None
    )
    assert heads["5"] == "stale"
    # тот же лот
    assert (
        _collect_fresh_lot(_lot("stale"), 0, 5, seen, heads, cfg, baseline=False, now=now)
        is None
    )


if __name__ == "__main__":
    test_no_post_on_first_scan()
    test_post_on_head_change()
    test_post_empty_to_new()
    test_old_lot_rising_to_head_is_not_new()
    test_error_must_not_look_like_empty_then_post()
    print("all ok")
