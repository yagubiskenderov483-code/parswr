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
    detect_fresh_lots,
    empty_funnel,
    format_funnel_report,
    format_lot,
    fresh_from_page,
    funnel_invariants,
    load_state,
    collection_is_primed,
    model_request_key,
    record_enqueue_dup,
    record_fresh_price_seen,
    record_work_in,
    record_worker_filter,
    save_state,
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
    assert config.BOT_TOKEN == "8825465611:AAGVEabGitYdpQeACvJDkN3pkmrGqK9Ze5g"
    assert config.API_ID == 28687552
    assert config.API_HASH == "1abf9a58d0c22f62437bec89bd6b27a3"
    assert config.SCAN_BATCH == 0
    assert config.RPC_CONCURRENCY == 6
    assert config.RPC_CONCURRENCY <= config.SCAN_PARALLEL
    assert config.PAGE_LIMIT == 12
    assert config.SCAN_PARALLEL == 12
    assert config.TRACKER_VERSION == "5.13.2"
    assert config.SCAN_MODEL_CHUNK == 0
    assert config.SCAN_MAX_PAGES == 2
    assert config.SCAN_SEED_PAGES == 8
    assert config.PAGE_SNAPSHOT_KEEP == 120
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
    """SCAN_BATCH=0 — все eligible коллекции за round."""
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
        assert "lot1" in data["observed"]


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
    assert d.female_pass == 1
    assert d.no_identity_reject == 1
    assert fx2["female_confident"] is True


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
    male_user = SimpleNamespace(
        id=2,
        username="ivan",
        first_name="Иван",
        last_name="",
        premium=False,
        lang_code="",
        photo=None,
        emoji_status=None,
        stars_rating=None,
        usernames=None,
        gender="male",
    )
    male_lot = _lot(seller="")
    fill_user(male_lot, male_user, username_source="resale_user")
    assert male_lot.api_gender == "male"


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


def test_scan_batch_default_is_all_collections() -> None:
    """Default SCAN_BATCH=0 — все id пула за round."""
    assert config.SCAN_BATCH == 0
    market = TelegramMarket.__new__(TelegramMarket)
    market.gift_ids = list(range(40))
    market._cursor = 0
    batch = market.next_batch(config.SCAN_BATCH)
    assert sorted(batch) == list(range(40))


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
    assert "REJECTION REASONS" in text
    assert "score_below_threshold=" in text
    assert "paid_dm=" in text
    assert "gifts_count_gt" not in text or "gifts_count_gt_MAX_NFTS" in text
    assert "score<" not in text


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
    assert config.TRACKER_VERSION == "5.13.2"


def test_config_floor_thresholds_from_env_defaults() -> None:
    assert config.MIN_MODEL_FLOOR == 4000
    assert config.MAX_MODEL_FLOOR == 27000
    assert config.LISTING_PRICE_TOLERANCE == 0.0
    assert config.FLOOR_CACHE_TTL == 1800.0
    assert config.PAGE_LIMIT == 12


def test_price_reject_below_and_above() -> None:
    from diagnostics import Diagnostics, price_reject_side

    assert price_reject_side(config.MIN_STARS - 1) == "below"
    assert price_reject_side(config.MAX_STARS + 1) == "above"
    assert price_reject_side(8000) == "other"
    d = Diagnostics()
    d.record_price_reject(100)
    d.record_price_reject(80_000)
    d.record_price_reject(8000)
    assert d.price_reject_below == 1
    assert d.price_reject_above == 1
    lines = "\n".join(d.rejection_reason_lines())
    assert "price: below=1 above=1" in lines
    assert "<" not in lines


def test_seen_reject_listing_owner_other() -> None:
    from diagnostics import Diagnostics
    from tracker import empty_funnel, record_enqueue_dup

    d = Diagnostics()
    d.record_seen_reason("listing")
    d.record_seen_reason("listing")
    fn = empty_funnel()
    record_enqueue_dup(fn, "seller", d)
    record_enqueue_dup(fn, "listing", d, seen_kind="other")
    assert d.seen_listing == 2
    assert d.seen_owner == 1
    assert d.seen_other == 1
    assert fn["dup_seller"] == 1
    assert fn["dup_listing"] == 1
    lines = "\n".join(d.rejection_reason_lines())
    assert "seen: listing=2 owner=1 other=1" in lines


def test_male_ru_girl_dm_level_reject_reasons() -> None:
    from diagnostics import Diagnostics
    from tracker import _record_filter_diagnostics, empty_funnel, record_worker_filter

    d = Diagnostics()
    fn = empty_funnel()
    male = _lot(first_name="Алексей", seller="lexa")
    _record_filter_diagnostics(d, male, "мужской")
    record_worker_filter(fn, male, "мужской")
    assert d.male_reject_reasons["male_name"] >= 1
    assert fn["male_reject"] == 1
    assert fn["ru_checked"] == 0

    foreign = _lot(lang_code="fa", first_name="Shop", about="store", seller="gift_market_fa")
    _record_filter_diagnostics(d, foreign, "не русский")
    record_worker_filter(fn, foreign, "не русский")
    assert d.ru_reject_foreign_lang == 1

    no_cyr = _lot(first_name="GiftShop", last_name="", about="best deals", seller="shop99")
    _record_filter_diagnostics(d, no_cyr, "не русский")
    record_worker_filter(fn, no_cyr, "не русский")
    assert d.ru_reject_no_cyrillic == 1

    no_id = _lot(
        first_name="Seller",
        last_name="",
        seller="nft_market",
        about="привет 💅",
        has_photo=True,
        emoji_status="",
        gifts_text="",
        stories_text="",
        personal_channel="",
    )
    _record_filter_diagnostics(d, no_id, "нет женских признаков")
    record_worker_filter(fn, no_id, "нет женских признаков")
    assert d.girl_reject_no_identity == 1

    paid = _lot(first_name="Мария", free_dm=False, paid_dm_stars=50)
    _record_filter_diagnostics(d, paid, "платные ЛС")
    record_worker_filter(fn, paid, "платные ЛС")
    assert d.dm_paid == 1
    assert fn["dm_reject"] == 1
    assert fn["nft_checked"] == 0

    high = _lot(first_name="Мария", account_level=5)
    _record_filter_diagnostics(d, high, "level")
    record_worker_filter(fn, high, "level")
    assert d.level_above_limit == 1
    assert fn["level_reject"] == 1
    assert fn["nft_pass"] == 0

    unknown = _lot(first_name="Мария", account_level=None)
    assert filter_lot(
        unknown,
        min_stars=config.MIN_STARS,
        max_stars=config.MAX_STARS,
        max_level=config.MAX_ACCOUNT_LEVEL,
        max_nfts=config.MAX_NFTS,
    ) == ""
    fn2 = empty_funnel()
    d2 = Diagnostics()
    _record_filter_diagnostics(d2, unknown, "")
    record_worker_filter(fn2, unknown, "")
    assert d2.level_unknown == 1
    assert d2.level_above_limit == 0
    assert fn2["level_pass"] == 1
    assert fn2["level_reject"] == 0
    assert fn2["nft_pass"] == 1

    lines = "\n".join(d.rejection_reason_lines())
    assert "foreign=1" in lines
    assert "no_cyrillic=1" in lines
    assert "no_identity=1" in lines
    assert "score_below_threshold=" in lines
    assert "paid_dm=1" in lines
    assert "above_limit=1" in lines
    assert "<" not in lines


def test_nft_reject_reason_count_limit_and_log() -> None:
    import logging

    from diagnostics import Diagnostics, nft_reject_details
    from filters import passes_nfts
    from tracker import _record_filter_diagnostics, empty_funnel, record_worker_filter

    over = _lot(
        first_name="Мария",
        gifts_count=7,
        stars=8000,
        model_id=4242,
        model_floor=6200.0,
        seller="maria_ok",
        seller_id=111,
    )
    assert passes_nfts(over, config.MAX_NFTS) is False
    assert filter_lot(
        over,
        min_stars=config.MIN_STARS,
        max_stars=config.MAX_STARS,
        max_level=config.MAX_ACCOUNT_LEVEL,
        max_nfts=config.MAX_NFTS,
    ) == "много NFT"
    info = nft_reject_details(over)
    assert info["rejects"] is True
    assert info["reason"] == "count_above_limit"
    assert info["nft_count"] == 7
    assert info["nft_limit"] == config.MAX_NFTS
    assert info["condition"] == "gifts_count_gt_MAX_NFTS"

    unknown = _lot(first_name="Мария", gifts_count=None)
    assert passes_nfts(unknown, config.MAX_NFTS) is None
    assert filter_lot(
        unknown,
        min_stars=config.MIN_STARS,
        max_stars=config.MAX_STARS,
        max_level=config.MAX_ACCOUNT_LEVEL,
        max_nfts=config.MAX_NFTS,
    ) == ""
    uinfo = nft_reject_details(unknown)
    assert uinfo["rejects"] is False
    assert uinfo["reason"] == "unknown_count"

    ok = _lot(first_name="Мария", gifts_count=6)
    assert passes_nfts(ok, config.MAX_NFTS) is True

    d = Diagnostics()
    fn = empty_funnel()
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    h = _H()
    h.setLevel(logging.INFO)
    log = logging.getLogger("diagnostics")
    log.addHandler(h)
    log.setLevel(logging.INFO)
    try:
        _record_filter_diagnostics(d, over, "много NFT")
        record_worker_filter(fn, over, "много NFT")
    finally:
        log.removeHandler(h)

    assert fn["nft_reject"] == 1
    assert fn["nft_pass"] == 0
    assert d.nft_reject_reasons["count_above_limit"] == 1
    assert d.nft_reject_counts[7] == 1
    assert d.nft_limit_last == 6
    assert d.nft_reject_conditions["gifts_count_gt_MAX_NFTS"] == 1
    msgs = [r.getMessage() for r in records]
    nft_logs = [m for m in msgs if m.startswith("NFT_REJECT ")]
    assert len(nft_logs) == 1
    msg = nft_logs[0]
    assert "reason=count_above_limit" in msg
    assert "listing=8000" in msg
    assert "floor=6200" in msg
    assert "model=4242" in msg
    assert "owner_known=true" in msg
    assert "nft_count=7" in msg
    assert "nft_limit=6" in msg
    assert "cond=gifts_count_gt_MAX_NFTS" in msg
    assert "maria_ok" not in msg
    assert "Мария" not in msg
    assert "@" not in msg
    lines = "\n".join(d.rejection_reason_lines())
    assert "count_above_limit=1" in lines
    assert "nft_count=7x1" in lines
    assert "nft_limit=6" in lines
    assert "gifts_count_gt_MAX_NFTS" in lines
    assert "<" not in lines
    hidden = _lot(
        first_name="Мария",
        gifts_count=12,
        stars=9000,
        model_id=None,
        model_floor=None,
        seller="",
        seller_id=None,
    )
    d.record_nft_reject(hidden)
    assert d.nft_reject_counts[12] == 1
    lines2 = "\n".join(d.rejection_reason_lines())
    assert "nft_count=7x1,12x1" in lines2


def test_status_rejection_reasons_section() -> None:
    import re

    from bot import ControlBot
    from tracker import Runtime, _record_filter_diagnostics

    ctrl = ControlBot.__new__(ControlBot)
    ctrl.authorized = True
    ctrl.account_name = "tester"
    ctrl.runtime = Runtime()
    rt = ctrl.runtime
    rt.snapshot_ready = True
    rt.snapshot = 10
    rt.passes = 1
    rt.collections = 151
    rt.funnel["fresh_detected"] = 10
    rt.funnel["fresh"] = 10
    d = rt.diag
    d.record_price_reject(100)
    d.record_price_reject(99_000)
    d.record_seen_reason("listing")
    nft = _lot(first_name="Мария", gifts_count=9, stars=7777, model_id=1, model_floor=5000)
    _record_filter_diagnostics(d, nft, "много NFT")
    text = ControlBot._status_text(ctrl)
    assert "REJECTION REASONS" in text
    assert "price: below=1 above=1" in text
    assert "seen: listing=1" in text
    assert "count_above_limit=1" in text
    assert "nft_limit=6" in text
    assert "score_below_threshold=" in text
    assert "paid_dm=" in text
    assert "score<" not in text
    assert re.search(r"<\d", text) is None


def test_rejection_diagnostics_do_not_change_filters() -> None:
    """Счётчики причин не ослабляют и не меняют filter_lot / passes_*."""
    from filters import passes_free_dm, passes_level, passes_nfts

    girl = _lot(first_name="Мария", gifts_count=4, account_level=1, free_dm=True)
    assert filter_lot(
        girl,
        min_stars=config.MIN_STARS,
        max_stars=config.MAX_STARS,
        max_level=config.MAX_ACCOUNT_LEVEL,
        max_nfts=config.MAX_NFTS,
    ) == ""
    assert passes_nfts(girl, 6) is True
    assert passes_level(girl, 2) is True
    assert passes_free_dm(girl) is True
    none_nft = _lot(first_name="Мария", gifts_count=None)
    assert passes_nfts(none_nft, 6) is None
    none_lvl = _lot(first_name="Мария", account_level=None)
    assert passes_level(none_lvl, 2) is None
    paid = _lot(first_name="Мария", free_dm=False)
    assert passes_free_dm(paid) is False
    over = _lot(first_name="Мария", gifts_count=7)
    assert passes_nfts(over, 6) is False
    hi = _lot(first_name="Мария", account_level=3)
    assert passes_level(hi, 2) is False


def test_model_chunk_rotates_eligible_models() -> None:
    market = TelegramMarket.__new__(TelegramMarket)
    market._model_cursors = {}
    models = [10, 20, 30, 40, 50, 60, 70, 80, 90]
    a = market.next_model_chunk(7, models, 3)
    b = market.next_model_chunk(7, models, 3)
    c = market.next_model_chunk(7, models, 3)
    assert a == [10, 20, 30]
    assert b == [40, 50, 60]
    assert c == [70, 80, 90]
    d = market.next_model_chunk(7, models, 3)
    assert d == [10, 20, 30]
    all_models = market.next_model_chunk(8, models, 0)
    assert all_models == models


def test_fetch_newest_stops_when_page_all_known() -> None:
    import asyncio

    m = TelegramMarket.__new__(TelegramMarket)
    calls = {"n": 0}

    async def fp(*_a, **_k):
        calls["n"] += 1
        m.last_next_offset = "p2"
        if calls["n"] == 1:
            return [_lot(id="a", stars=8000), _lot(id="b", stars=8000)]
        return [_lot(id="c", stars=8000)]

    m.fetch_page = fp
    m.last_next_offset = ""

    async def run():
        return await TelegramMarket.fetch_newest_until_known(
            m,
            1,
            model_ids=[11],
            known_ids={"a", "b"},
            seen={},
            max_pages=3,
            limit=12,
        )

    lots, meta = asyncio.run(run())
    assert calls["n"] == 1
    assert meta["pages"] == 1
    assert meta["old"] == 2
    assert meta["new"] == 0
    assert [x.id for x in lots] == ["a", "b"]


def test_fetch_newest_depth_beyond_page_limit_can_be_new() -> None:
    import asyncio

    m = TelegramMarket.__new__(TelegramMarket)
    calls = {"n": 0}

    async def fp(*_a, offset="", **_k):
        calls["n"] += 1
        if not offset:
            m.last_next_offset = "p2"
            return [_lot(id=f"k{i}", stars=8000) for i in range(11)] + [
                _lot(id="new0", stars=8000)
            ]
        m.last_next_offset = ""
        return [_lot(id="new12", stars=9000)] + [
            _lot(id=f"z{i}", stars=8000) for i in range(11)
        ]

    m.fetch_page = fp
    m.last_next_offset = ""
    known = {f"k{i}" for i in range(11)} | {f"z{i}" for i in range(11)}

    async def run():
        return await TelegramMarket.fetch_newest_until_known(
            m,
            1,
            model_ids=[22],
            known_ids=known,
            seen={},
            max_pages=2,
            limit=12,
        )

    lots, meta = asyncio.run(run())
    assert calls["n"] == 2
    assert meta["pages"] == 2
    assert meta["new"] == 2
    assert meta["depths"]["new12"] >= 12
    assert "new12" in {x.id for x in lots}


def test_merge_page_snapshot_keeps_more_than_top12() -> None:
    from tracker import merge_page_snapshot

    prev = [f"old{i}" for i in range(20)]
    page = [f"new{i}" for i in range(12)]
    out = merge_page_snapshot(prev, page, keep=80)
    assert len(out) == 32
    assert out[:12] == page
    assert "old0" in out
    lots = [_lot(id="old0", stars=8000), _lot(id="brand_new", stars=8000)]
    _, fresh = fresh_from_page(out, lots, {}, config.MIN_STARS, config.MAX_STARS)
    assert [x.id for x in fresh] == ["brand_new"]


def test_scan_discovery_metrics_show_where_new_listings_go() -> None:
    from diagnostics import Diagnostics

    d = Diagnostics()
    d.record_scan_discovery(
        101,
        new_n=3,
        old_n=9,
        pages=2,
        models=6,
        fresh_candidates=2,
        depths={"a": 0, "b": 4, "c": 15},
        eligible=True,
    )
    d.note_new_candidates(101, 1)
    assert d.new_listing_seen == 3
    assert d.old_listing_seen == 9
    assert d.listing_page_depth_max == 15
    assert d.collections_scanned == 1
    assert d.eligible_collections_scanned == 1
    assert d.new_candidates_per_collection["101"] == 3
    summary = d.new_candidates_summary()
    assert "n=3" in summary
    assert "<" not in summary


def test_same_owner_simultaneous_workers_only_one_send() -> None:
    import threading

    from tracker import claim_owner_for_send

    seen: dict[str, float] = {}
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(lot: Lot) -> None:
        barrier.wait()
        results.append(claim_owner_for_send(lot, seen))

    a = _lot(id="nft1", slug="A-1", seller="ann", seller_id=123)
    b = _lot(id="nft2", slug="B-1", seller="ann_new", seller_id=123)
    t1 = threading.Thread(target=worker, args=(a,))
    t2 = threading.Thread(target=worker, args=(b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results.count("") == 1
    assert results.count("дубль продавца") == 1
    assert "id:123" in seen


def test_same_owner_restart_second_nft_blocked() -> None:
    import tempfile
    from pathlib import Path

    from tracker import (
        claim_owner_for_send,
        persist_sent_owner,
        reload_seen_sellers,
        save_state,
    )

    first = _lot(id="nft1", slug="A-1", seller="ann", seller_id=777)
    second = _lot(id="nft2", slug="B-1", seller="hidden", seller_id=777)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        seen: dict[str, float] = {}
        assert claim_owner_for_send(first, seen) == ""
        persist_sent_owner(seen, first)
        save_state(path, {"seen": {}, "seen_sellers": seen, "market_ids": []})
        restored: dict[str, float] = {}
        reload_seen_sellers(path, restored)
        assert claim_owner_for_send(second, restored) == "дубль продавца"


def test_female_gate_rejects_male_name_plus_female_emoji() -> None:
    from filters import female_confident, girl_reject_reason, male_reject_reason

    lot = _lot(first_name="Алексей", about="💅🎀💖", seller="lexa_shop", has_photo=True)
    assert looks_male(lot) is True
    assert female_confident(lot) is False
    assert is_girl(lot) is False
    assert male_reject_reason(lot) == "male_name"
    assert girl_reject_reason(lot) == "male"


def test_female_gate_rejects_male_username_plus_female_bio() -> None:
    from filters import female_confident, male_reject_reason

    lot = _lot(
        first_name="Shop",
        about="девушка pink 💅",
        seller="ivan_nft",
        gifts_text="Rose Heart",
        has_photo=True,
    )
    assert looks_male(lot) is True
    assert female_confident(lot) is False
    assert is_girl(lot) is False
    assert male_reject_reason(lot) == "male_username"


def test_female_gate_rejects_danila_despite_a_ending() -> None:
    from filters import female_confident

    lot = _lot(first_name="Данила", about="🎀 девушка", seller="danila_gift")
    assert looks_male(lot) is True
    assert female_confident(lot) is False
    assert is_girl(lot) is False


def test_female_gate_rejects_male_plus_devushka_word() -> None:
    lot = _lot(first_name="Никита", about="девушка, пишите", seller="nikita")
    assert looks_male(lot) is True
    assert is_girl(lot) is False


def test_female_gate_rejects_gifts_emoji_photo_only() -> None:
    from filters import female_confident

    lot = _lot(
        first_name="Lee",
        last_name="",
        about="",
        seller="lee_shop",
        gifts_text="Rose Heart Perfume Bouquet",
        stories_text="🌸",
        has_photo=True,
        emoji_status="💅",
    )
    assert looks_male(lot) is False
    assert female_confident(lot) is False
    assert is_girl(lot) is False


def test_female_gate_rejects_empty_and_latin_nickname() -> None:
    from filters import female_confident, girl_reject_reason

    empty = _lot(first_name="", last_name="", about="", seller="xx_store")
    empty.first_name = ""
    empty.about = ""
    assert female_confident(empty) is False
    assert girl_reject_reason(empty) == "no_identity"
    nick = _lot(first_name="Sunny", about="hi", seller="sunny_xx", lang_code="")
    assert looks_male(nick) is False
    assert female_confident(nick) is False
    assert is_girl(nick) is False


def test_female_gate_rejects_ambiguous_sasha() -> None:
    from filters import female_confident, girl_reject_reason

    lot = _lot(first_name="Саша", about="торгую гифтами 💅", seller="sasha_nft")
    assert is_girl(lot) is False
    assert female_confident(lot) is False
    assert girl_reject_reason(lot) in {"male", "ambiguous"}


def test_female_gate_api_gender_male_overrides_female_name() -> None:
    from filters import female_confident, male_reject_reason

    lot = _lot(first_name="Мария", about="привет", seller="masha_nft")
    lot.api_gender = "male"
    assert looks_male(lot) is True
    assert female_confident(lot) is False
    assert is_girl(lot) is False
    assert male_reject_reason(lot) == "male_explicit"


def test_female_gate_keeps_confident_maria() -> None:
    from filters import female_confident, girl_reject_reason

    lot = _lot()
    assert female_confident(lot) is True
    assert is_girl(lot) is True
    assert girl_reject_reason(lot) == "ok"


def _detect(
    lots: list[Lot],
    *,
    observed: dict | None = None,
    primed: dict | None = None,
    pages: dict | None = None,
    seen: dict | None = None,
    collection_id: int = 1,
    model_ids: list[int] | None = None,
    round_hits: dict | None = None,
    stats: dict | None = None,
    request_ok: bool = True,
) -> tuple[list[Lot], dict, dict, dict, dict]:
    observed = observed if observed is not None else {}
    primed = primed if primed is not None else {}
    pages = pages if pages is not None else {}
    seen = seen if seen is not None else {}
    round_hits = round_hits if round_hits is not None else {}
    stats = stats if stats is not None else empty_funnel()
    fresh, _verdicts = detect_fresh_lots(
        lots,
        observed=observed,
        primed=primed,
        pages=pages,
        seen=seen,
        collection_id=collection_id,
        model_ids=model_ids if model_ids is not None else [10],
        round_hits=round_hits,
        stats=stats,
        request_ok=request_ok,
    )
    return fresh, observed, primed, pages, stats


def test_new_listing_seen_is_not_page_absence() -> None:
    """new_listing_seen / GENUINE_NEW ≠ «нет в текущей page snapshot»."""
    from market import TelegramMarket

    m = TelegramMarket.__new__(TelegramMarket)

    def _old(lot: Lot, known_ids: set[str], seen: dict) -> bool:
        if lot.id in known_ids or lot.id in seen:
            return True
        if lot.slug and lot.slug in seen:
            return True
        return False

    lot = _lot(id="oldA", slug="OldA-1", stars=8000)
    # Нет в текущей page snapshot, но уже observed → OLD, не NEW
    assert _old(lot, known_ids={"other"}, seen={}) is False
    fresh, observed, primed, pages, stats = _detect(
        [lot], observed={"oldA": {"first": 1.0, "last": 1.0}}, primed={"1:10": 1.0}
    )
    assert fresh == []
    assert stats["genuine_new"] == 0
    assert stats["observed_old"] == 1
    _ = m


def test_a_old_listing_after_restart() -> None:
    """A) scan#1 listing A; restart; scan#3 — A не fresh."""
    import tempfile
    from pathlib import Path

    a = _lot(id="A", slug="Gift-A", stars=8000, collection_id=7, model_id=10)
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    seen: dict = {}
    fresh, observed, primed, pages, stats = _detect(
        [a],
        observed=observed,
        primed=primed,
        pages=pages,
        seen=seen,
        collection_id=7,
        model_ids=[10],
    )
    assert fresh == []
    assert stats["unprimed_seed"] == 1
    assert stats["genuine_new"] == 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        save_state(
            path,
            {
                "seen": seen,
                "seen_sellers": {},
                "skip_sellers": {},
                "market_ids": [],
                "heads": {},
                "pages": pages,
                "observed": observed,
                "primed_models": primed,
            },
        )
        loaded = load_state(path)
    fresh2, *_rest = _detect(
        [a],
        observed=loaded["observed"],
        primed=loaded["primed_models"],
        pages=loaded["pages"],
        seen=loaded["seen"],
        collection_id=7,
        model_ids=[10],
    )
    assert fresh2 == []
    assert "A" in loaded["observed"]


def test_b_old_listing_on_next_page() -> None:
    """B) listing на странице 2 после seed не становится NEW."""
    p1 = [_lot(id="p1a", stars=8000, scan_page=1, scan_offset="")]
    p2 = [_lot(id="p2c", stars=9000, scan_page=2, scan_offset="off2")]
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    _detect(
        p1 + p2,
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=3,
        model_ids=[4],
    )
    fresh, *_ = _detect(
        p2,
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=3,
        model_ids=[4],
    )
    assert fresh == []


def test_c_same_listing_two_model_queries() -> None:
    """C) один listing_id в двух model queries — второй OLD."""
    lot = _lot(id="X", slug="X-1", stars=8000, model_id=1)
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    stats = empty_funnel()
    _detect(
        [lot],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=5,
        model_ids=[1],
        stats=stats,
    )
    lot_b = _lot(id="X", slug="X-1", stars=8000, model_id=2)
    primed[model_request_key(5, [2])] = 1.0
    fresh, *_rest, stats2 = _detect(
        [lot_b],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=5,
        model_ids=[2],
        stats=empty_funnel(),
    )
    assert fresh == []
    assert stats2["observed_old"] == 1
    assert stats2["genuine_new"] == 0


def test_d_same_listing_two_collections() -> None:
    """D) один listing_id в двух collection/request — второй OLD."""
    lot = _lot(id="Z", stars=8000, collection_id=1)
    observed: dict = {}
    primed: dict = {}
    _detect(
        [lot],
        observed=observed,
        primed=primed,
        collection_id=1,
        model_ids=[8],
    )
    primed[model_request_key(2, [8])] = 1.0
    fresh, *_rest, stats = _detect(
        [_lot(id="Z", stars=8000, collection_id=2)],
        observed=observed,
        primed=primed,
        collection_id=2,
        model_ids=[8],
        stats=empty_funnel(),
    )
    assert fresh == []
    assert stats["observed_old"] == 1


def test_e_new_listing_between_scan1_and_scan2() -> None:
    """E) новый listing между scan #1 и #2 — GENUINE_NEW."""
    a = _lot(id="keep", stars=8000)
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    _detect(
        [a],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[11],
    )
    neu = _lot(id="brand", slug="Brand-1", stars=7000)
    fresh, *_rest, stats = _detect(
        [neu, a],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[11],
        stats=empty_funnel(),
    )
    assert [x.id for x in fresh] == ["brand"]
    assert stats["genuine_new"] == 1
    assert stats["genuine_new_listings"] == 1
    assert stats["fresh_unique"] == 1
    assert stats["fresh_detected"] == 1


def test_f_reorder_existing_no_false_positive() -> None:
    """F) reorder существующих listings без false positive."""
    a = _lot(id="a", stars=8000)
    b = _lot(id="b", stars=9000)
    c = _lot(id="c", stars=10000)
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    _detect(
        [a, b, c],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=1,
        model_ids=[1],
    )
    fresh, *_rest, stats = _detect(
        [c, a, b],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=1,
        model_ids=[1],
        stats=empty_funnel(),
    )
    assert fresh == []
    assert stats["genuine_new"] == 0
    assert stats["observed_old"] == 3


def test_g_pagination_next_offset() -> None:
    """G) scanner берёт page 2 по next_offset; page2 seed ≠ NEW позже."""
    import asyncio

    m = TelegramMarket.__new__(TelegramMarket)
    calls = {"n": 0, "offsets": []}

    async def fp(*_a, offset="", **_k):
        calls["n"] += 1
        calls["offsets"].append(offset)
        if not offset:
            m.last_next_offset = "p2"
            return [_lot(id=f"k{i}", stars=8000) for i in range(12)]
        assert offset == "p2"
        m.last_next_offset = ""
        return [_lot(id=f"p2_{i}", stars=8000) for i in range(12)]

    m.fetch_page = fp
    m.last_next_offset = ""

    async def run():
        return await TelegramMarket.fetch_newest_until_known(
            m,
            1,
            model_ids=[22],
            known_ids=set(),
            seen={},
            max_pages=2,
            limit=12,
        )

    lots, meta = asyncio.run(run())
    assert calls["n"] == 2
    assert calls["offsets"] == ["", "p2"]
    assert meta["pages"] == 2
    assert lots[-1].scan_page == 2
    assert lots[-1].scan_offset == "p2"
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    fresh1, *_ = _detect(
        lots,
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=1,
        model_ids=[22],
    )
    assert fresh1 == []
    page2 = [x for x in lots if x.scan_page == 2]
    fresh2, *_rest, stats = _detect(
        page2,
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=1,
        model_ids=[22],
        stats=empty_funnel(),
    )
    assert fresh2 == []
    assert stats["genuine_new"] == 0


def test_h_snapshot_persistence() -> None:
    """H) observed + primed переживают save/load и schema migrate."""
    import json
    import tempfile
    from pathlib import Path

    from tracker import STATE_SCHEMA

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(
            json.dumps(
                {
                    "seen": {"legacy": 10.0},
                    "seen_sellers": {},
                    "pages": {"99": ["snap1", "snap2"]},
                    "schema": 10,
                }
            ),
            encoding="utf-8",
        )
        data = load_state(path)
        assert data["schema"] == STATE_SCHEMA
        assert "legacy" in data["observed"]
        assert "snap1" in data["observed"]
        assert "snap2" in data["observed"]
        assert data["pages"] == {}
        save_state(path, data)
        again = load_state(path)
        assert "snap1" in again["observed"]
        assert again["schema"] == STATE_SCHEMA


def test_round_dedup_same_listing_two_requests() -> None:
    lot = _lot(id="DUP", stars=8000, model_id=1)
    observed: dict = {}
    primed: dict = {"1:1": 1.0, "1:2": 1.0}
    round_hits: dict = {}
    stats = empty_funnel()
    fresh1, *_ = _detect(
        [lot],
        observed=observed,
        primed=primed,
        collection_id=1,
        model_ids=[1],
        round_hits=round_hits,
        stats=stats,
    )
    # primed + never observed → genuine first time
    assert [x.id for x in fresh1] == ["DUP"]
    lot2 = _lot(id="DUP", stars=8000, model_id=2)
    fresh2, *_ = _detect(
        [lot2],
        observed=observed,
        primed=primed,
        collection_id=1,
        model_ids=[2],
        round_hits=round_hits,
        stats=stats,
    )
    assert fresh2 == []
    assert stats["duplicate_listing_ids_same_round"] == 1
    assert stats["duplicate_listing_ids_across_models"] == 1


def test_forensic_verdict_fields() -> None:
    from diagnostics import Diagnostics

    lot = _lot(
        id="F1",
        stars=6400,
        collection_id=12,
        model_id=33,
        scan_page=2,
        scan_offset="off9",
        scan_source="scan:collection=12:models=33:page=2:offset=off9",
    )
    observed: dict = {}
    primed: dict = {}
    verdicts: list = []
    detect_fresh_lots(
        [lot],
        observed=observed,
        primed=primed,
        pages={},
        seen={},
        collection_id=12,
        model_ids=[33],
        round_hits={},
        forensic=verdicts,
    )
    assert len(verdicts) == 1
    row = verdicts[0]
    for key in (
        "collection_id",
        "model_id",
        "listing_id",
        "listing_price",
        "first_seen_at",
        "previous_seen_at",
        "snapshot_contains_before",
        "seen_contains_before",
        "page_number",
        "offset",
        "source_request",
        "reason",
    ):
        assert key in row
    assert row["listing_id"] == "F1"
    assert row["reason"] == "UNPRIMED_SEED"
    assert row["page_number"] == 2
    assert row["offset"] == "off9"
    d = Diagnostics()
    d.record_freshness_verdict(row)
    text = "\n".join(d.freshness_forensics_lines())
    assert "id=F1" in text
    assert "reason=UNPRIMED_SEED" in text


def test_1_old_listing_before_start_not_sent() -> None:
    """1) Старый listing до запуска — первый scan не отправляет."""
    lot = _lot(id="preexist", slug="Pre-1", stars=8000)
    q = _stub_queue()
    fresh, *_rest, stats = _detect([lot], collection_id=1, model_ids=[10])
    assert fresh == []
    assert stats["unprimed_seed"] == 1
    assert stats["genuine_new"] == 0
    assert q.enqueue(fresh) == 0
    assert q._items == []


def test_8_unprimed_seed_never_enqueued() -> None:
    """8) UNPRIMED_SEED никогда не попадает в очередь."""
    lots = [
        _lot(id="u1", slug="U-1", stars=8000),
        _lot(id="u2", slug="U-2", stars=9000),
        _lot(id="u3", slug="U-3", stars=12000),
    ]
    q = _stub_queue()
    fresh, _obs, primed, _pages, stats = _detect(
        lots, collection_id=4, model_ids=[7, 8]
    )
    assert fresh == []
    assert stats["unprimed_seed"] == 3
    assert stats["genuine_new_listings"] == 0
    assert model_request_key(4, [7, 8]) in primed
    assert q.enqueue(fresh) == 0
    assert q._items == []


def test_5_same_listing_page1_and_page2_one_genuine() -> None:
    """5) Один listing на page 1 и page 2 — ровно один GENUINE_NEW."""
    lot_p1 = _lot(id="SAME", slug="Same-1", stars=8000, scan_page=1, scan_offset="")
    lot_p2 = _lot(id="SAME", slug="Same-1", stars=8000, scan_page=2, scan_offset="p2")
    primed = {model_request_key(1, [10]): 1.0}
    stats = empty_funnel()
    fresh, *_ = _detect(
        [lot_p1, lot_p2],
        primed=primed,
        collection_id=1,
        model_ids=[10],
        stats=stats,
    )
    assert [x.id for x in fresh] == ["SAME"]
    assert stats["genuine_new"] == 1
    assert stats["duplicate_listing_ids_same_round"] == 1


def test_7_sync_pages_second_query_keeps_ids() -> None:
    """7) Второй sync_pages merge, не затирает ранее накопленные IDs."""
    import asyncio

    from tracker import Runtime, sync_pages

    observed: dict = {
        "keepA": {"first": 1.0, "last": 1.0, "c": 1, "m": 1},
        "keepB": {"first": 1.0, "last": 1.0, "c": 1, "m": 1},
    }
    primed: dict = {}
    pages = {"1:11": ["keepA", "keepB"]}
    market = TelegramMarket.__new__(TelegramMarket)
    market.floors = type("F", (), {"eligible_model_ids": staticmethod(lambda gid: [11])})()
    market.last_next_offset = ""

    async def fp(*_a, **_k):
        market.last_next_offset = ""
        return [_lot(id="newC", stars=8000, collection_id=1, model_id=11)]

    market.fetch_page = fp

    async def run():
        await sync_pages(
            market, [1], pages, Runtime(), observed=observed, primed=primed
        )

    asyncio.run(run())
    assert "keepA" in observed
    assert "keepB" in observed
    assert "newC" in observed
    assert "keepA" in pages["1:11"]
    assert "keepB" in pages["1:11"]
    assert "newC" in pages["1:11"]


def test_10_genuine_new_excludes_seen_and_observed() -> None:
    """10) GENUINE_NEW_LISTINGS не включает already seen/observed."""
    primed = {model_request_key(1, [10]): 1.0}
    seen_lot = _lot(id="seen1", slug="Seen-1", stars=8000)
    obs_lot = _lot(id="obs1", slug="Obs-1", stars=9000)
    fresh_seen, *_rest, stats_s = _detect(
        [seen_lot],
        primed=dict(primed),
        seen={"seen1": 1.0},
        stats=empty_funnel(),
    )
    fresh_obs, *_rest, stats_o = _detect(
        [obs_lot],
        observed={"obs1": {"first": 1.0, "last": 1.0}},
        primed=dict(primed),
        stats=empty_funnel(),
    )
    assert fresh_seen == []
    assert fresh_obs == []
    assert stats_s["genuine_new"] == 0
    assert stats_s["genuine_new_listings"] == 0
    assert stats_o["genuine_new"] == 0
    assert stats_o["observed_old"] == 1


def test_e_genuine_new_exactly_once() -> None:
    """3) Появление между scan#1 и #2 — GENUINE_NEW ровно один раз, scan#3 OLD."""
    a = _lot(id="keep", stars=8000)
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    _detect([a], observed=observed, primed=primed, pages=pages, collection_id=9, model_ids=[11])
    neu = _lot(id="brand", slug="Brand-1", stars=7000)
    stats2 = empty_funnel()
    fresh2, *_ = _detect(
        [neu, a],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[11],
        stats=stats2,
    )
    stats3 = empty_funnel()
    fresh3, *_ = _detect(
        [neu, a],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[11],
        stats=stats3,
    )
    assert [x.id for x in fresh2] == ["brand"]
    assert stats2["genuine_new"] == 1
    assert fresh3 == []
    assert stats3["genuine_new"] == 0


def test_two_scanner_workers_one_genuine_new() -> None:
    """Два worker одновременно увидели один новый listing → один GENUINE_NEW."""
    import threading

    lot_a = _lot(id="RACE", slug="Race-1", stars=8000, model_id=1)
    lot_b = _lot(id="RACE", slug="Race-1", stars=8000, model_id=2)
    observed: dict = {}
    primed = {model_request_key(1, [1]): 1.0, model_request_key(1, [2]): 1.0}
    pages: dict = {}
    seen: dict = {}
    round_hits: dict = {}
    barrier = threading.Barrier(2)
    results: list[list[str]] = []

    def worker(lot: Lot, models: list[int]) -> None:
        barrier.wait()
        fresh, _v = detect_fresh_lots(
            [lot],
            observed=observed,
            primed=primed,
            pages=pages,
            seen=seen,
            collection_id=1,
            model_ids=models,
            round_hits=round_hits,
        )
        results.append([x.id for x in fresh])

    t1 = threading.Thread(target=worker, args=(lot_a, [1]))
    t2 = threading.Thread(target=worker, args=(lot_b, [2]))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results.count(["RACE"]) == 1
    assert results.count([]) == 1
    q = _stub_queue()
    sent = []
    for chunk in results:
        if chunk:
            sent.extend(chunk)
            q.enqueue([_lot(id="RACE", slug="Race-1", stars=8000)])
    assert sent == ["RACE"]
    assert len(q._items) == 1


def test_business_filters_unchanged_in_pr56() -> None:
    """PR #56 не меняет female / price / floor / owner / post interval."""
    assert config.MIN_STARS == 5000
    assert config.MAX_STARS == 25000
    assert config.MIN_MODEL_FLOOR == 4000
    assert config.MAX_MODEL_FLOOR == 27000
    assert config.POST_INTERVAL == 4.0
    assert config.GIRL_MIN_SCORE == 5
    assert config.GIRL_REQUIRE_IDENTITY is True
    assert config.LISTING_PRICE_TOLERANCE == 0.0
    girl = _lot()
    assert is_girl(girl) is True
    assert filter_lot(girl, min_stars=5000, max_stars=25000) == ""
    boy = _lot(first_name="Никита", about="торгую гифтами", seller="nikita_gifts")
    assert filter_lot(boy, min_stars=5000, max_stars=25000) == "мужской"


def test_status_includes_scan_owner_female_metrics() -> None:
    from bot import ControlBot
    from tracker import Runtime

    ctrl = ControlBot.__new__(ControlBot)
    ctrl.authorized = True
    ctrl.account_name = "tester"
    ctrl.runtime = Runtime()
    rt = ctrl.runtime
    rt.snapshot_ready = True
    rt.funnel["fresh_detected"] = 1
    rt.funnel["fresh"] = 1
    rt.funnel["new_listing_seen"] = 4
    rt.funnel["old_listing_seen"] = 12
    rt.funnel["genuine_new"] = 2
    rt.funnel["genuine_new_listings"] = 2
    rt.funnel["unique_listing_ids"] = 9
    rt.funnel["listing_page_depth"] = 15
    rt.funnel["collections_scanned"] = 8
    rt.funnel["eligible_collections_scanned"] = 6
    rt.funnel["owner_sent_total"] = 1
    rt.funnel["owner_duplicate_total"] = 2
    rt.funnel["owner_dup_enqueue"] = 1
    rt.funnel["owner_dup_post_enrich"] = 1
    rt.funnel["owner_dup_send_guard"] = 0
    rt.funnel["owner_id_missing"] = 3
    d = rt.diag
    d.record_scan_discovery(5, new_n=4, old_n=12, pages=2, models=6, fresh_candidates=2)
    d.female_pass = 1
    d.female_reject = 2
    d.male_name_reject = 1
    text = ControlBot._status_text(ctrl)
    assert "new_listing_seen=4" in text
    assert "old_listing_seen=12" in text
    assert "GENUINE_NEW_LISTINGS=2" in text
    assert "unique_listing_ids=9" in text
    assert "listing_page_depth=" in text
    assert "collections_scanned=" in text
    assert "eligible_collections_scanned=" in text
    assert "new_candidates_per_collection=" in text
    assert "owner_sent_total=1" in text
    assert "owner_duplicate_total=2" in text
    assert "owner_dup_enqueue=1" in text
    assert "owner_dup_post_enrich=1" in text
    assert "owner_dup_send_guard=0" in text
    assert "owner_id_missing=" in text
    assert "female_pass=1" in text
    assert "male_name_reject=1" in text
    assert "score<" not in text
    assert "жду новые лоты с маркета" in text
    assert "eligible newest, unknown id = new" in text


def test_status_warmup_shows_progress() -> None:
    from bot import ControlBot
    from tracker import Runtime

    ctrl = ControlBot.__new__(ControlBot)
    ctrl.authorized = True
    ctrl.account_name = "tester"
    ctrl.runtime = Runtime()
    rt = ctrl.runtime
    rt.snapshot_ready = False
    rt.warmup_stage = "floors"
    rt.warmup_done = 24
    rt.warmup_total = 151
    text = ControlBot._status_text(ctrl)
    assert "Прогрев: каталог floor 24/151" in text
    assert "в канал не пощу" in text


def test_telegram_unauthorized_detected() -> None:
    from bot import is_telegram_unauthorized

    assert is_telegram_unauthorized(RuntimeError("Telegram server says - Unauthorized"))
    assert is_telegram_unauthorized(RuntimeError("Unauthorized"))
    assert is_telegram_unauthorized(RuntimeError("polling упал")) is False


def test_split_telegram_html_under_limit() -> None:
    from bot import split_telegram_html

    short = "ok"
    assert split_telegram_html(short, limit=100) == ["ok"]
    lines = [f"line-{i:04d} " + ("x" * 40) for i in range(200)]
    blob = "\n".join(lines)
    parts = split_telegram_html(blob, limit=3900)
    assert len(parts) >= 2
    assert all(len(p) <= 3900 for p in parts)
    assert "\n".join(parts) == blob
    huge = "a" * 5000
    parts = split_telegram_html(huge, limit=3900)
    assert all(len(p) <= 3900 for p in parts)
    assert "".join(parts) == huge


def test_status_chunks_fit_telegram_limit() -> None:
    """/status больше не падает с message is too long."""
    from bot import ControlBot, split_telegram_html
    from tracker import Runtime

    ctrl = ControlBot.__new__(ControlBot)
    ctrl.authorized = True
    ctrl.account_name = "tester"
    ctrl.runtime = Runtime()
    rt = ctrl.runtime
    rt.snapshot_ready = True
    rt.snapshot = 99999
    rt.passes = 80
    rt.collections = 160
    rt.posted = 12
    rt.funnel["fresh_detected"] = 500
    rt.funnel["fresh"] = 500
    for key in rt.funnel:
        if isinstance(rt.funnel[key], int) and rt.funnel[key] == 0:
            rt.funnel[key] = 3
    d = rt.diag
    d.girl_reject_score_lt_min = 0
    for i in range(20):
        d.record_freshness_verdict(
            {
                "listing_id": f"very-long-listing-id-{i}-" + ("z" * 40),
                "collection_id": 12_345_678,
                "model_id": 99_888_777,
                "listing_price": 12345,
                "first_seen_at": 1_700_000_000.123,
                "previous_seen_at": 1_700_000_111.456,
                "snapshot_contains_before": True,
                "seen_contains_before": False,
                "page_number": 2,
                "offset": "offset-token-" + ("n" * 80),
                "source_request": "scan:collection=1:models=1,2,3,4,5,6:page=2:offset=" + ("o" * 60),
                "reason": "UNPRIMED_SEED",
            }
        )
    text = ControlBot._status_text(ctrl)
    assert "FRESHNESS last20" not in text
    parts = split_telegram_html(text)
    assert parts
    assert all(len(p) <= 4096 for p in parts)
    assert "message is too long" not in text


def test_unknown_after_known_is_genuine_new() -> None:
    """Unknown id ниже известного на той же странице — GENUINE_NEW."""
    keep = _lot(id="keep", stars=8000)
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    _detect(
        [keep],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[11],
    )
    neu = _lot(id="brand", slug="Brand-1", stars=7000)
    floater = _lot(id="old_float", slug="Old-9", stars=6500)
    fresh, *_rest, stats = _detect(
        [neu, keep, floater],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[11],
        stats=empty_funnel(),
    )
    assert [x.id for x in fresh] == ["brand", "old_float"]
    assert stats["genuine_new"] == 2
    assert stats["old_after_anchor"] == 0
    assert stats["observed_old"] >= 1


def test_collection_prime_covers_other_model_key() -> None:
    """Смена model filter той же коллекции не UNPRIMED_SEED."""
    keep = _lot(id="keep", stars=8000)
    observed: dict = {}
    primed: dict = {}
    pages: dict = {}
    _detect(
        [keep],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[11],
    )
    assert collection_is_primed(primed, 9, "9:") is True
    neu = _lot(id="brand", slug="Brand-1", stars=7000)
    fresh, *_rest, stats = _detect(
        [neu, keep],
        observed=observed,
        primed=primed,
        pages=pages,
        collection_id=9,
        model_ids=[],
        stats=empty_funnel(),
    )
    assert [x.id for x in fresh] == ["brand"]
    assert stats["unprimed_seed"] == 0
    assert stats["genuine_new"] == 1


def test_collection_prime_does_not_match_other_gid() -> None:
    primed = {model_request_key(90, [1]): 1.0}
    assert collection_is_primed(primed, 9, "9:") is False
    assert collection_is_primed(primed, 90, "90:1") is True


def test_fetch_live_stops_at_first_known() -> None:
    import asyncio

    m = TelegramMarket.__new__(TelegramMarket)
    calls = {"n": 0}

    async def fp(*_a, offset="", **_k):
        calls["n"] += 1
        if not offset:
            m.last_next_offset = "p2"
            return [
                _lot(id="new1", stars=8000),
                _lot(id="old1", stars=8000),
                _lot(id="float1", stars=8000),
            ]
        m.last_next_offset = ""
        return [_lot(id="should_not_fetch", stars=8000)]

    m.fetch_page = fp
    m.last_next_offset = ""

    async def run():
        return await TelegramMarket.fetch_newest_until_known(
            m,
            1,
            model_ids=[22],
            known_ids={"old1"},
            seen={},
            max_pages=3,
            limit=12,
            stop_at_first_known=True,
        )

    lots, meta = asyncio.run(run())
    assert calls["n"] == 1
    assert meta["pages"] == 1
    assert meta["hit_known"] is True
    assert "should_not_fetch" not in {x.id for x in lots}
    assert [x.id for x in lots] == ["new1", "old1", "float1"]


def test_owners_do_not_repeat_in_queue() -> None:
    import time

    q = _stub_queue(seen_sellers={"id:1001": time.time()})
    a = _lot(id="A1", slug="Gift-1", seller="alice", seller_id=1001, stars=8000)
    b = _lot(id="B2", slug="Gift-2", seller="alice", seller_id=1001, stars=9000)
    assert q.enqueue([a, b]) == 0
    assert q._items == []


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
        test_scan_batch_default_is_all_collections,
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
        test_price_reject_below_and_above,
        test_seen_reject_listing_owner_other,
        test_male_ru_girl_dm_level_reject_reasons,
        test_nft_reject_reason_count_limit_and_log,
        test_status_rejection_reasons_section,
        test_rejection_diagnostics_do_not_change_filters,
        test_model_chunk_rotates_eligible_models,
        test_fetch_newest_stops_when_page_all_known,
        test_fetch_newest_depth_beyond_page_limit_can_be_new,
        test_merge_page_snapshot_keeps_more_than_top12,
        test_scan_discovery_metrics_show_where_new_listings_go,
        test_same_owner_simultaneous_workers_only_one_send,
        test_same_owner_restart_second_nft_blocked,
        test_female_gate_rejects_male_name_plus_female_emoji,
        test_female_gate_rejects_male_username_plus_female_bio,
        test_female_gate_rejects_danila_despite_a_ending,
        test_female_gate_rejects_male_plus_devushka_word,
        test_female_gate_rejects_gifts_emoji_photo_only,
        test_female_gate_rejects_empty_and_latin_nickname,
        test_female_gate_rejects_ambiguous_sasha,
        test_female_gate_api_gender_male_overrides_female_name,
        test_female_gate_keeps_confident_maria,
        test_status_includes_scan_owner_female_metrics,
        test_status_warmup_shows_progress,
        test_telegram_unauthorized_detected,
        test_split_telegram_html_under_limit,
        test_status_chunks_fit_telegram_limit,
        test_unknown_after_known_is_genuine_new,
        test_collection_prime_covers_other_model_key,
        test_collection_prime_does_not_match_other_gid,
        test_fetch_live_stops_at_first_known,
        test_owners_do_not_repeat_in_queue,
        test_new_listing_seen_is_not_page_absence,
        test_a_old_listing_after_restart,
        test_b_old_listing_on_next_page,
        test_c_same_listing_two_model_queries,
        test_d_same_listing_two_collections,
        test_e_new_listing_between_scan1_and_scan2,
        test_f_reorder_existing_no_false_positive,
        test_g_pagination_next_offset,
        test_h_snapshot_persistence,
        test_round_dedup_same_listing_two_requests,
        test_forensic_verdict_fields,
        test_1_old_listing_before_start_not_sent,
        test_8_unprimed_seed_never_enqueued,
        test_5_same_listing_page1_and_page2_one_genuine,
        test_7_sync_pages_second_query_keeps_ids,
        test_10_genuine_new_excludes_seen_and_observed,
        test_e_genuine_new_exactly_once,
        test_two_scanner_workers_one_genuine_new,
        test_business_filters_unchanged_in_pr56,
    ]
    for fn in tests:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nВсе {len(tests)} тестов прошли")


if __name__ == "__main__":
    main()
