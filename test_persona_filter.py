"""Только девушки: обычные имена + позорный/кринж профиль."""

from __future__ import annotations

from market import Lot
from tracker import (
    _looks_female,
    _looks_male,
    is_ordinary_girl_name,
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


def test_ordinary_names() -> None:
    for name in ("Лера", "Катя", "Настюха", "Маша", "Даша", "Юля"):
        lot = _lot(first_name=name)
        assert is_ordinary_girl_name(lot), name
        assert _looks_female(lot), name
        assert passes_persona_filter(lot) is None, name


def test_cringe_girl() -> None:
    lot = _lot(first_name="Аня", seller="princess_xxx", has_photo=False)
    assert _looks_female(lot)
    assert passes_persona_filter(lot) is None


def test_male_skip() -> None:
    lot = _lot(first_name="Вася", seller="biker99", about="мото")
    assert _looks_male(lot)
    assert passes_persona_filter(lot) == "persona"


def test_unknown_skip() -> None:
    lot = _lot(first_name="Xy", seller="crypto_king")
    assert seller_persona(lot) is None
    assert passes_persona_filter(lot) == "persona"


if __name__ == "__main__":
    test_ordinary_names()
    test_cringe_girl()
    test_male_skip()
    test_unknown_skip()
    print("all ok")
