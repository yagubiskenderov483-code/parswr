"""
Полный прогон фильтров как на Bothost: python3 test_tracker_pipeline.py
"""

from __future__ import annotations

import time
from pathlib import Path

from market import Lot, MarketPriceBook, is_russian_lot
from tracker import (
    Config,
    PostQueue,
    TrackerRuntime,
    _extract_fresh_from_collection,
    filter_for_post,
    profile_is_thin,
    skip_is_incomplete,
)


def _lot(**kwargs) -> Lot:
    data = dict(
        id="lot-1",
        title="Desk Calendar",
        number=42,
        stars=6200.0,
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
    """Мальчики и арабы режутся; девушки и нейтральные проходят."""
    book = MarketPriceBook()
    book.set_floor(["desk calendar", "cid:99"], 5500.0)
    lots: list[Lot] = []
    for i in range(5):
        lots.append(
            _lot(
                id=f"g{i}",
                seller=f"mariagifts{i}",
                seller_id=100 + i,
                first_name="Мария",
                lang_code="ru",
                stars=5600.0 + i,
            )
        )
    for i in range(3):
        lots.append(
            _lot(
                id=f"n{i}",
                seller=f"nfttrader{i}",
                seller_id=150 + i,
                first_name="",
                stars=5650.0 + i,
            )
        )
    for i in range(3):
        lots.append(
            _lot(
                id=f"boy{i}",
                seller=f"alexgifts{i}",
                seller_id=200 + i,
                first_name="Alex",
                stars=5700.0,
            )
        )
    lots.append(
        _lot(
            id="ahmed",
            seller="ahmedgifts",
            seller_id=250,
            first_name="Ahmed",
            stars=5800.0,
        )
    )
    lots.append(
        _lot(
            id="dump",
            stars=20000.0,
            title="Desk Calendar",
            collection_id=99,
            first_name="Мария",
            seller="dumpgirl",
            seller_id=3001,
            lang_code="ru",
        )
    )
    passed, stats = _filter_batch(lots, book)
    assert stats["overprice"] == 1
    assert stats["not_female"] == 3
    assert stats["non_ru"] == 1
    assert stats["unknown_ru"] == 3
    assert len(passed) == 5


def test_typical_post_ready_lot() -> None:
    lot = _lot(
        first_name="Мария",
        seller="mariagifts",
        stars=6200.0,
        lang_code="ru",
    )
    assert is_russian_lot(lot) is True
    passed, stats = _filter_batch([lot])
    assert len(passed) == 1
    assert sum(stats.values()) == 0


def test_latin_ru_unknown_skipped() -> None:
    lot = _lot(seller="cryptogifts", first_name="", lang_code="")
    assert is_russian_lot(lot) is None
    passed, stats = _filter_batch([lot])
    assert stats["unknown_ru"] == 1
    assert passed == []


def test_boy_blocked_girl_passes() -> None:
    boy, stats_b = _filter_batch(
        [_lot(first_name="Alex", seller="alexgifts", seller_id=1)]
    )
    girl, stats_g = _filter_batch(
        [_lot(first_name="Мария", seller="mariagifts", seller_id=2, lang_code="ru")]
    )
    assert stats_b["not_female"] == 1
    assert boy == []
    assert len(girl) == 1
    assert stats_g["not_female"] == 0


def test_various_prices_pass_filters() -> None:
    book = MarketPriceBook()
    book.set_floor(["desk calendar", "cid:99"], 5500.0)
    for stars in (5600.0, 6200.0):
        lot = _lot(
            stars=stars,
            seller=f"mariagifts{int(stars)}",
            seller_id=int(stars),
            first_name="Мария",
            lang_code="ru",
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


def test_overprice_extract_does_not_burn_seen() -> None:
    book = MarketPriceBook()
    book.set_floor(["desk calendar", "cid:99"], 3500.0)
    lot = _lot(id="new-dump", stars=12000.0, first_name="Мария", seller="mariagifts")
    cfg = Config(api_id=1, api_hash="x", session_string="", bot_token="t", target_channel="")
    cfg.strict_fair_price = True
    cfg.fair_price_ratio = 2.0
    cfg.min_stars = 5000
    cfg.max_stars = 25000
    cfg.hot_limit = 8
    seen: dict[str, float] = {}
    fresh, stats = _extract_fresh_from_collection(
        [lot],
        cfg=cfg,
        seen=seen,
        snapshot_ids=set(),
        batch_market_ids=set(),
        baseline=False,
        now=time.time(),
        price_book=book,
    )
    assert fresh == []
    assert stats["skipped_overprice"] == 1
    assert "new-dump" not in seen


def test_snapshot_id_blocks_old_lot() -> None:
    """Лот из снимка маркета — уже видели, в канал не повторяем."""
    lot = _lot(id="live-ok", stars=8000.0, first_name="Мария", seller="mariagifts")
    cfg = Config(api_id=1, api_hash="x", session_string="", bot_token="t", target_channel="")
    cfg.strict_fair_price = False
    cfg.min_stars = 5000
    cfg.max_stars = 25000
    cfg.hot_limit = 8
    seen: dict[str, float] = {}
    fresh, stats = _extract_fresh_from_collection(
        [lot],
        cfg=cfg,
        seen=seen,
        snapshot_ids={"live-ok"},
        batch_market_ids=set(),
        baseline=False,
        now=time.time(),
        price_book=None,
    )
    assert fresh == []
    assert stats["skipped_market"] == 1


def test_baseline_snapshot_burns_seen() -> None:
    lot = _lot(id="old-ok", stars=8000.0, first_name="Мария", seller="mariagifts")
    cfg = Config(api_id=1, api_hash="x", session_string="", bot_token="t", target_channel="")
    cfg.strict_fair_price = False
    cfg.min_stars = 5000
    cfg.max_stars = 25000
    cfg.hot_limit = 8
    seen: dict[str, float] = {}
    fresh, _stats = _extract_fresh_from_collection(
        [lot],
        cfg=cfg,
        seen=seen,
        snapshot_ids=set(),
        batch_market_ids=set(),
        baseline=True,
        now=time.time(),
        price_book=None,
    )
    assert fresh == []
    assert "old-ok" in seen


def test_fresh_lot_marked_seen() -> None:
    lot = _lot(id="new-ok", stars=8000.0, first_name="Мария", seller="mariagifts")
    cfg = Config(api_id=1, api_hash="x", session_string="", bot_token="t", target_channel="")
    cfg.strict_fair_price = False
    cfg.min_stars = 5000
    cfg.max_stars = 25000
    cfg.hot_limit = 4
    seen: dict[str, float] = {}
    fresh, _stats = _extract_fresh_from_collection(
        [lot],
        cfg=cfg,
        seen=seen,
        snapshot_ids=set(),
        batch_market_ids=set(),
        baseline=False,
        now=time.time(),
        price_book=None,
    )
    assert len(fresh) == 1
    assert "new-ok" in seen
    fresh2, stats2 = _extract_fresh_from_collection(
        [lot],
        cfg=cfg,
        seen=seen,
        snapshot_ids=set(),
        batch_market_ids=set(),
        baseline=False,
        now=time.time(),
        price_book=None,
    )
    assert fresh2 == []
    assert stats2["skipped_seen"] == 1


def test_enqueue_accepts_fresh_lot_already_marked_seen() -> None:
    """Баг /status: +1 новых, очередь 0 — extract писал seen, enqueue дропал."""
    lot = _lot(
        id="new-ok",
        stars=8000.0,
        first_name="Мария",
        seller="mariagifts",
        slug="DeskCalendar-99",
        lang_code="ru",
    )
    cfg = Config(api_id=1, api_hash="x", session_string="", bot_token="t", target_channel="")
    cfg.strict_fair_price = False
    cfg.min_stars = 5000
    cfg.max_stars = 25000
    cfg.hot_limit = 4
    seen: dict[str, float] = {}
    fresh, _stats = _extract_fresh_from_collection(
        [lot],
        cfg=cfg,
        seen=seen,
        snapshot_ids=set(),
        batch_market_ids=set(),
        baseline=False,
        now=time.time(),
        price_book=None,
    )
    assert len(fresh) == 1
    assert "new-ok" in seen
    rt = TrackerRuntime()
    q = PostQueue(
        sender=None,
        market=None,
        cfg=cfg,
        seen=seen,
        seen_sellers={},
        state={},
        state_path=Path("/tmp/tracker-test-state.json"),
        runtime=rt,
    )
    assert q.enqueue(fresh) == 1
    assert q.pending == 1
    assert q.enqueue(fresh) == 0


def test_enqueue_drops_only_already_posted_seller() -> None:
    cfg = Config(api_id=1, api_hash="x", session_string="", bot_token="t", target_channel="")
    now = time.time()
    seen_sellers = {"mariagifts": now, "id:1": now}
    other = _lot(
        id="next-gift",
        stars=8100.0,
        seller="mariagifts",
        seller_id=1,
        slug="HeartLocket-1",
        first_name="Мария",
        lang_code="ru",
    )
    q = PostQueue(
        sender=None,
        market=None,
        cfg=cfg,
        seen={},
        seen_sellers=seen_sellers,
        state={},
        state_path=Path("/tmp/tracker-test-state.json"),
        runtime=TrackerRuntime(),
    )
    assert q.enqueue([other]) == 0
    q2 = PostQueue(
        sender=None,
        market=None,
        cfg=cfg,
        seen={},
        seen_sellers={},
        state={},
        state_path=Path("/tmp/tracker-test-state.json"),
        runtime=TrackerRuntime(),
    )
    assert q2.enqueue([other]) == 1


def test_thin_profile_needs_ru() -> None:
    lot = _lot(first_name="", seller="nftgifts2024", lang_code="", about="")
    assert profile_is_thin(lot) is True
    passed, stats = _filter_batch([lot])
    assert stats["unknown_ru"] == 1
    assert passed == []
    assert skip_is_incomplete(lot, stats) is True


def test_live_ru_gifts15_keeps_maria_cuts_farm() -> None:
    """RU + gifts≤15 + рынок; девушка не обязательна."""
    girl = _lot(
        id="ok",
        first_name="Мария",
        seller="mariagifts",
        seller_id=501,
        lang_code="ru",
        gifts_count=12,
        stars=8000.0,
    )
    farm = _lot(
        id="farm",
        first_name="Олег",
        seller="olegfarm",
        seller_id=502,
        lang_code="ru",
        gifts_count=16,
        stars=8000.0,
    )
    boy_ru = _lot(
        id="boy",
        first_name="Иван",
        seller="ivangifts",
        seller_id=503,
        lang_code="ru",
        gifts_count=8,
        stars=8000.0,
    )
    passed, stats = filter_for_post(
        [girl, farm, boy_ru],
        {},
        now=time.time(),
        strict_ru=True,
        strict_free=False,
        max_account_level=10,
        max_gifts=15,
        female_only=False,
        strict_fair_price=False,
    )
    assert stats["many_gifts"] == 1
    ids = {x.id for x in passed}
    assert ids == {"ok", "boy"}


def test_known_boy_skip_is_complete() -> None:
    lot = _lot(first_name="Alex", seller="alexgifts", lang_code="")
    assert profile_is_thin(lot) is False
    _passed, stats = _filter_batch([lot])
    assert stats["not_female"] == 1
    assert skip_is_incomplete(lot, stats) is False


def test_account_level_11_blocked() -> None:
    lot = _lot(first_name="Мария", seller="mariagifts", lang_code="ru", account_level=11)
    passed, stats = filter_for_post(
        [lot],
        {},
        now=time.time(),
        strict_ru=True,
        strict_free=False,
        max_account_level=10,
        max_gifts=15,
        female_only=False,
        strict_fair_price=False,
    )
    assert passed == []
    assert stats["level"] == 1


def test_account_level_10_passes() -> None:
    lot = _lot(first_name="Мария", seller="mariagifts", lang_code="ru", account_level=10)
    passed, _stats = filter_for_post(
        [lot],
        {},
        now=time.time(),
        strict_ru=True,
        strict_free=False,
        max_account_level=10,
        max_gifts=15,
        female_only=False,
        strict_fair_price=False,
    )
    assert passed == [lot]


def main() -> None:
    tests = [
        test_scenario_23_like_bothost,
        test_typical_post_ready_lot,
        test_latin_ru_unknown_skipped,
        test_boy_blocked_girl_passes,
        test_various_prices_pass_filters,
        test_telegram_value_dump_blocked,
        test_overprice_extract_does_not_burn_seen,
        test_snapshot_id_blocks_old_lot,
        test_baseline_snapshot_burns_seen,
        test_fresh_lot_marked_seen,
        test_enqueue_accepts_fresh_lot_already_marked_seen,
        test_enqueue_drops_only_already_posted_seller,
        test_thin_profile_needs_ru,
        test_live_ru_gifts15_keeps_maria_cuts_farm,
        test_known_boy_skip_is_complete,
        test_account_level_11_blocked,
        test_account_level_10_passes,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: pipeline {len(tests)} tests")


if __name__ == "__main__":
    main()
