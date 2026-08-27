"""Тесты персон: девочки + тупые пацаны."""

from __future__ import annotations

from market import Lot
from tracker import (
    _looks_female,
    _looks_male,
    passes_persona_filter,
    seller_persona,
)


def _lot(**kw) -> Lot:
    base = dict(
        id="x",
        title="G",
        number=1,
        stars=1000.0,
        slug="g",
        seller="user",
        seller_id=1,
        is_premium=False,
        account_level=0,
        gifts_count=0,
        has_photo=False,
        about="",
    )
    base.update(kw)
    return Lot(**base)


def test_female() -> None:
    lot = _lot(first_name="Аня", seller="anya_girl")
    assert _looks_female(lot)
    assert seller_persona(lot) == "female"
    assert passes_persona_filter(lot) is None


def test_male() -> None:
    lot = _lot(first_name="Вася", seller="biker99", about="мото")
    assert _looks_male(lot)
    assert seller_persona(lot) == "male"
    assert passes_persona_filter(lot) is None


def test_unknown_persona_skip() -> None:
    lot = _lot(first_name="Xy", seller="crypto_king")
    assert seller_persona(lot) is None
    assert passes_persona_filter(lot) == "persona"


def test_premium_skip() -> None:
    lot = _lot(first_name="Аня", is_premium=True)
    assert passes_persona_filter(lot) == "premium"


if __name__ == "__main__":
    test_female()
    test_male()
    test_unknown_persona_skip()
    test_premium_skip()
    print("all ok")
