"""Тесты формата карточки и фильтров: python3 test_tracker.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from filters import (
    classify_skip,
    filter_lot,
    is_girl,
    is_russian,
    looks_male,
    russian_why,
    skip_stats,
)
from market import (
    Lot,
    TelegramMarket,
    count_unique_star_gifts,
    extract_star_gift_ids,
    ids_from_json_payload,
    merge_ids,
    _stars_level,
)
from tracker import (
    empty_funnel,
    format_funnel_report,
    format_lot,
    fresh_from_page,
    funnel_invariants,
    record_enqueue_dup,
    record_fresh_price_seen,
    record_work_in,
    record_worker_filter,
)


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
    assert filter_lot(lot, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""


def test_skips_boy() -> None:
    lot = _lot(first_name="Никита", about="торгую гифтами", seller="nikita_gifts")
    assert is_girl(lot) is False
    assert looks_male(lot) is True
    assert filter_lot(lot, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "мужской"


def test_skips_male_nick() -> None:
    lot = _lot(first_name="Alex", about="", seller="ivan_market")
    assert looks_male(lot) is True


def test_price_range() -> None:
    ok = _lot(stars=5000)
    assert filter_lot(ok, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""
    ok2 = _lot(stars=25000)
    assert filter_lot(ok2, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""
    low = _lot(stars=4999)
    assert filter_lot(low, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "цена"
    high = _lot(stars=25001)
    assert filter_lot(high, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "цена"


def test_level_max_2() -> None:
    assert filter_lot(_lot(account_level=2), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""
    assert filter_lot(_lot(account_level=3), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "level"
    # Telegram часто не отдаёт stars_rating — None не сжигает лот как «level»
    assert filter_lot(_lot(account_level=None), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""


def test_max_12_nfts() -> None:
    assert filter_lot(_lot(gifts_count=6), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""
    assert filter_lot(_lot(gifts_count=7), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "много NFT"
    assert filter_lot(_lot(gifts_count=None), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""


def test_free_dm_only() -> None:
    assert filter_lot(_lot(free_dm=True), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""
    assert filter_lot(_lot(free_dm=False), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "платные ЛС"
    assert filter_lot(_lot(free_dm=None), min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""


def test_girl_from_bio_emoji() -> None:
    lot = _lot(first_name="Alexa", about="she/her 🎀", seller="alexa_nft")
    assert is_girl(lot) is False
    ru = _lot(first_name="Алекса", about="девушка, пишите 🎀", seller="alexa_nft")
    assert is_girl(ru) is True


def test_hardcoded_filters() -> None:
    assert config.MIN_STARS == 5000
    assert config.MAX_STARS == 25000
    assert config.MAX_ACCOUNT_LEVEL == 2
    assert config.MAX_NFTS == 6
    assert config.POST_INTERVAL == 4.0
    assert config.CHANNEL_ID == -1003784435307
    assert config.BOT_USERNAME == "jsjeigiejwhnewbot"
    assert config.API_ID == 28687552
    assert config.API_HASH == "1abf9a58d0c22f62437bec89bd6b27a3"
    assert config.SCAN_BATCH == 0
    assert config.PAGE_LIMIT == 12
    assert config.SCAN_PARALLEL == 12
    assert config.TRACKER_VERSION == "5.9.0"
    assert config.MIN_COLLECTIONS == 50
    assert config.GIRL_MIN_SCORE == 5


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


def test_is_russian_empty_profile_is_unknown() -> None:
    """Нет имени/био/lang — не not_ru, а «нет данных». lang_code пустой сам по себе не режет."""
    empty = _lot(first_name="", last_name="", about="", seller="nft_store", lang_code="")
    empty.first_name = ""
    empty.about = ""
    empty.lang_code = ""
    assert is_russian(empty) is None
    assert "empty" in russian_why(empty)
    assert filter_lot(empty, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "нет данных"


def test_is_russian_latin_shop_name_is_not_ru() -> None:
    """Латинское имя-магазин без кириллицы → not_ru. Это и есть 83 в статусе."""
    shop = _lot(first_name="Shop", about="", seller="gift_market", lang_code="")
    assert is_russian(shop) is False
    why = russian_why(shop)
    assert "FAIL" in why
    assert "no cyrillic" in why
    assert filter_lot(shop, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "не русский"


def test_is_russian_female_username_counts() -> None:
    """Ник masha_nft без кириллического имени — всё равно ru (согласовано с girl)."""
    nick = _lot(first_name="Shop", about="", seller="masha_nft", lang_code="")
    assert is_russian(nick) is True
    assert "username" in russian_why(nick)


def test_is_russian_cyrillic_without_lang_code() -> None:
    """Telegram не отдаёт lang_code чужих юзеров — кириллица всё равно ru."""
    ru = _lot(first_name="Мария", about="привет", lang_code="")
    assert is_russian(ru) is True
    assert "cyrillic" in russian_why(ru)


def test_dup_reasons_split_listing_and_seller() -> None:
    stats = skip_stats()
    classify_skip("дубль продавца", stats)
    classify_skip("дубль лота", stats)
    assert stats["dup_seller"] == 1
    assert stats["dup_listing"] == 1
    assert stats["dup"] == 2


def test_skips_non_russian() -> None:
    latin = _lot(first_name="Kristina", about="🌸", seller="kris_shop", lang_code="en")
    assert is_girl(latin) is True
    assert is_russian(latin) is True
    assert filter_lot(latin, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""
    iranian = _lot(first_name="Sara", about="hello", seller="sara_nft", lang_code="fa")
    assert is_russian(iranian) is False
    assert looks_male(iranian) is False
    assert filter_lot(iranian, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "не русский"
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
    assert filter_lot(yno, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "нет женских признаков"
    boy = _lot(
        first_name="Алексей",
        about="девушке можно писать 💅 girl pink",
        seller="etalonkasexa",
    )
    assert looks_male(boy) is True
    assert is_girl(boy) is False
    sasha = _lot(first_name="Саша", about="торгую гифтами", seller="sasha_nft")
    assert is_girl(sasha) is False
    nick = _lot(first_name="Shop", about="торгую гифтами", seller="masha_nft")
    assert is_girl(nick) is True
    assert filter_lot(nick, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""


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
    assert filter_lot(whale, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "много NFT"
    few = _lot(first_name="Мария", gifts_count=3)
    assert filter_lot(few, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""


def test_latin_girl_with_russian_bio() -> None:
    lot = _lot(first_name="Kristina", about="привет, пишите", seller="kris_shop")
    assert is_russian(lot) is True
    assert is_girl(lot) is True
    assert filter_lot(lot, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == ""


def test_skips_persian_and_latin_boys() -> None:
    reza = _lot(first_name="Reza", about="", seller="reza_gifts", lang_code="fa")
    assert looks_male(reza) is True
    assert is_girl(reza) is False
    assert filter_lot(reza, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "мужской"
    nima = _lot(first_name="Nima", about="nft", seller="nima_shop")
    assert looks_male(nima) is True
    amir = _lot(first_name="Shop", about="", seller="amir_nft")
    assert looks_male(amir) is True
    assert filter_lot(amir, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "мужской"
    boy = _lot(first_name="Алексей", about="торгую", seller="lexa_gifts")
    assert looks_male(boy) is True
    assert is_girl(boy) is False
    assert filter_lot(boy, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "мужской"


def test_fresh_from_page_only_new_listings() -> None:
    old = _lot(id="old", stars=10000)
    mid = _lot(id="mid", stars=9000)
    new = _lot(id="new", stars=8000)
    expensive = _lot(id="exp", stars=99_000)
    bubbled = _lot(id="mid", stars=9000)
    page, fresh = fresh_from_page(None, [new], {}, config.MIN_STARS, config.MAX_STARS)
    assert page == ["new"]
    assert fresh == []
    page, fresh = fresh_from_page(["new", "old"], [new, old], {}, config.MIN_STARS, config.MAX_STARS)
    assert fresh == []
    page, fresh = fresh_from_page(["old"], [new, mid, old], {}, config.MIN_STARS, config.MAX_STARS)
    assert [x.id for x in fresh] == ["new", "mid"]
    page, fresh = fresh_from_page(["old"], [expensive, mid, old], {}, config.MIN_STARS, config.MAX_STARS)
    assert [x.id for x in fresh] == ["mid"]
    # #1 купили — mid всплыл, он уже был на странице
    page, fresh = fresh_from_page(["old", "mid"], [bubbled, old], {}, config.MIN_STARS, config.MAX_STARS)
    assert fresh == []
    page, fresh = fresh_from_page(["old"], [new], {"new": 1.0}, config.MIN_STARS, config.MAX_STARS)
    assert fresh == []


def test_fresh_from_page_ignores_api_order() -> None:
    """Известный id больше не делает break — новый лот после него не теряется."""
    a = _lot(id="A", stars=8000)
    b = _lot(id="B", stars=9000)
    c = _lot(id="C", stars=10000)
    new = _lot(id="NEW", stars=7000)
    # Telegram отдал не newest→oldest: известный B стоит перед новым
    page, fresh = fresh_from_page(["A", "B", "C"], [b, new, a], {}, config.MIN_STARS, config.MAX_STARS)
    assert page == ["B", "NEW", "A"]
    assert [x.id for x in fresh] == ["NEW"]
    # пустой снимок — ничего не постим
    page, fresh = fresh_from_page([], [new, a], {}, config.MIN_STARS, config.MAX_STARS)
    assert fresh == []
    # всплытие mid среди перемешанных известных
    mid = _lot(id="mid", stars=9000)
    old = _lot(id="old", stars=10000)
    page, fresh = fresh_from_page(["old", "mid"], [mid, old, new], {}, config.MIN_STARS, config.MAX_STARS)
    assert [x.id for x in fresh] == ["NEW"]


def test_pipeline_stats_sequential() -> None:
    """Счётчики — последовательная воронка на реальных множествах."""
    fn = empty_funnel()

    record_fresh_price_seen(fn, fresh=True, price_ok=False, already_seen=False)
    record_fresh_price_seen(fn, fresh=True, price_ok=True, already_seen=True)
    record_fresh_price_seen(fn, fresh=True, price_ok=True, already_seen=False)

    assert fn["fresh_detected"] == 3
    assert fn["price_checked"] == 3
    assert fn["price_pass"] == 2
    assert fn["price_reject"] == 1
    assert fn["seen_checked"] == 2
    assert fn["seen_pass"] == 1
    assert fn["seen_reject"] == 1

    # 1) seller duplicate на enqueue
    record_enqueue_dup(fn, "seller")
    assert fn["dup_seller"] == 1
    assert fn["ru_checked"] == 0
    assert fn["nft_pass"] == 0

    record_enqueue_dup(fn, "listing")
    assert fn["dup_listing"] == 1

    record_work_in(fn)

    # RU reject
    record_worker_filter(
        fn, _lot(first_name="Shop", about="", seller="gift_market"), "не русский"
    )
    assert fn["male_pass"] == 1
    assert fn["ru_checked"] == 1
    assert fn["ru_reject"] == 1
    assert fn["girl_checked"] == 0

    # girl reject
    record_worker_filter(
        fn,
        _lot(first_name="Lee", about="привет торгую", seller="shop999xx"),
        "нет женских признаков",
    )
    assert fn["ru_pass"] == 1
    assert fn["girl_reject"] == 1

    # 4) normal successful lot
    record_worker_filter(fn, _lot(first_name="Мария"), "")
    assert fn["nft_pass"] == 1
    assert fn["girl_pass"] == 1

    # 3) male lot — отдельная стадия
    before_ru = fn["ru_checked"]
    before_girl = fn["girl_checked"]
    record_worker_filter(
        fn, _lot(first_name="Алексей", seller="lexa"), "мужской"
    )
    assert fn["male_reject"] == 1
    assert fn["male_checked"] >= 1
    assert fn["ru_checked"] == before_ru
    assert fn["girl_checked"] == before_girl
    assert fn["reject_male"] == 1
    assert fn["reject_girl"] == 1  # только от girl reject выше, не от male

    # 2)+5)+6) seller duplicate после enrich — terminal, без nft/send
    before_nft = fn["nft_pass"]
    before_ru2 = fn["ru_checked"]
    before_male = fn["male_checked"]
    record_worker_filter(
        fn, _lot(first_name="Мария", seller="masha"), "дубль продавца"
    )
    assert fn["dup_seller"] == 2  # enqueue + post-enrich
    assert fn["dup_seller_post_enrich"] == 1
    assert fn["nft_pass"] == before_nft
    assert fn["ru_checked"] == before_ru2
    assert fn["male_checked"] == before_male
    assert fn["send_attempt"] == 0

    inv = funnel_invariants(fn)
    assert inv == [], inv
    report = format_funnel_report(fn)
    assert "male:" in report
    assert "post_enrich_seller:" in report


def test_stats_enqueue_seller_dup() -> None:
    fn = empty_funnel()
    record_enqueue_dup(fn, "seller")
    assert fn["dup_seller"] == 1
    assert fn["reject_duplicate_seller"] == 1
    assert fn["dup_seller_post_enrich"] == 0
    assert fn["nft_pass"] == 0
    assert fn["ru_checked"] == 0


def test_stats_post_enrich_seller_dup_no_nft_pass() -> None:
    fn = empty_funnel()
    record_worker_filter(fn, _lot(first_name="Мария"), "дубль продавца")
    assert fn["dup_seller"] == 1
    assert fn["dup_seller_post_enrich"] == 1
    assert fn["nft_pass"] == 0
    assert fn["nft_checked"] == 0
    assert fn["girl_pass"] == 0
    assert fn["ru_pass"] == 0
    assert fn["male_checked"] == 0
    assert fn["send_attempt"] == 0
    assert funnel_invariants(fn) == []


def test_stats_male_lot_separate_stage() -> None:
    fn = empty_funnel()
    record_worker_filter(fn, _lot(first_name="Алексей", seller="lexa"), "мужской")
    assert fn["male_checked"] == 1
    assert fn["male_reject"] == 1
    assert fn["male_pass"] == 0
    assert fn["ru_checked"] == 0
    assert fn["girl_checked"] == 0
    assert fn["reject_male"] == 1
    assert funnel_invariants(fn) == []


def test_stats_successful_lot() -> None:
    fn = empty_funnel()
    record_worker_filter(fn, _lot(first_name="Мария"), "")
    assert fn["male_pass"] == 1
    assert fn["ru_pass"] == 1
    assert fn["girl_pass"] == 1
    assert fn["dm_pass"] == 1
    assert fn["level_pass"] == 1
    assert fn["nft_pass"] == 1
    assert fn["dup_seller"] == 0
    assert funnel_invariants(fn) == []


def test_seller_dup_does_not_burn_to_seen() -> None:
    """Seller dup в enqueue не должен писать лот в seen: лот должен подхватиться на следующем проходе."""
    from tracker import PostQueue, Runtime
    import asyncio

    seen: dict = {}
    seen_sellers: dict = {}
    runtime = Runtime()
    q = PostQueue.__new__(PostQueue)
    q.seen = seen
    q.seen_sellers = seen_sellers
    q.market_ids = set()
    q._items = []
    q._queued = set()
    q._inflight = set()
    q._inflight_sellers = set()
    q._stop = False
    q._event = asyncio.Event()
    q._lock = asyncio.Lock()
    q._retries = {}
    q._last_title = ""
    q.state = {}
    q.state_file = None
    q.runtime = runtime

    # публикуем продавца
    import time
    seen_sellers["u:masha"] = time.time()
    seen_sellers["id:111"] = time.time()

    lot = _lot(id="X1", seller="masha", seller_id=111, stars=8000)
    added = q.enqueue([lot])
    assert added == 0
    # лот НЕ в seen — следующий проход подхватит
    assert "X1" not in seen
    assert runtime.funnel["dup_seller"] == 1
    assert runtime.funnel["reject_duplicate_seller"] == 1
    assert runtime.funnel["ru_checked"] == 0


def test_snapshot_then_only_new_listings() -> None:
    """Initial snapshot (prev empty) → no posts; later new id → detect."""
    a = _lot(id="a", stars=8000)
    b = _lot(id="b", stars=9000)
    page, fresh = fresh_from_page(None, [a, b], {}, config.MIN_STARS, config.MAX_STARS)
    assert page == ["a", "b"]
    assert fresh == []
    page, fresh = fresh_from_page(["a", "b"], [a, b], {}, config.MIN_STARS, config.MAX_STARS)
    assert fresh == []
    neu = _lot(id="new1", stars=7000)
    page, fresh = fresh_from_page(
        ["a", "b"], [neu, a, b], {}, config.MIN_STARS, config.MAX_STARS
    )
    assert [x.id for x in fresh] == ["new1"]


def test_pagination_reorder_not_new() -> None:
    a = _lot(id="a", stars=8000)
    b = _lot(id="b", stars=9000)
    c = _lot(id="c", stars=10000)
    page, fresh = fresh_from_page(
        ["a", "b", "c"], [c, a, b], {}, config.MIN_STARS, config.MAX_STARS
    )
    assert fresh == []


def test_girl_multi_signal_rejects_bio_only() -> None:
    from filters import female_score, has_female_identity

    yno = _lot(first_name="", about="девушке можно писать 💅", seller="ynosleep")
    assert has_female_identity(yno) is False
    assert female_score(yno) >= 3
    assert is_girl(yno) is False


def test_girl_multi_signal_name_passes() -> None:
    from filters import female_score

    m = _lot(first_name="Мария", about="", has_photo=False)
    assert female_score(m) >= config.GIRL_MIN_SCORE
    assert is_girl(m) is True


def test_uncertain_no_identity_rejects() -> None:
    lot = _lot(first_name="Shop", about="🌸💖", seller="gift_xx", has_photo=True)
    assert looks_male(lot) is False
    assert is_girl(lot) is False


def test_non_russian_girl_rejects() -> None:
    lot = _lot(first_name="Sara", about="hello", seller="sara_g", lang_code="fa")
    assert is_russian(lot) is False
    assert filter_lot(lot, min_stars=config.MIN_STARS, max_stars=config.MAX_STARS) == "не русский"


def test_scan_batch_zero_means_all_collections() -> None:
    assert config.SCAN_BATCH == 0
    market = TelegramMarket.__new__(TelegramMarket)
    market.gift_ids = list(range(1, 21))
    market._cursor = 5
    batch = market.next_batch(config.SCAN_BATCH)
    assert sorted(batch) == list(range(1, 21))


def test_post_interval_separate_from_poll() -> None:
    assert config.POST_INTERVAL == 4.0
    assert config.POLL_INTERVAL < config.POST_INTERVAL


def test_seller_keys_username_found() -> None:
    from filters import seller_keys

    lot = _lot(seller="masha_shop", seller_id=42)
    assert "masha_shop" in seller_keys(lot) or "u:masha_shop" in seller_keys(lot)
    unknown = _lot(seller="", seller_id=99)
    assert seller_keys(unknown) == {"id:99"}


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


def test_percentile_and_scan_round_metrics() -> None:
    from diagnostics import Diagnostics, percentile

    assert percentile([], 50) is None
    assert percentile([10.0], 50) == 10.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    d = Diagnostics()
    d.record_scan_round(
        {
            "pass": 1,
            "round_started_at": 1.0,
            "round_finished_at": 2.0,
            "round_ms": 1000.0,
            "collections_checked": 10,
            "collections_success": 9,
            "collections_failed": 1,
            "api_fetch_count": 10,
            "found_in_range": 3,
            "fresh_detected": 5,
            "queued": 2,
            "duplicate_seller": 1,
            "duplicate_listing": 0,
            "flood_wait_count": 0,
            "flood_wait_seconds": 0.0,
            "timeout_count": 0,
        }
    )
    assert d.last_round["pass"] == 1
    assert d.scan_p50() == 1000.0
    lines = d.status_lines()
    assert any("scan round:" in x for x in lines)
    assert any("detection_latency: UNKNOWN" in x for x in lines)


def test_detection_latency_unknown_without_listing_time() -> None:
    from diagnostics import Diagnostics

    d = Diagnostics()
    lot = _lot(listing_created_at=None)
    d.record_detection(lot, pass_no=3)
    assert d.detection_latency_unknown == 1
    assert d.detection_latency_known == 0
    assert d.detections[-1]["detection_latency"] is None
    assert lot.listing_created_at is None


def test_ru_reject_codes() -> None:
    from diagnostics import russian_reject_code

    assert russian_reject_code(_lot(lang_code="fa", first_name="Ali", about="")) == (
        "foreign_lang"
    )
    shop = _lot(first_name="GiftShop", last_name="", about="best deals", lang_code="")
    shop.seller = "gift_shop_99"
    # ensure no cyrillic / not latin female
    assert is_russian(shop) is False
    assert russian_reject_code(shop) == "no_cyrillic"


def test_girl_forensics_no_identity_and_pass() -> None:
    from diagnostics import Diagnostics, girl_forensics, girl_reject_code
    from filters import has_female_identity

    maria = _lot()
    assert girl_reject_code(maria) == "ok"
    fx2 = girl_forensics(maria)
    assert fx2["identity"] is True
    assert fx2["score"] >= config.GIRL_MIN_SCORE
    assert any(s.startswith("name:") for s in fx2["signals"])

    d = Diagnostics()
    d.record_girl_outcome(maria, passed=True)
    no_id = _lot(
        first_name="Seller",
        last_name="",
        seller="nft_market",
        about="пиши 💅",
        has_photo=True,
        emoji_status="",
        gifts_text="",
        stories_text="",
        personal_channel="",
    )
    assert has_female_identity(no_id) is False
    assert girl_reject_code(no_id) == "no_identity"
    d.record_girl_outcome(no_id, passed=False)
    assert d.girl_pass == 1
    assert d.girl_reject_no_identity == 1
    assert d.girl_identity_false == 1


def test_username_and_floodwait_split() -> None:
    from diagnostics import Diagnostics

    d = Diagnostics()
    page = _lot(seller="maria_x")
    page.username_source = "resale_user"
    d.record_username(page, had_before_enrich=True)
    assert d.username_from_page == 1
    assert d.username_from_resale_user == 1

    later = _lot(seller="kate")
    later.username_source = "get_entity"
    d.record_username(later, had_before_enrich=False)
    assert d.username_from_get_entity == 1

    missing = _lot(seller="")
    d.record_username(missing, had_before_enrich=False)
    assert d.username_unknown == 1

    d.note_flood("scan", 2.0)
    d.note_flood("enrich", 3.0)
    d.note_flood("send", 1.0)
    assert d.scan_floodwait_count == 1
    assert d.enrich_floodwait_count == 1
    assert d.send_floodwait_count == 1
    assert d.scan_floodwait_seconds == 2.0
    d.record_enrich(120.0, ok=True)
    d.record_enrich(200.0, ok=False)
    assert d.enrich_count == 2
    assert d.enrich_success == 1
    assert d.enrich_failed == 1
    assert d.enrich_p50() is not None


def test_fill_user_sets_username_source() -> None:
    from types import SimpleNamespace

    from market import fill_user

    lot = _lot(seller="")
    user = SimpleNamespace(
        id=1,
        username="anna_nft",
        first_name="Anna",
        last_name="",
        premium=False,
        lang_code="",
        photo=None,
        emoji_status=None,
        stars_rating=None,
        usernames=None,
    )
    fill_user(lot, user, username_source="resale_user")
    assert lot.seller == "anna_nft"
    assert lot.username_source == "resale_user"
    fill_user(lot, user, username_source="get_entity")
    assert lot.username_source == "resale_user"  # first wins


def test_status_html_safe_diagnostics() -> None:
    """Telegram HTML: «score<5=0» парсилось как тег 5=0 — /status падал."""
    import re

    from bot import ControlBot
    from tracker import Runtime

    ctrl = ControlBot.__new__(ControlBot)
    ctrl.authorized = True
    ctrl.account_name = "tester"
    ctrl.runtime = Runtime()
    rt = ctrl.runtime
    rt.snapshot_ready = True
    rt.snapshot = 120
    rt.passes = 4
    rt.collections = 151
    rt.posted = 2
    rt.queue = 0
    rt.last_found = 42
    rt.last_fresh = 24
    rt.funnel["fresh_detected"] = 936
    rt.funnel["fresh"] = 936
    rt.funnel["price_checked"] = 936
    rt.funnel["price_pass"] = 157
    rt.funnel["price_reject"] = 779
    rt.funnel["seen_checked"] = 157
    rt.funnel["seen_pass"] = 143
    rt.funnel["seen_reject"] = 14
    rt.funnel["dup_seller"] = 28
    rt.funnel["work_in"] = 115
    rt.funnel["dequeued"] = 115
    rt.funnel["male_checked"] = 115
    rt.funnel["male_reject"] = 31
    rt.funnel["male_pass"] = 84
    rt.funnel["ru_checked"] = 84
    rt.funnel["ru_pass"] = 13
    rt.funnel["ru_reject"] = 71
    rt.funnel["girl_checked"] = 13
    rt.funnel["girl_pass"] = 3
    rt.funnel["girl_reject"] = 10
    rt.funnel["dm_checked"] = 3
    rt.funnel["dm_pass"] = 3
    rt.funnel["level_checked"] = 3
    rt.funnel["level_pass"] = 3
    rt.funnel["nft_checked"] = 3
    rt.funnel["nft_pass"] = 2
    rt.funnel["nft_reject"] = 1
    rt.funnel["send_attempt"] = 2
    rt.funnel["sent"] = 2
    rt.last_error = "FloodWait <tag> & more"
    d = rt.diag
    d.girl_reject_score_lt_min = 0
    d.girl_reject_no_identity = 10
    d.girl_pass = 3
    d.girl_reject = 10
    d.girl_identity_true = 3
    d.girl_identity_false = 10
    d.detection_latency_unknown = 42
    d.ru_reject_no_cyrillic = 48
    d.record_scan_round(
        {
            "pass": 4,
            "round_started_at": 1.0,
            "round_finished_at": 2.0,
            "round_ms": 8420.0,
            "collections_checked": 151,
            "collections_success": 151,
            "collections_failed": 0,
            "api_fetch_count": 151,
            "found_in_range": 42,
            "fresh_detected": 37,
            "queued": 24,
            "duplicate_seller": 18,
            "duplicate_listing": 0,
            "flood_wait_count": 0,
            "flood_wait_seconds": 0.0,
            "timeout_count": 0,
        }
    )
    d.record_enrich(120.0, ok=True)
    text = ControlBot._status_text(ctrl)

    assert "DIAGNOSTICS" in text
    assert "scan p50:" in text
    assert "detection_latency:" in text
    assert "RU reject:" in text
    assert "girl diagnostics:" in text
    assert "username:" in text
    assert "enrich:" in text
    assert "floodwait:" in text
    # корневая регрессия: сырой score<5=… ломает HTML
    assert "score<5" not in text
    assert "score_lt_5=" in text
    # last_error экранирован
    assert "<tag>" not in text
    assert "&lt;tag&gt;" in text
    # нет «тегов» вида <5=0> / <foo=
    assert re.search(r"<\d", text) is None
    allowed = {"code", "/code", "b", "/b"}
    for raw in re.findall(r"</?([a-zA-Z0-9_=<]+)", text):
        name = raw.split("=", 1)[0].rstrip("/")
        assert name in allowed or f"/{name}" in allowed or name in {
            "code",
            "b",
        }, raw


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
        test_is_russian_empty_profile_is_unknown,
        test_is_russian_latin_shop_name_is_not_ru,
        test_is_russian_female_username_counts,
        test_is_russian_cyrillic_without_lang_code,
        test_dup_reasons_split_listing_and_seller,
        test_skips_non_russian,
        test_girl_from_gifts_and_stories,
        test_bio_hints_do_not_make_a_girl,
        test_stars_level_reads_current_level,
        test_seller_keys_match_username_and_id,
        test_count_unique_nfts_skips_unlimited,
        test_latin_girl_with_russian_bio,
        test_skips_persian_and_latin_boys,
        test_fresh_from_page_only_new_listings,
        test_fresh_from_page_ignores_api_order,
        test_seller_dup_does_not_burn_to_seen,
        test_pipeline_stats_sequential,
        test_stats_enqueue_seller_dup,
        test_stats_post_enrich_seller_dup_no_nft_pass,
        test_stats_male_lot_separate_stage,
        test_stats_successful_lot,
        test_snapshot_then_only_new_listings,
        test_pagination_reorder_not_new,
        test_girl_multi_signal_rejects_bio_only,
        test_girl_multi_signal_name_passes,
        test_uncertain_no_identity_rejects,
        test_non_russian_girl_rejects,
        test_scan_batch_zero_means_all_collections,
        test_post_interval_separate_from_poll,
        test_seller_keys_username_found,
        test_state_schema_clears_seller_bans,
        test_percentile_and_scan_round_metrics,
        test_detection_latency_unknown_without_listing_time,
        test_ru_reject_codes,
        test_girl_forensics_no_identity_and_pass,
        test_username_and_floodwait_split,
        test_fill_user_sets_username_source,
        test_status_html_safe_diagnostics,
    ]
    for fn in tests:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nВсе {len(tests)} тестов прошли")


if __name__ == "__main__":
    main()
