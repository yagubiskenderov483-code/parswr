"""RU-фильтр не должен глушить все лоты: python3 test_tracker_post.py"""

from __future__ import annotations

import time

from market import Lot, is_russian_lot
from tracker import filter_for_post


def _lot(**kwargs) -> Lot:
    data = dict(
        id="lot-1",
        title="Desk Calendar",
        number=42,
        stars=8000.0,
        slug="DeskCalendar-42",
        seller="alexgifts",
        seller_id=111,
        first_name="Alex",
        free_dm=True,
        account_level=1,
        gifts_count=6,
    )
    data.update(kwargs)
    return Lot(**data)


def _filter(lots: list[Lot]):
    return filter_for_post(
        lots,
        {},
        now=time.time(),
        strict_ru=True,
        strict_free=False,
        max_account_level=2,
        max_gifts=20,
        female_only=False,
        strict_fair_price=False,
    )


def test_latin_username_is_unknown_not_foreign() -> None:
    """Типичный продавец: латинский ник, без lang_code — не режем как не-RU."""
    lot = _lot()
    assert is_russian_lot(lot) is None


def test_empty_profile_is_unknown() -> None:
    lot = _lot(seller="", seller_id=None, first_name="", last_name="", about="")
    assert is_russian_lot(lot) is None


def test_cyrillic_name_is_ru() -> None:
    lot = _lot(first_name="Иван", last_name="Петров")
    assert is_russian_lot(lot) is True


def test_ru_flag_is_ru() -> None:
    lot = _lot(first_name="Alex", about="from 🇷🇺")
    assert is_russian_lot(lot) is True


def test_lang_ru_is_ru() -> None:
    lot = _lot(lang_code="ru")
    assert is_russian_lot(lot) is True


def test_arabic_name_is_not_ru() -> None:
    lot = _lot(first_name="محمد")
    assert is_russian_lot(lot) is False


def test_lang_ar_is_not_ru() -> None:
    lot = _lot(lang_code="ar", first_name="Alex")
    assert is_russian_lot(lot) is False


def test_saudi_flag_is_not_ru() -> None:
    lot = _lot(about="🇸🇦 gifts")
    assert is_russian_lot(lot) is False


def test_filter_posts_latin_seller_with_strict_ru() -> None:
    """Баг /status: 12 в очереди, 0 в канал — все латинские ники резались как не-RU."""
    lot = _lot()
    out, stats = _filter([lot])
    assert stats["non_ru"] == 0
    assert len(out) == 1
    assert out[0].id == "lot-1"


def test_filter_posts_cyrillic_seller() -> None:
    lot = _lot(first_name="Мария")
    out, stats = _filter([lot])
    assert stats["non_ru"] == 0
    assert len(out) == 1


def test_filter_skips_arabic_seller() -> None:
    lot = _lot(first_name="محمد")
    out, stats = _filter([lot])
    assert stats["non_ru"] == 1
    assert out == []


def test_filter_skips_paid_dm() -> None:
    lot = _lot(free_dm=False, paid_dm_stars=50)
    out, stats = _filter([lot])
    assert stats["paid"] == 1
    assert out == []


def test_filter_skips_high_level() -> None:
    lot = _lot(account_level=5)
    out, stats = _filter([lot])
    assert stats["level"] == 1
    assert out == []


def test_filter_allows_unknown_level() -> None:
    lot = _lot(account_level=None)
    out, stats = _filter([lot])
    assert stats["level"] == 0
    assert len(out) == 1


def main() -> None:
    tests = [
        test_latin_username_is_unknown_not_foreign,
        test_empty_profile_is_unknown,
        test_cyrillic_name_is_ru,
        test_ru_flag_is_ru,
        test_lang_ru_is_ru,
        test_arabic_name_is_not_ru,
        test_lang_ar_is_not_ru,
        test_saudi_flag_is_not_ru,
        test_filter_posts_latin_seller_with_strict_ru,
        test_filter_posts_cyrillic_seller,
        test_filter_skips_arabic_seller,
        test_filter_skips_paid_dm,
        test_filter_skips_high_level,
        test_filter_allows_unknown_level,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
