"""Только девушки по критериям: имена + позорный профиль. Пацанов нет."""

from __future__ import annotations

from market import Lot
from tracker import (
    is_ordinary_girl_name,
    matches_girl_criteria,
    passes_persona_filter,
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
    for name in ("Лера", "Катя", "Настюха", "Маша", "Даша", "Юля", "Настя"):
        lot = _lot(first_name=name)
        assert is_ordinary_girl_name(lot), name
        assert matches_girl_criteria(lot), name
        assert passes_persona_filter(lot) is None, name


def test_cringe_girl() -> None:
    lot = _lot(first_name="", seller="зайка_princess")
    assert matches_girl_criteria(lot)
    assert passes_persona_filter(lot) is None


def test_male_skip() -> None:
    lot = _lot(first_name="Вася", seller="biker99", about="мото")
    assert not matches_girl_criteria(lot)
    assert passes_persona_filter(lot) == "persona"


def test_random_digits_not_girl() -> None:
    lot = _lot(first_name="Xy", seller="crypto_king99")
    assert not matches_girl_criteria(lot)
    assert passes_persona_filter(lot) == "persona"


def test_nikita_not_girl() -> None:
    lot = _lot(first_name="Никита", seller="nikita")
    assert not matches_girl_criteria(lot)


def test_profile_signals() -> None:
    lot = _lot(
        first_name="Тома",
        has_photo=True,
        has_personal_channel=True,
        has_stories=True,
        gifts_count=2,
        about="🎀",
    )
    assert matches_girl_criteria(lot)


if __name__ == "__main__":
    test_ordinary_names()
    test_cringe_girl()
    test_male_skip()
    test_random_digits_not_girl()
    test_nikita_not_girl()
    test_profile_signals()
    print("all ok")
