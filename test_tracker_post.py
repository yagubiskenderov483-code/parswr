"""RU-фильтр не должен глушить все лоты: python3 test_tracker_post.py"""

from __future__ import annotations

import json
import time
from pathlib import Path

from market import Lot, MarketPriceBook, is_clean_female_profile, is_russian_lot
from tracker import filter_for_post
from tracker_filters import FILTER_SCHEMA, migrate_legacy_filters


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
    """Латинский ник без lang_code — неизвестно, не режем (простой режим)."""
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
    """Латинский ник при RU=да проходит как «неизвестно» — выдача не глохнет."""
    lot = _lot()
    out, stats = _filter([lot])
    assert stats["non_ru"] == 0
    assert stats["unknown_ru"] == 1
    assert len(out) == 1


def test_russian_woman_latin_nick_passes_ru() -> None:
    lot = _lot(first_name="Мария", seller="cryptogifts", lang_code="")
    assert is_russian_lot(lot) is True
    out, stats = _filter([lot])
    assert stats["non_ru"] == 0
    assert len(out) == 1


def test_filter_skips_arabic_seller() -> None:
    lot = _lot(first_name="محمد")
    out, stats = _filter([lot])
    assert stats["non_ru"] == 1
    assert out == []


def test_filter_skips_paid_dm() -> None:
    lot = _lot(first_name="Мария", seller="mariagifts", free_dm=False, paid_dm_stars=50)
    out, stats = _filter([lot])
    assert stats["paid"] == 1
    assert out == []


def test_filter_skips_high_level() -> None:
    lot = _lot(first_name="Мария", seller="mariagifts", account_level=5)
    out, stats = _filter([lot])
    assert stats["level"] == 1
    assert out == []


def test_filter_allows_unknown_level() -> None:
    lot = _lot(first_name="Мария", seller="mariagifts", account_level=None)
    out, stats = _filter([lot])
    assert stats["level"] == 0
    assert len(out) == 1


def _filter_strict(lots: list[Lot], book: MarketPriceBook | None = None):
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
        price_book=book,
    )


def test_overprice_300_listed_at_10k() -> None:
    """Нищий гифт коллекции ~300⭐, выставили за 10к — не постим."""
    book = MarketPriceBook()
    book.set_floor(["desk calendar", "cid:99"], 300.0)
    lot = _lot(
        stars=10000.0,
        title="Desk Calendar",
        collection_id=99,
        first_name="Мария",
        seller="mariagifts",
        about="привет",
    )
    assert book.is_fair_price(lot) is False
    out, stats = _filter_strict([lot], book)
    assert stats["overprice"] == 1
    assert out == []


def test_fair_listing_near_collection_floor() -> None:
    book = MarketPriceBook()
    book.set_floor(["desk calendar"], 8000.0)
    lot = _lot(
        stars=9000.0,
        title="Desk Calendar",
        first_name="Мария",
        seller="mariagifts",
        about="привет",
    )
    assert book.is_fair_price(lot) is True
    out, stats = _filter_strict([lot], book)
    assert stats["overprice"] == 0
    assert len(out) == 1


def test_telegram_value_blocks_dump_without_floor() -> None:
    book = MarketPriceBook()
    lot = _lot(
        stars=10000.0,
        telegram_value=320.0,
        title="Cheap Gift",
        first_name="Мария",
        seller="mariagifts",
        about="привет",
    )
    assert book.is_fair_price(lot) is False
    out, stats = _filter_strict([lot], book)
    assert stats["overprice"] == 1


def test_migrate_schema4_file_upgrades() -> None:
    from tracker_filters import ensure_default_filters, load_filters, migrate_legacy_filters

    class Cfg:
        pass

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "tracker_filters.json"
        path.write_text(
            json.dumps(
                {
                    "filter_schema": 4,
                    "female_only": False,
                    "strict_fair_price": False,
                    "min_stars": 5000,
                    "max_stars": 25000,
                }
            ),
            encoding="utf-8",
        )
        ensure_default_filters(path)
        migrated = migrate_legacy_filters(load_filters(path))
        assert migrated["filter_schema"] == FILTER_SCHEMA
        assert migrated["female_only"] is True
        assert migrated["strict_fair_price"] is False
        assert migrated["min_stars"] == 5000.0
        assert migrated["max_stars"] == 25000.0


def test_female_skips_boys() -> None:
    lot = _lot(first_name="Никита", seller="nikitagifts")
    assert is_clean_female_profile(lot) is False
    out, stats = _filter_strict([lot])
    assert stats["not_female"] == 1
    assert out == []


def test_female_keeps_maria() -> None:
    lot = _lot(first_name="Мария", seller="mariagifts", about="привет")
    assert is_clean_female_profile(lot) is True
    out, stats = _filter_strict([lot])
    assert stats["not_female"] == 0
    assert len(out) == 1


def test_empty_profile_blocked() -> None:
    """Пустое имя + нет био/канала — не постим (иностранцы-пустышки)."""
    lot = _lot(first_name="", seller="nftgifts2024", seller_id=222, about="")
    assert is_clean_female_profile(lot) is False
    out, stats = _filter_strict([lot])
    assert stats["not_female"] == 1
    assert stats["empty_profile"] == 1
    assert out == []


def test_bio_or_channel_passes() -> None:
    bio = _lot(first_name="", seller="anngifts", seller_id=331, about="живу в спб")
    ch = _lot(
        first_name="",
        seller="anngifts2",
        seller_id=332,
        about="",
        personal_channel="12345",
    )
    link = _lot(
        first_name="",
        seller="anngifts3",
        seller_id=333,
        about="канал t.me/annachat",
    )
    for lot in (bio, ch, link):
        assert is_clean_female_profile(lot) is True, lot.seller
        out, stats = _filter_strict([lot])
        assert stats["not_female"] == 0, lot.seller
        assert len(out) == 1, lot.seller


def test_simple_mode_neutral_lot_passes() -> None:
    """Режим по умолчанию (без girls-only и рынка): нейтральный лот постится."""
    lot = _lot(first_name="", seller="nftgifts2024", seller_id=222)
    out, stats = filter_for_post(
        [lot],
        {},
        now=time.time(),
        strict_ru=True,
        strict_free=False,
        max_account_level=2,
        max_gifts=20,
        female_only=False,
        strict_fair_price=False,
    )
    assert len(out) == 1


def test_boys_still_blocked_in_soft_mode() -> None:
    """Мягкий режим всё равно режет мужиков: Dima, Vanya, Никита."""
    for fn, nick in (("Dima", "dima123"), ("Vanya", "vanya1"), ("Никита", "nik1")):
        lot = _lot(first_name=fn, seller=nick, seller_id=300)
        assert is_clean_female_profile(lot) is False, fn
        out, stats = _filter_strict([lot])
        assert stats["not_female"] == 1, fn
        assert out == [], fn


def test_latin_female_name_passes() -> None:
    """Kristina с кириллицей в bio — русская девочка, проходит."""
    lot = _lot(
        first_name="Kristina",
        seller="kris2024",
        seller_id=224,
        about="привет, продаю подарки",
    )
    assert is_clean_female_profile(lot) is True
    out, stats = _filter_strict([lot])
    assert stats["not_female"] == 0
    assert stats["non_ru"] == 0
    assert len(out) == 1


def test_male_username_blocked() -> None:
    lot = _lot(first_name="", seller="nikita_gifts", seller_id=223, about="привет")
    assert is_clean_female_profile(lot) is False
    out, stats = _filter_strict([lot])
    assert stats["not_female"] == 1
    assert out == []


def test_migrate_schema8_filled_profile_5k25k() -> None:
    """schema<8 → девочки (не пустышки), рынок выкл, 5k–25k."""
    out = migrate_legacy_filters(
        {
            "filter_schema": 7,
            "female_only": True,
            "strict_fair_price": True,
            "min_stars": 2000,
            "max_stars": 5000,
        }
    )
    assert out["filter_schema"] == FILTER_SCHEMA
    assert out["female_only"] is True
    assert out["strict_fair_price"] is False
    assert out["min_stars"] == 5000.0
    assert out["max_stars"] == 25000.0


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
        test_russian_woman_latin_nick_passes_ru,
        test_filter_skips_arabic_seller,
        test_filter_skips_paid_dm,
        test_filter_skips_high_level,
        test_filter_allows_unknown_level,
        test_overprice_300_listed_at_10k,
        test_fair_listing_near_collection_floor,
        test_telegram_value_blocks_dump_without_floor,
        test_migrate_schema4_file_upgrades,
        test_female_skips_boys,
        test_female_keeps_maria,
        test_empty_profile_blocked,
        test_bio_or_channel_passes,
        test_simple_mode_neutral_lot_passes,
        test_boys_still_blocked_in_soft_mode,
        test_latin_female_name_passes,
        test_male_username_blocked,
        test_migrate_schema8_filled_profile_5k25k,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
