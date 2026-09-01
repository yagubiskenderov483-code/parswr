"""
Полный прогон фильтров как на Bothost: python3 test_tracker_pipeline.py
"""

from __future__ import annotations

import time

from market import Lot, MarketPriceBook, is_russian_lot
from tracker import filter_for_post


def _lot(**kwargs) -> Lot:
    data = dict(
        id="lot-1",
        title="Desk Calendar",
        number=42,
        stars=3500.0,
        slug="DeskCalendar-42",
        seller="gifttrader",
        seller_id=111,
        first_name="",
        free_dm=True,
        account_level=1,
        gifts_count=6,
        collection_id=99,
    )
    data.update(kwargs)
    return Lot(**data)


def _filter_batch(
    lots: list[Lot],
    book: MarketPriceBook | None = None,
) -> tuple[list[Lot], dict[str, int]]:
    return filter_for_post(
        lots,
        {},
        now=time.time(),
        strict_ru=True,
        strict_free=False,
        max_account_level=2,
        max_gifts=20,
        female_only=True,
        strict_fair_price=True,
        price_book=book or MarketPriceBook(),
    )


def test_scenario_23_like_bothost() -> None:
    """19 девочек + 3 фермы + 1 завышение = 19 в канал."""
    book = MarketPriceBook()
    # Пол коллекции ~3.5k — лоты 3.2–4k ок, дамп 10k нет
    book.set_floor(["desk calendar", "cid:99"], 3500.0)
    lots: list[Lot] = []
    names = ("Анна", "Мария", "Елена", "Ольга", "Катя", "Юля", "Даша")
    for i in range(19):
        lots.append(
            _lot(
                id=f"n{i}",
                seller=f"nfttrader{i}",
                seller_id=100 + i,
                first_name=names[i % len(names)],
                stars=3200.0 + i,
            )
        )
    for i in range(3):
        lots.append(
            _lot(
                id=f"farm{i}",
                seller=f"farmer{i}",
                seller_id=200 + i,
                first_name="Анна",
                gifts_count=45,
                stars=4000.0,
            )
        )
    lots.append(
        _lot(
            id="dump",
            stars=10000.0,
            title="Desk Calendar",
            collection_id=99,
            first_name="Мария",
            seller="mariagifts",
            seller_id=3001,
        )
    )
    passed, stats = _filter_batch(lots, book)
    assert stats["not_female"] == 0
    assert stats["many_gifts"] == 3
    assert stats["overprice"] == 1
    assert stats["non_ru"] == 0
    assert len(passed) == 19


def test_typical_post_ready_lot() -> None:
    lot = _lot(
        first_name="Мария",
        seller="mariagifts",
        stars=4200.0,
        lang_code="ru",
    )
    assert is_russian_lot(lot) is True
    passed, stats = _filter_batch([lot])
    assert len(passed) == 1
    assert sum(stats.values()) == 0


def test_latin_foreign_rejected_russian_woman_passes() -> None:
    lot_foreign = _lot(seller="cryptogifts", first_name="", lang_code="")
    assert is_russian_lot(lot_foreign) is False
    passed_f, stats_f = _filter_batch([lot_foreign])
    assert passed_f == []
    assert stats_f["non_ru"] + stats_f["not_female"] >= 1

    lot_ru = _lot(
        seller="cryptogifts",
        first_name="Мария",
        seller_id=999,
        lang_code="en",
    )
    assert is_russian_lot(lot_ru) is True
    passed_r, stats_r = _filter_batch([lot_ru])
    assert stats_r["non_ru"] == 0
    assert len(passed_r) == 1


def test_seven_women_pass_filters() -> None:
    """7 русских девочек с латинскими никами — все в канал."""
    book = MarketPriceBook()
    book.set_floor(["desk calendar", "cid:99"], 3500.0)
    names = ("Мария", "Анна", "Елена", "Катя", "Ольга", "Саша", "Даша")
    lots = [
        _lot(
            id=f"w{i}",
            seller=f"giftgirl{i}",
            seller_id=500 + i,
            first_name=name,
            stars=3800.0,
        )
        for i, name in enumerate(names)
    ]
    passed, stats = _filter_batch(lots, book)
    assert stats["not_female"] == 0
    assert stats["non_ru"] == 0
    assert len(passed) == 7


def test_boy_blocked_girl_passes() -> None:
    boy, stats_b = _filter_batch(
        [_lot(first_name="Никита", seller="nikitagifts", seller_id=1)]
    )
    girl, stats_g = _filter_batch(
        [_lot(first_name="Мария", seller="mariagifts", seller_id=2)]
    )
    assert stats_b["not_female"] == 1
    assert boy == []
    assert len(girl) == 1
    assert stats_g["not_female"] == 0


def test_various_prices_pass_filters() -> None:
    book = MarketPriceBook()
    book.set_floor(["desk calendar", "cid:99"], 3500.0)
    for stars in (2500.0, 4200.0):
        lot = _lot(
            stars=stars,
            seller=f"u{int(stars)}",
            seller_id=int(stars),
            first_name="Анна",
        )
        passed, stats = _filter_batch([lot], book)
        assert len(passed) == 1
        assert stats["overprice"] == 0


def test_telegram_value_dump_blocked() -> None:
    book = MarketPriceBook()
    lot = _lot(
        stars=10000.0,
        telegram_value=280.0,
        first_name="Мария",
        seller="mariagifts",
    )
    passed, stats = _filter_batch([lot], book)
    assert stats["overprice"] == 1
    assert passed == []
    reason = book.overprice_reason(lot)
    assert "завышено" in reason and "280" in reason


def main() -> None:
    tests = [
        test_scenario_23_like_bothost,
        test_typical_post_ready_lot,
        test_latin_foreign_rejected_russian_woman_passes,
        test_seven_women_pass_filters,
        test_boy_blocked_girl_passes,
        test_various_prices_pass_filters,
        test_telegram_value_dump_blocked,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: pipeline {len(tests)} tests")


if __name__ == "__main__":
    main()
