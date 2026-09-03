"""Тесты формата карточки и фильтров: python3 test_tracker.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from filters import filter_lot, is_girl, is_russian, looks_male
from market import (
    Lot,
    TelegramMarket,
    count_unique_star_gifts,
    extract_star_gift_ids,
    ids_from_json_payload,
    merge_ids,
    _stars_level,
)
from tracker import format_lot, fresh_from_page


def _lot(**kwargs) -> Lot:
    base = dict(
        id="1",
        title="Sharp Tongue",
        number=1491,
        stars=6666.0,
        slug="SharpTongue-1491",
        model="Avatar",
        seller="Katta_xoja",
        seller_id=7276424760,
        first_name="Мария",
        about="девушке можно писать 💅",
        is_premium=False,
        free_dm=True,
        account_level=1,
        gifts_count=4,
        has_photo=True,
    )
    base.update(kwargs)
    return Lot(**base)


def test_card_matches_screenshot() -> None:
    lot = _lot()
    ts = datetime(2026, 9, 1, 20, 12, 17, tzinfo=timezone(timedelta(hours=3))).timestamp()
    text = format_lot(lot, ts=ts)
    expected = """🎉 <b>НОВЫЙ ЛИСТИНГ</b>

🎁 Гифт: <b>Sharp Tongue</b>
💰 Цена: <b>6666 Stars / 67.99 TON</b>
🏷 Модель: <b>Avatar</b>
👤 Продавец: @Katta_xoja (<code>7276424760</code>)
👧 Имя: Мария
📊 Level: 1
📢 Сообщение: бесплатно
💃 Статус: без Premium
🔗 <a href="https://t.me/nft/SharpTongue-1491">SharpTongue-1491</a>
🕒 01.09.2026 20:12:17"""
    assert text == expected, text


def test_girl_keeps_maria() -> None:
    assert is_girl(_lot()) is True
    assert looks_male(_lot()) is False


def test_girl_keeps_latin_name() -> None:
    lot = _lot(first_name="Kristina", about="🌸", seller="kris_shop")
    assert is_girl(lot) is True
    assert is_russian(lot) is True
    assert filter_lot(lot, min_stars=4500, max_stars=27000) == ""


def test_skips_boy() -> None:
    lot = _lot(first_name="Никита", about="торгую гифтами", seller="nikita_gifts")
    assert is_girl(lot) is False
    assert looks_male(lot) is True
    assert filter_lot(lot, min_stars=4500, max_stars=27000) == "мужской"


def test_skips_male_nick() -> None:
    lot = _lot(first_name="Alex", about="", seller="ivan_market")
    assert looks_male(lot) is True


def test_price_range() -> None:
    ok = _lot(stars=5000)
    assert filter_lot(ok, min_stars=4500, max_stars=27000) == ""
    low = _lot(stars=4499)
    assert filter_lot(low, min_stars=4500, max_stars=27000) == "цена"
    high = _lot(stars=27001)
    assert filter_lot(high, min_stars=4500, max_stars=27000) == "цена"


def test_level_max_2() -> None:
    assert filter_lot(_lot(account_level=2), min_stars=4500, max_stars=27000) == ""
    assert filter_lot(_lot(account_level=3), min_stars=4500, max_stars=27000) == "level"
    assert filter_lot(_lot(account_level=None), min_stars=4500, max_stars=27000) == ""
    assert (
        filter_lot(
            _lot(account_level=None),
            min_stars=4500,
            max_stars=27000,
            require_known=True,
        )
        == "level"
    )


def test_max_12_nfts() -> None:
    assert filter_lot(_lot(gifts_count=6), min_stars=4500, max_stars=27000) == ""
    assert filter_lot(_lot(gifts_count=7), min_stars=4500, max_stars=27000) == "много NFT"
    assert filter_lot(_lot(gifts_count=None), min_stars=4500, max_stars=27000) == ""


def test_free_dm_only() -> None:
    assert filter_lot(_lot(free_dm=True), min_stars=4500, max_stars=27000) == ""
    assert filter_lot(_lot(free_dm=False), min_stars=4500, max_stars=27000) == "платные ЛС"
    assert filter_lot(_lot(free_dm=None), min_stars=4500, max_stars=27000) == ""


def test_girl_from_bio_emoji() -> None:
    lot = _lot(first_name="Alexa", about="she/her 🎀", seller="alexa_nft")
    assert is_girl(lot) is False
    ru = _lot(first_name="Алекса", about="девушка, пишите 🎀", seller="alexa_nft")
    assert is_girl(ru) is True


def test_hardcoded_filters() -> None:
    assert config.MIN_STARS == 4500
    assert config.MAX_STARS == 27000
    assert config.MAX_ACCOUNT_LEVEL == 2
    assert config.MAX_NFTS == 6
    assert config.POST_INTERVAL == 4.0
    assert config.CHANNEL_ID == -1003784435307
    assert config.BOT_USERNAME == "jsjeigiejwhnewbot"
    assert config.API_ID == 28687552
    assert config.API_HASH == "1abf9a58d0c22f62437bec89bd6b27a3"
    assert config.SCAN_BATCH == 36
    assert config.PAGE_LIMIT == 8
    assert config.TRACKER_VERSION == "5.2.0"
    assert config.MIN_COLLECTIONS == 50


def test_collect_ids_keeps_zero_resale() -> None:
    class Gift:
        def __init__(self, gid: int, resale: int = 0) -> None:
            self.id = gid
            self.availability_resale = resale

    ids = TelegramMarket._collect_gift_ids(
        [Gift(11, 0), Gift(12, 5), Gift(11, 0), Gift(13, 0)]
    )
    assert ids == [11, 12, 13]


def test_next_batch_all_collections() -> None:
    market = TelegramMarket.__new__(TelegramMarket)
    market.gift_ids = [1, 2, 3, 4, 5]
    market._cursor = 3
    batch = market.next_batch(0)
    assert sorted(batch) == [1, 2, 3, 4, 5]
    assert market._cursor == 0


def test_merge_and_json_catalog() -> None:
    assert merge_ids([1, 2], [2, 3], []) == [1, 2, 3]
    ids = ids_from_json_payload(
        {
            "gift_ids": [5983471780763796287],
            "5936085638515261992": "Signet Ring",
        }
    )
    assert 5983471780763796287 in ids
    assert 5936085638515261992 in ids
    small = ids_from_json_payload({"gift_ids": [11, 12]})
    assert small == []  # короткие id витрины не считаем коллекциями


def test_extract_star_gift_ids() -> None:
    import struct

    gid = 5983471780763796287
    blob = (
        b"xxxx"
        + struct.pack("<I", 0x313A9547)
        + struct.pack("<I", 0)
        + struct.pack("<q", gid)
        + b"yyyy"
    )
    assert extract_star_gift_ids(blob) == [gid]


def test_bundled_catalog_has_enough() -> None:
    market = TelegramMarket.__new__(TelegramMarket)
    ids = TelegramMarket.load_from_bundled(market)
    assert len(ids) >= 50
    assert len(ids) >= 100


def test_skips_non_russian() -> None:
    latin = _lot(first_name="Kristina", about="🌸", seller="kris_shop", lang_code="en")
    assert is_girl(latin) is True
    assert is_russian(latin) is True
    assert filter_lot(latin, min_stars=4500, max_stars=27000) == ""
    iranian = _lot(first_name="Sara", about="hello", seller="sara_nft", lang_code="fa")
    assert is_russian(iranian) is False
    assert looks_male(iranian) is False
    assert filter_lot(iranian, min_stars=4500, max_stars=27000) == "не русский"
    assert is_russian(_lot()) is True
    cis_latin = _lot(first_name="Kristina", about="🌸", seller="kris_shop", lang_code="")
    assert is_russian(cis_latin) is True


def test_girl_from_gifts_and_stories() -> None:
    lot = _lot(
        first_name="Lee",
        about="",
        seller="lee_shop",
        gifts_text="Rose Heart Perfume",
        stories_text="новая ава 💅",
        has_photo=True,
    )
    assert is_girl(lot) is False
    ru = _lot(
        first_name="Мария",
        about="пишите 💅",
        seller="masha_shop",
        gifts_text="Rose Heart Perfume",
        stories_text="новая ава 💅",
        has_photo=True,
    )
    assert is_girl(ru) is True


def test_bio_hints_do_not_make_a_girl() -> None:
    """@ynosleep / @Etalonkasexa пролезали из-за «девушке можно писать» в био."""
    yno = _lot(first_name="", about="девушке можно писать 💅", seller="ynosleep")
    assert is_girl(yno) is False
    assert filter_lot(yno, min_stars=4500, max_stars=27000) == "нет женских признаков"
    boy = _lot(
        first_name="Алексей",
        about="девушке можно писать 💅 girl pink",
        seller="etalonkasexa",
    )
    assert looks_male(boy) is True
    assert is_girl(boy) is False
    sasha = _lot(first_name="Саша", about="торгую гифтами", seller="sasha_nft")
    assert is_girl(sasha) is False


def test_stars_level_reads_current_level() -> None:
    class Rating:
        def __init__(self) -> None:
            self.current_level = 3
            self.level = None

    assert _stars_level(Rating()) == 3
    assert _stars_level(2) == 2
    assert _stars_level({"level": 1}) == 1


def test_seller_keys_match_username_and_id() -> None:
    from filters import seller_keys

    a = _lot(seller="Masha", seller_id=111)
    b = _lot(seller="masha", seller_id=111)
    c = _lot(seller="", seller_id=111)
    assert seller_keys(a) & seller_keys(b)
    assert seller_keys(a) & seller_keys(c)


def test_count_unique_nfts_skips_unlimited() -> None:
    class Gift:
        def __init__(self, slug: str = "") -> None:
            self.slug = slug

    class Item:
        def __init__(self, gift: Gift) -> None:
            self.gift = gift

    class Saved:
        def __init__(self, gifts: list) -> None:
            self.gifts = gifts

    cheap = Gift(slug="")
    one = Gift(slug="PlushPepe-1")
    two = Gift(slug="DurovCap-2")
    saved = Saved([Item(cheap), Item(one), Item(two)])
    assert count_unique_star_gifts(saved) == 2
    whale = _lot(first_name="Мария", gifts_count=10)
    assert filter_lot(whale, min_stars=4500, max_stars=27000) == "много NFT"
    few = _lot(first_name="Мария", gifts_count=3)
    assert filter_lot(few, min_stars=4500, max_stars=27000) == ""


def test_latin_girl_with_russian_bio() -> None:
    lot = _lot(first_name="Kristina", about="привет, пишите", seller="kris_shop")
    assert is_russian(lot) is True
    assert is_girl(lot) is True
    assert filter_lot(lot, min_stars=4500, max_stars=27000) == ""


def test_skips_persian_and_latin_boys() -> None:
    reza = _lot(first_name="Reza", about="", seller="reza_gifts", lang_code="fa")
    assert looks_male(reza) is True
    assert is_girl(reza) is False
    assert filter_lot(reza, min_stars=4500, max_stars=27000) == "мужской"
    nima = _lot(first_name="Nima", about="nft", seller="nima_shop")
    assert looks_male(nima) is True
    amir = _lot(first_name="Shop", about="", seller="amir_nft")
    assert looks_male(amir) is True
    assert filter_lot(amir, min_stars=4500, max_stars=27000) == "мужской"
    boy = _lot(first_name="Алексей", about="торгую", seller="lexa_gifts")
    assert looks_male(boy) is True
    assert is_girl(boy) is False
    assert filter_lot(boy, min_stars=4500, max_stars=27000) == "мужской"


def test_fresh_from_page_only_new_listings() -> None:
    old = _lot(id="old", stars=10000)
    mid = _lot(id="mid", stars=9000)
    new = _lot(id="new", stars=8000)
    expensive = _lot(id="exp", stars=99_000)
    bubbled = _lot(id="mid", stars=9000)
    page, fresh = fresh_from_page(None, [new], {}, 4500, 27000)
    assert page == ["new"]
    assert fresh == []
    page, fresh = fresh_from_page(["new", "old"], [new, old], {}, 4500, 27000)
    assert fresh == []
    page, fresh = fresh_from_page(["old"], [new, mid, old], {}, 4500, 27000)
    assert [x.id for x in fresh] == ["new", "mid"]
    page, fresh = fresh_from_page(["old"], [expensive, mid, old], {}, 4500, 27000)
    assert [x.id for x in fresh] == ["mid"]
    # #1 купили — mid всплыл, он уже был на странице
    page, fresh = fresh_from_page(["old", "mid"], [bubbled, old], {}, 4500, 27000)
    assert fresh == []
    page, fresh = fresh_from_page(["old"], [new], {"new": 1.0}, 4500, 27000)
    assert fresh == []


def test_state_schema_clears_seller_bans() -> None:
    import json
    import tempfile
    from pathlib import Path

    from tracker import STATE_SCHEMA, load_state

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(
            json.dumps(
                {
                    "seen": {"lot1": 1.0},
                    "seen_sellers": {"spammer": 1.0},
                    "market_ids": ["old"],
                    "schema": 5,
                }
            ),
            encoding="utf-8",
        )
        data = load_state(path)
        assert data["skip_sellers"] == {}
        assert data["schema"] == STATE_SCHEMA
        assert data["seen"]["lot1"] == 1.0
        assert data["market_ids"] == ["old"]
        assert data["pages"] == {}
        assert data["seen_sellers"] == {"spammer": 1.0}


def main() -> None:
    tests = [
        test_card_matches_screenshot,
        test_girl_keeps_maria,
        test_girl_keeps_latin_name,
        test_skips_boy,
        test_skips_male_nick,
        test_price_range,
        test_level_max_2,
        test_max_12_nfts,
        test_free_dm_only,
        test_girl_from_bio_emoji,
        test_hardcoded_filters,
        test_collect_ids_keeps_zero_resale,
        test_next_batch_all_collections,
        test_merge_and_json_catalog,
        test_extract_star_gift_ids,
        test_bundled_catalog_has_enough,
        test_skips_non_russian,
        test_girl_from_gifts_and_stories,
        test_bio_hints_do_not_make_a_girl,
        test_stars_level_reads_current_level,
        test_seller_keys_match_username_and_id,
        test_count_unique_nfts_skips_unlimited,
        test_latin_girl_with_russian_bio,
        test_skips_persian_and_latin_boys,
        test_fresh_from_page_only_new_listings,
        test_state_schema_clears_seller_bans,
    ]
    for fn in tests:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nВсе {len(tests)} тестов прошли")


if __name__ == "__main__":
    main()
