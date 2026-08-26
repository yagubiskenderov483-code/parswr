"""Юнит-тесты фильтра «нубов» (без Telegram)."""

from __future__ import annotations

import time

from market import Lot
from tracker import filter_for_post, passes_account_level, passes_noob_seller


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
        account_level=1,
        is_premium=False,
        gifts_count=2,
        lang_code="ru",
    )
    base.update(kw)
    return Lot(**base)


def test_passes_account_level_requires_known_in_noob_mode() -> None:
    lot = _lot(account_level=None)
    assert passes_account_level(lot, 2, require_known=False) is True
    assert passes_account_level(lot, 2, require_known=True) is False
    assert passes_account_level(_lot(account_level=3), 2) is False
    assert passes_account_level(_lot(account_level=-1), 2) is True


def test_passes_noob_seller() -> None:
    assert passes_noob_seller(_lot(), max_gifts=5) is None
    assert passes_noob_seller(_lot(is_premium=True), max_gifts=5) == "premium"
    assert passes_noob_seller(_lot(gifts_count=20), max_gifts=5) == "pro"
    assert passes_noob_seller(_lot(gifts_count=None), max_gifts=5) is None


def test_filter_for_post_noob_mode() -> None:
    now = time.time()
    lots = [
        _lot(id="ok", seller="a"),
        _lot(id="prem", seller="b", is_premium=True),
        _lot(id="pro", seller="c", gifts_count=99),
        _lot(id="lvl", seller="d", account_level=5),
    ]
    out, stats = filter_for_post(
        lots,
        {},
        now=now,
        strict_ru=False,
        noob_mode=True,
        max_gifts_count=5,
        max_account_level=2,
    )
    assert len(out) == 1
    assert out[0].id == "ok"
    assert stats["premium"] == 1
    assert stats["pro"] == 1
    assert stats["level"] == 1


if __name__ == "__main__":
    test_passes_account_level_requires_known_in_noob_mode()
    test_passes_noob_seller()
    test_filter_for_post_noob_mode()
    print("all ok")