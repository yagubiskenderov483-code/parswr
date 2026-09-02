"""Тесты формата карточки и фильтров: python3 test_tracker.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from filters import filter_lot, is_girl, looks_male
from market import Lot
from tracker import format_lot


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


def test_skips_boy() -> None:
    lot = _lot(first_name="Никита", about="торгую гифтами", seller="nikita_gifts")
    assert is_girl(lot) is False
    assert looks_male(lot) is True


def test_skips_male_nick() -> None:
    lot = _lot(first_name="Alex", about="", seller="ivan_market")
    assert looks_male(lot) is True


def test_price_range() -> None:
    ok = _lot(stars=3000)
    assert filter_lot(ok, min_stars=3000, max_stars=25000) == ""
    low = _lot(stars=2999)
    assert filter_lot(low, min_stars=3000, max_stars=25000) == "цена"
    high = _lot(stars=25001)
    assert filter_lot(high, min_stars=3000, max_stars=25000) == "цена"


def test_level_max_2() -> None:
    assert filter_lot(_lot(account_level=2), min_stars=3000, max_stars=25000) == ""
    assert filter_lot(_lot(account_level=3), min_stars=3000, max_stars=25000) == "level"
    assert filter_lot(_lot(account_level=None), min_stars=3000, max_stars=25000) == "нет данных"


def test_max_12_nfts() -> None:
    assert filter_lot(_lot(gifts_count=12), min_stars=3000, max_stars=25000) == ""
    assert filter_lot(_lot(gifts_count=13), min_stars=3000, max_stars=25000) == "много NFT"
    assert filter_lot(_lot(gifts_count=None), min_stars=3000, max_stars=25000) == "нет данных"


def test_free_dm_only() -> None:
    assert filter_lot(_lot(free_dm=True), min_stars=3000, max_stars=25000) == ""
    assert filter_lot(_lot(free_dm=False), min_stars=3000, max_stars=25000) == "платные ЛС"
    assert filter_lot(_lot(free_dm=None), min_stars=3000, max_stars=25000) == "нет данных"


def test_girl_from_bio_emoji() -> None:
    lot = _lot(first_name="Alexa", about="she/her 🎀", seller="alexa_nft")
    assert is_girl(lot) is True


def test_hardcoded_filters() -> None:
    assert config.MIN_STARS == 3000
    assert config.MAX_STARS == 25000
    assert config.MAX_ACCOUNT_LEVEL == 2
    assert config.MAX_NFTS == 12
    assert config.POST_INTERVAL == 4.0
    assert config.CHANNEL_ID == -1003784435307
    assert config.BOT_USERNAME == "jsjeigiejwhnewbot"
    assert config.API_ID == 28687552
    assert config.API_HASH == "1abf9a58d0c22f62437bec89bd6b27a3"


def test_girl_from_gifts_and_stories() -> None:
    lot = _lot(
        first_name="Lee",
        about="",
        seller="lee_shop",
        gifts_text="Rose Heart Perfume",
        stories_text="новая ава 💅",
        has_photo=True,
    )
    assert is_girl(lot) is True


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
        test_girl_from_gifts_and_stories,
    ]
    for fn in tests:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nВсе {len(tests)} тестов прошли")


if __name__ == "__main__":
    main()
