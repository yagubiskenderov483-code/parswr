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


def _stub_queue(seen: dict | None = None, seen_sellers: dict | None = None):
    import asyncio

    from tracker import PostQueue, Runtime

    q = PostQueue.__new__(PostQueue)
    q.seen = seen if seen is not None else {}
    q.seen_sellers = seen_sellers if seen_sellers is not None else {}
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
    q.runtime = Runtime()
    return q


def _fake_user(**kwargs):
    from types import SimpleNamespace

    base = dict(
        id=1,
        username="",
        first_name="Мария",
        last_name="",
        premium=False,
        lang_code="",
        photo=None,
        emoji_status=None,
        stars_rating=None,
        usernames=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


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
    assert config.SCAN_BATCH == config.SCAN_PARALLEL
    assert config.SCAN_BATCH > 0
    assert config.RPC_CONCURRENCY == 4
    assert config.RPC_CONCURRENCY <= config.SCAN_PARALLEL
    assert config.PAGE_LIMIT == 12
    assert config.SCAN_PARALLEL == 12
    assert config.TRACKER_VERSION == "5.10.0"
    assert config.MIN_MODEL_FLOOR == 4000
    assert config.MAX_MODEL_FLOOR == 27000
    assert config.MIN_COLLECTIONS == 50
    assert config.GIRL_MIN_SCORE == 5
    assert config.REQUEST_TIMEOUT == 8.0


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
    """SCAN_BATCH=0 — escape hatch: все коллекции за round (не default)."""
    market = TelegramMarket.__new__(TelegramMarket)
    market.gift_ids = list(range(1, 21))
    market._cursor = 5
    batch = market.next_batch(0)
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


def test_extract_owner_user_id_peer_and_raw_int() -> None:
    from types import SimpleNamespace

    from market import extract_owner_user_id

    assert extract_owner_user_id(SimpleNamespace(user_id=111)) == 111
    assert extract_owner_user_id(555) == 555
    assert extract_owner_user_id(SimpleNamespace(id=777)) == 777
    assert extract_owner_user_id(None) is None
    assert extract_owner_user_id(0) is None
    assert extract_owner_user_id(SimpleNamespace(channel_id=999)) is None


def test_next_batch_ring_advances_cursor() -> None:
    market = TelegramMarket.__new__(TelegramMarket)
    market.gift_ids = list(range(10))
    market._cursor = 0
    a = market.next_batch(3)
    b = market.next_batch(3)
    assert a == [0, 1, 2]
    assert b == [3, 4, 5]
    assert set(a).isdisjoint(b)
    market._cursor = 8
    wrapped = market.next_batch(3)
    assert wrapped == [8, 9, 0]


def test_scan_batch_default_is_parallel_wave_not_all() -> None:
    """Default кольцо = SCAN_PARALLEL, не shuffle всех коллекций."""
    assert config.SCAN_BATCH == config.SCAN_PARALLEL
    market = TelegramMarket.__new__(TelegramMarket)
    market.gift_ids = list(range(40))
    market._cursor = 0
    batch = market.next_batch(config.SCAN_BATCH)
    assert len(batch) == config.SCAN_PARALLEL
    assert batch == list(range(config.SCAN_PARALLEL))


def test_scan_scheduling_does_not_change_detection() -> None:
    """Кольцо сканера не меняет snapshot / reorder semantics."""
    a = _lot(id="a", stars=8000)
    b = _lot(id="b", stars=9000)
    neu = _lot(id="new1", stars=7000)
    page, fresh = fresh_from_page(None, [a, b], {}, config.MIN_STARS, config.MAX_STARS)
    assert page == ["a", "b"]
    assert fresh == []
    page, fresh = fresh_from_page(
        ["a", "b"], [neu, a, b], {}, config.MIN_STARS, config.MAX_STARS
    )
    assert [x.id for x in fresh] == ["new1"]
    page, fresh = fresh_from_page(
        ["a", "b"], [b, a], {}, config.MIN_STARS, config.MAX_STARS
    )
    assert fresh == []
    assert config.POST_INTERVAL == 4.0


def test_two_lots_same_owner_id_only_first_enqueued() -> None:
    q = _stub_queue()
    a = _lot(id="A1", slug="Gift-1", seller="alice", seller_id=1001)
    b = _lot(id="B2", slug="Gift-2", seller="alice", seller_id=1001)
    added = q.enqueue([a, b])
    assert added == 1
    assert q._items[0].id == "A1"
    assert q.runtime.funnel["dup_seller"] == 1
    assert "B2" not in q.seen


def test_same_owner_id_different_username_blocked() -> None:
    q = _stub_queue()
    first = _lot(id="A1", slug="Gift-1", seller="alice", seller_id=1001)
    assert q.enqueue([first]) == 1
    from tracker import persist_sent_owner
    import time

    persist_sent_owner(q.seen_sellers, first, time.time())
    q._items.clear()
    q._queued.clear()
    second = _lot(id="B2", slug="Gift-2", seller="alice_new", seller_id=1001)
    assert q.enqueue([second]) == 0
    assert q.runtime.funnel["dup_seller"] == 1


def test_hidden_username_known_owner_id_dedupes() -> None:
    q = _stub_queue()
    sent = _lot(id="A1", slug="Gift-1", seller="alice", seller_id=1001)
    from tracker import persist_sent_owner
    import time

    persist_sent_owner(q.seen_sellers, sent, time.time())
    hidden = _lot(id="B2", slug="Gift-2", seller="", seller_id=1001)
    assert q.enqueue([hidden]) == 0
    assert "id:1001" in q.seen_sellers


def test_two_unknown_owners_without_id_not_merged() -> None:
    q = _stub_queue()
    u1 = _lot(id="U1", slug="Gift-1", seller="", seller_id=None)
    u2 = _lot(id="U2", slug="Gift-2", seller="", seller_id=None)
    added = q.enqueue([u1, u2])
    assert added == 2
    assert q.runtime.funnel["dup_seller"] == 0
    from filters import seller_keys

    assert seller_keys(u1) == set()
    assert seller_keys(u2) == set()


def test_persistent_seen_sellers_survives_reload() -> None:
    import json
    import tempfile
    import time
    from pathlib import Path

    from tracker import load_state, persist_sent_owner, save_state

    lot = _lot(id="A1", seller="alice", seller_id=4242)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        state = {
            "seen": {},
            "seen_sellers": {},
            "skip_sellers": {},
            "market_ids": [],
            "pages": {},
            "schema": 9,
        }
        persist_sent_owner(state["seen_sellers"], lot, time.time())
        save_state(path, state)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["seen_sellers"]["id:4242"]
        assert "u:alice" in raw["seen_sellers"]
        loaded = load_state(path)
        q = _stub_queue(seen_sellers=loaded["seen_sellers"])
        other = _lot(id="B2", slug="Other-2", seller="alice_renamed", seller_id=4242)
        assert q.enqueue([other]) == 0


def test_owner_dup_after_enrich_uses_id_not_username() -> None:
    import time

    from tracker import owner_dup_after_enrich, persist_sent_owner

    seen: dict = {}
    first = _lot(id="A1", seller="alice", seller_id=9)
    persist_sent_owner(seen, first, time.time())
    renamed = _lot(id="B2", seller="bob", seller_id=9)
    assert owner_dup_after_enrich(renamed, seen) == "дубль продавца"
    hidden = _lot(id="C3", seller="", seller_id=9)
    assert owner_dup_after_enrich(hidden, seen) == "дубль продавца"
    other = _lot(id="D4", seller="carol", seller_id=10)
    assert owner_dup_after_enrich(other, seen) == ""
    no_id = _lot(id="E5", seller="", seller_id=None)
    assert owner_dup_after_enrich(no_id, seen) == "нет продавца"


def test_hidden_owner_unique_gift_fallback() -> None:
    import asyncio
    from types import SimpleNamespace

    from market import TelegramMarket

    class FakeTG:
        def __init__(self) -> None:
            self.calls: list = []
            self.entity = _fake_user(id=77, username="", first_name="Мария")
            self.unique = SimpleNamespace(
                gift=SimpleNamespace(owner_id=SimpleNamespace(user_id=77)),
                users=[_fake_user(id=77, username="anna_nft", first_name="Анна")],
            )

        async def get_entity(self, uid):
            self.calls.append(("get_entity", int(uid)))
            return self.entity

        async def get_input_entity(self, uid):
            self.calls.append(("get_input_entity", int(uid)))
            return SimpleNamespace(user_id=int(uid), access_hash=1)

        async def __call__(self, req):
            self.calls.append(type(req).__name__)
            if type(req).__name__ == "GetUniqueStarGiftRequest":
                return self.unique
            raise AssertionError(type(req).__name__)

    async def run() -> None:
        m = TelegramMarket.__new__(TelegramMarket)
        m.client = FakeTG()
        m._flood_until = 0.0
        m.diag = None
        m._profile_cache = {}
        lot = _lot(seller="", seller_id=None, slug="Gift-77")
        lot.seller = ""
        await m.resolve_owner(lot, timeout=1.0)
        assert lot.seller_id == 77
        assert lot.seller == "anna_nft"
        assert lot.username_source == "unique_gift"
        assert "GetUniqueStarGiftRequest" in m.client.calls

    asyncio.run(run())


def test_hidden_owner_fulluser_via_input_entity() -> None:
    import asyncio
    from types import SimpleNamespace

    from market import TelegramMarket

    class FakeTG:
        def __init__(self) -> None:
            self.calls: list = []

        async def get_entity(self, uid):
            self.calls.append(("get_entity", int(uid)))
            return _fake_user(id=int(uid), username="", first_name="Мария")

        async def get_input_entity(self, uid):
            self.calls.append(("get_input_entity", int(uid)))
            return SimpleNamespace(user_id=int(uid), access_hash=99)

        async def __call__(self, req):
            self.calls.append(type(req).__name__)
            if type(req).__name__ == "GetFullUserRequest":
                return SimpleNamespace(
                    users=[_fake_user(id=88, username="from_full", first_name="Мария")],
                    full_user=SimpleNamespace(about="привет", personal_channel_id=None, stars_rating=None),
                )
            if type(req).__name__ == "GetUniqueStarGiftRequest":
                return SimpleNamespace(
                    gift=SimpleNamespace(owner_id=SimpleNamespace(user_id=88)),
                    users=[_fake_user(id=88, username="", first_name="Мария")],
                )
            raise AssertionError(type(req).__name__)

    async def run() -> None:
        m = TelegramMarket.__new__(TelegramMarket)
        m.client = FakeTG()
        m._flood_until = 0.0
        m.diag = None
        m._profile_cache = {}
        lot = _lot(seller="", seller_id=88, slug="Gift-88")
        lot.seller = ""
        await m.enrich_profile(lot, timeout=1.0)
        assert ("get_input_entity", 88) in m.client.calls
        assert "GetFullUserRequest" in m.client.calls
        assert lot.seller == "from_full"
        assert lot.username_source == "full_user"

    asyncio.run(run())


def test_hidden_owner_stays_unknown_when_api_has_no_username() -> None:
    import asyncio
    from types import SimpleNamespace

    from market import TelegramMarket

    class FakeTG:
        async def get_entity(self, uid):
            return _fake_user(id=int(uid), username="", first_name="Shop")

        async def get_input_entity(self, uid):
            return SimpleNamespace(user_id=int(uid), access_hash=1)

        async def __call__(self, req):
            if type(req).__name__ == "GetUniqueStarGiftRequest":
                return SimpleNamespace(
                    gift=SimpleNamespace(owner_id=SimpleNamespace(user_id=5)),
                    users=[_fake_user(id=5, username="", first_name="Shop")],
                )
            if type(req).__name__ == "GetFullUserRequest":
                return SimpleNamespace(
                    users=[_fake_user(id=5, username="", first_name="Shop")],
                    full_user=SimpleNamespace(about="", personal_channel_id=None, stars_rating=None),
                )
            return SimpleNamespace()

    async def run() -> None:
        m = TelegramMarket.__new__(TelegramMarket)
        m.client = FakeTG()
        m._flood_until = 0.0
        m.diag = None
        m._profile_cache = {}
        lot = _lot(seller="", seller_id=5, slug="Gift-5")
        lot.seller = ""
        await m.resolve_owner(lot, timeout=1.0)
        await m.enrich_profile(lot, timeout=1.0)
        assert lot.seller_id == 5
        assert lot.seller == ""

    asyncio.run(run())


def test_cached_entity_without_username_is_not_success() -> None:
    """Кэш User без username не обрывает fallback на UniqueStarGift."""
    import asyncio
    from types import SimpleNamespace

    from market import TelegramMarket

    class FakeTG:
        def __init__(self) -> None:
            self.calls: list = []

        async def get_entity(self, uid):
            self.calls.append("get_entity")
            return _fake_user(id=int(uid), username="", first_name="Мария")

        async def __call__(self, req):
            self.calls.append(type(req).__name__)
            return SimpleNamespace(
                gift=SimpleNamespace(owner_id=SimpleNamespace(user_id=3)),
                users=[_fake_user(id=3, username="real_nick", first_name="Мария")],
            )

    async def run() -> None:
        m = TelegramMarket.__new__(TelegramMarket)
        m.client = FakeTG()
        m._flood_until = 0.0
        m.diag = None
        lot = _lot(seller="", seller_id=3, slug="Gift-3")
        lot.seller = ""
        await m.resolve_owner(lot, timeout=1.0)
        assert "get_entity" in m.client.calls
        assert "GetUniqueStarGiftRequest" in m.client.calls
        assert lot.seller == "real_nick"
        assert lot.username_source == "unique_gift"

    asyncio.run(run())


def test_parse_gift_owner_id_raw_int() -> None:
    from types import SimpleNamespace

    from market import parse_gift

    gift = SimpleNamespace(
        id=99,
        slug="Gift-99",
        title="Gift",
        num=1,
        resell_amount=[SimpleNamespace(amount=8000)],
        attributes=[],
        owner_id=123456,
        gift_id=1,
    )
    # StarsAmount-like: class name won't match, but amount>0 fallback in loop
    class StarsAmt:
        def __init__(self) -> None:
            self.amount = 8000

    gift.resell_amount = [StarsAmt()]
    lot = parse_gift(gift, users=None)
    assert lot is not None
    assert lot.seller_id == 123456
    assert lot.seller == ""


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
    assert "owner_id:" in text
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
    assert "MODEL CATALOG" in text
    assert "models_total=" in text
    assert "collections_eligible=" in text
    assert "bad_model_value=" in text
    assert "OWNER" in text


def test_listing_vs_model_floor_split() -> None:
    from floors import listing_and_floor_reason, model_floor_verdict

    assert model_floor_verdict(None) == "unknown"
    assert model_floor_verdict(300) == "bad_model_value"
    assert model_floor_verdict(6200) == "ok"
    assert model_floor_verdict(80000) == "above_max"
    # дешёвая модель, listing 8000 — не кандидат
    assert listing_and_floor_reason(listing_stars=8000, floor=350) == "REJECT_BAD_MODEL_VALUE"
    # нормальная модель
    assert listing_and_floor_reason(listing_stars=7000, floor=6200) == ""
    # UNKNOWN не притворяемся
    assert listing_and_floor_reason(listing_stars=8000, floor=None) == "floor неизвестен"


def test_cheap_model_high_listing_rejected_before_girl() -> None:
    lot = _lot(stars=8000, model_id=111, model_floor=350.0)
    assert filter_lot(
        lot,
        min_stars=config.MIN_STARS,
        max_stars=config.MAX_STARS,
        max_level=config.MAX_ACCOUNT_LEVEL,
        max_nfts=config.MAX_NFTS,
    ) == "REJECT_BAD_MODEL_VALUE"


def test_unknown_floor_not_invented() -> None:
    from floors import FloorCatalog

    cat = FloorCatalog(path=None)
    cat.observe_model(1, 99, "Cheap")
    assert cat.get_floor(1, 99) is None
    assert cat.stats()["model_floor_unknown"] == 1
    assert cat.eligible_model_ids(1) == []


def test_floor_catalog_min_price_is_floor() -> None:
    from floors import FloorCatalog

    cat = FloorCatalog(path=None)
    cat.observe_floor(10, 5, 9000, "Rare")
    cat.observe_floor(10, 5, 6200, "Rare")
    cat.observe_floor(10, 5, 8800, "Rare")
    assert cat.get_floor(10, 5) == 6200
    assert 10 in cat.scan_collection_ids()


def test_floor_catalog_ttl_and_persist(tmp_path=None) -> None:
    import tempfile
    from pathlib import Path

    from floors import FloorCatalog

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "floors.json"
        cat = FloorCatalog(path)
        cat.observe_floor(1, 2, 5000, "A")
        cat.updated_at = 1.0
        cat.save()
        cat2 = FloorCatalog(path)
        cat2.load()
        assert cat2.get_floor(1, 2) == 5000
        assert cat2.is_fresh(now=1.0 + config.FLOOR_CACHE_TTL + 10) is False
        cat2.updated_at = time_module_now = __import__("time").time()
        assert cat2.is_fresh() is True


def test_apply_listing_floor_filters_paths() -> None:
    from floors import FloorCatalog
    from tracker import apply_listing_floor_filters, empty_funnel

    cat = FloorCatalog(path=None)
    cat.observe_floor(1, 10, 350, "Cheap")
    cat.observe_floor(1, 20, 6200, "Rare")
    cheap = _lot(id="c", slug="C-1", stars=8000, collection_id=1, model_id=10)
    rare = _lot(id="r", slug="R-1", stars=7000, collection_id=1, model_id=20)
    unknown = _lot(id="u", slug="U-1", stars=8000, collection_id=1, model_id=30)
    fn = empty_funnel()
    kept = apply_listing_floor_filters([cheap, rare, unknown], cat, fn)
    assert [x.id for x in kept] == ["r"]
    assert fn["bad_model_value"] == 1
    assert fn["model_floor_pass"] == 1
    assert fn["model_floor_unknown"] == 1


def test_scan_ids_prioritize_listing_band() -> None:
    from floors import FloorCatalog

    cat = FloorCatalog(path=None)
    cat.observe_floor(1, 1, 4500, "Low")  # eligible but below listing 5k
    cat.observe_floor(2, 2, 8000, "Mid")  # in listing band
    cat.observe_floor(3, 3, 200, "Junk")  # not eligible
    ids = cat.scan_collection_ids([1, 2, 3])
    assert 2 in ids
    assert 1 in ids
    assert 3 not in ids
    assert ids[0] == 2


def test_next_batch_rings_eligible_pool() -> None:
    market = TelegramMarket.__new__(TelegramMarket)
    market.gift_ids = list(range(100))
    market._cursor = 0
    pool = [10, 20, 30, 40]
    a = market.next_batch(2, pool=pool)
    b = market.next_batch(2, pool=pool)
    assert a == [10, 20]
    assert b == [30, 40]


def test_parse_gift_sets_model_id() -> None:
    from types import SimpleNamespace

    from market import parse_gift

    class StarGiftAttributeModel:
        def __init__(self) -> None:
            self.name = "Avatar"
            self.document = SimpleNamespace(id=555)

    class StarsAmt:
        amount = 8000

    gift = SimpleNamespace(
        id=1,
        slug="Gift-1",
        title="Gift",
        num=1,
        resell_amount=[StarsAmt()],
        attributes=[StarGiftAttributeModel()],
        owner_id=9,
        gift_id=77,
    )
    lot = parse_gift(gift)
    assert lot is not None
    assert lot.model == "Avatar"
    assert lot.model_id == 555
    assert lot.model_floor is None


def test_owner_dup_enqueue_counter() -> None:
    fn = empty_funnel()
    record_enqueue_dup(fn, "seller")
    assert fn["owner_dup_enqueue"] == 1
    assert fn["owner_dup_post_enrich"] == 0
    assert fn["owner_dup_send_guard"] == 0
    assert fn["send_attempt"] == 0


def test_owner_dup_post_enrich_counter() -> None:
    fn = empty_funnel()
    record_worker_filter(fn, _lot(first_name="Мария"), "дубль продавца")
    assert fn["owner_dup_post_enrich"] == 1
    assert fn["owner_dup_enqueue"] == 0
    assert fn["send_attempt"] == 0


def test_owner_dup_send_guard_no_send_attempt() -> None:
    import time

    from tracker import owner_dup_send_guard, persist_sent_owner

    seen: dict = {}
    first = _lot(id="A1", seller="alice", seller_id=77)
    persist_sent_owner(seen, first, time.time())
    later = _lot(id="B2", seller="alice_new", seller_id=77)
    assert owner_dup_send_guard(later, seen) == "дубль продавца"
    fn = empty_funnel()
    _bump = __import__("tracker")._bump
    _bump(fn, "owner_dup_send_guard")
    assert fn["owner_dup_send_guard"] == 1
    assert fn["send_attempt"] == 0


def test_owner_sent_persisted_key_is_id() -> None:
    import time

    from tracker import persist_sent_owner

    seen: dict = {}
    persist_sent_owner(seen, _lot(seller="x", seller_id=42), time.time())
    assert "id:42" in seen
    assert seen["id:42"] > 0
    fn = empty_funnel()
    __import__("tracker")._bump(fn, "owner_sent_persisted")
    assert fn["owner_sent_persisted"] == 1


def test_reload_seen_sellers_from_disk() -> None:
    import json
    import tempfile
    import time
    from pathlib import Path

    from tracker import persist_sent_owner, reload_seen_sellers

    lot = _lot(id="A", seller="ann", seller_id=9)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        persist_sent_owner({}, lot, time.time())  # warmup
        payload = {"seen_sellers": {"id:9": time.time(), "u:ann": time.time()}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        dest: dict = {}
        reload_seen_sellers(path, dest)
        other = _lot(id="B", slug="B-1", seller="renamed", seller_id=9)
        q = _stub_queue(seen_sellers=dest)
        assert q.enqueue([other]) == 0
        assert q.runtime.funnel["owner_dup_enqueue"] == 1


def test_post_enrich_stops_when_id_appears_after_enqueue() -> None:
    import time

    from tracker import owner_dup_after_enrich, persist_sent_owner

    q = _stub_queue()
    first = _lot(id="A", slug="A-1", seller="", seller_id=None)
    second = _lot(id="B", slug="B-1", seller="", seller_id=None)
    assert q.enqueue([first, second]) == 2
    persist_sent_owner(q.seen_sellers, _lot(id="A", seller="", seller_id=55), time.time())
    second.seller_id = 55
    assert owner_dup_after_enrich(second, q.seen_sellers) == "дубль продавца"


def test_same_nft_unknown_owner_not_resent() -> None:
    q = _stub_queue(seen={"U1": 1.0})
    again = _lot(id="U1", slug="Gift-1", seller="", seller_id=None)
    assert q.enqueue([again]) == 0
    assert q.runtime.funnel["dup_listing"] == 1


def test_owner_id_missing_reason() -> None:
    from tracker import owner_dup_after_enrich

    lot = _lot(seller="", seller_id=None)
    assert owner_dup_after_enrich(lot, {}) == "нет продавца"


def test_fresh_from_page_semantics_unchanged_v510() -> None:
    a = _lot(id="a", stars=8000)
    b = _lot(id="b", stars=9000)
    page, fresh = fresh_from_page(None, [a, b], {}, config.MIN_STARS, config.MAX_STARS)
    assert fresh == []
    neu = _lot(id="new1", stars=7000)
    _, fresh = fresh_from_page(["a", "b"], [neu, a], {}, config.MIN_STARS, config.MAX_STARS)
    assert [x.id for x in fresh] == ["new1"]
    assert config.POST_INTERVAL == 4.0
    assert config.RPC_CONCURRENCY <= config.SCAN_PARALLEL
    assert config.TRACKER_VERSION == "5.10.0"


def test_config_floor_thresholds_from_env_defaults() -> None:
    assert config.MIN_MODEL_FLOOR == 4000
    assert config.MAX_MODEL_FLOOR == 27000
    assert config.LISTING_PRICE_TOLERANCE == 0.0
    assert config.FLOOR_CACHE_TTL == 1800.0
    assert config.PAGE_LIMIT == 12


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
        test_extract_owner_user_id_peer_and_raw_int,
        test_next_batch_ring_advances_cursor,
        test_scan_batch_default_is_parallel_wave_not_all,
        test_scan_scheduling_does_not_change_detection,
        test_two_lots_same_owner_id_only_first_enqueued,
        test_same_owner_id_different_username_blocked,
        test_hidden_username_known_owner_id_dedupes,
        test_two_unknown_owners_without_id_not_merged,
        test_persistent_seen_sellers_survives_reload,
        test_owner_dup_after_enrich_uses_id_not_username,
        test_hidden_owner_unique_gift_fallback,
        test_hidden_owner_fulluser_via_input_entity,
        test_hidden_owner_stays_unknown_when_api_has_no_username,
        test_cached_entity_without_username_is_not_success,
        test_parse_gift_owner_id_raw_int,
        test_status_html_safe_diagnostics,
        test_listing_vs_model_floor_split,
        test_cheap_model_high_listing_rejected_before_girl,
        test_unknown_floor_not_invented,
        test_floor_catalog_min_price_is_floor,
        test_floor_catalog_ttl_and_persist,
        test_apply_listing_floor_filters_paths,
        test_scan_ids_prioritize_listing_band,
        test_next_batch_rings_eligible_pool,
        test_parse_gift_sets_model_id,
        test_owner_dup_enqueue_counter,
        test_owner_dup_post_enrich_counter,
        test_owner_dup_send_guard_no_send_attempt,
        test_owner_sent_persisted_key_is_id,
        test_reload_seen_sellers_from_disk,
        test_post_enrich_stops_when_id_appears_after_enqueue,
        test_same_nft_unknown_owner_not_resent,
        test_owner_id_missing_reason,
        test_fresh_from_page_semantics_unchanged_v510,
        test_config_floor_thresholds_from_env_defaults,
    ]
    for fn in tests:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nВсе {len(tests)} тестов прошли")


if __name__ == "__main__":
    main()
