"""Юнит-тесты ультра-лох фильтра (без Telegram)."""

from __future__ import annotations

import time

from market import Lot
from tracker import filter_for_post, passes_loh_filter


def _lot(**kw) -> Lot:
    base = dict(
        id="x-1",
        title="Gift",
        number=1,
        stars=1000.0,
        slug="g-1",
        seller="nubik",
        seller_id=42,
        free_dm=True,
        account_level=0,
        is_premium=False,
        gifts_count=0,
        has_photo=False,
        has_personal_channel=False,
        about="",
        lang_code="ru",
    )
    base.update(kw)
    return Lot(**base)


def test_passes_loh_filter_ultra() -> None:
    assert passes_loh_filter(_lot(), max_gifts=1, max_level=0) is None
    assert passes_loh_filter(_lot(is_premium=True), max_gifts=1, max_level=0) == "premium"
    assert passes_loh_filter(_lot(gifts_count=2), max_gifts=1, max_level=0) == "pro"
    assert passes_loh_filter(_lot(gifts_count=None), max_gifts=1, max_level=0) == "pro"
    assert passes_loh_filter(_lot(account_level=1), max_gifts=1, max_level=0) == "level"
    assert passes_loh_filter(_lot(has_photo=True), max_gifts=1, max_level=0) == "pro"
    assert passes_loh_filter(_lot(about="hi"), max_gifts=1, max_level=0) == "pro"


def test_filter_for_post_loh_mode() -> None:
    now = time.time()
    lots = [
        _lot(id="ok", seller="a", first_name="Аня"),
        _lot(id="prem", seller="b", is_premium=True),
        _lot(id="pro", seller="c", gifts_count=5),
        _lot(id="lvl", seller="d", account_level=2),
    ]
    out, stats = filter_for_post(
        lots,
        {},
        now=now,
        strict_ru=False,
        loh_mode=True,
        persona_mode=False,
        max_gifts_count=1,
        max_account_level=0,
    )
    assert len(out) == 1
    assert out[0].id == "ok"
    assert stats["premium"] == 1
    assert stats["pro"] == 1
    assert stats["level"] == 1


if __name__ == "__main__":
    test_passes_loh_filter_ultra()
    test_filter_for_post_loh_mode()
    print("all ok")
