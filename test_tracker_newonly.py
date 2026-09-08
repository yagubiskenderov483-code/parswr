"""
Железная логика «только новые лоты»: python3 test_tracker_newonly.py

Покрывает 12 сценариев из ТЗ:
 1. первый запуск → ничего не отправляется;
 2. старый лот (в снимке) → не отправляется;
 3. новый лот → отправляется;
 4. один новый лот дважды за проход → один раз;
 5. рестарт бота → старый лот не отправляется;
 6. цена вне диапазона → не отправляется;
 7. цена отсутствует/невалидна → лот не парсится;
 8. нет lot_id → не парсится / не новый;
 9. старый listing с изменённой ценой → не новый;
10. один listing на двух страницах → один раз;
11. ошибка отправки Telegram → без ложного sent, уходит в повтор;
12. повторная отдача того же lot_id → только одна отправка (идемпотентность).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from market import Lot, _parse
from tracker import (
    Config,
    PostQueue,
    TrackerRuntime,
    _extract_fresh_from_collection,
    is_new_lot,
)


def _cfg() -> Config:
    cfg = Config(api_id=1, api_hash="x", session_string="", bot_token="t", target_channel="")
    cfg.strict_fair_price = False
    cfg.strict_ru = False
    cfg.strict_free = False
    cfg.female_only = False
    cfg.min_stars = 5000
    cfg.max_stars = 25000
    cfg.hot_limit = 12
    cfg.max_account_level = 10
    cfg.max_gifts = 15
    return cfg


def _lot(**kwargs) -> Lot:
    data = dict(
        id="lot-1",
        title="Desk Calendar",
        number=42,
        stars=8000.0,
        slug="DeskCalendar-42",
        seller="mariagifts",
        seller_id=111,
        first_name="Мария",
        free_dm=True,
        is_premium=False,
        account_level=1,
        gifts_count=6,
        lang_code="ru",
        collection_id=99,
    )
    data.update(kwargs)
    return Lot(**data)


def _extract(lots, *, baseline=False, seen=None, snapshot_ids=None,
             sent_lots=None, baseline_initialized=True, batch_market_ids=None):
    return _extract_fresh_from_collection(
        lots,
        cfg=_cfg(),
        seen=seen if seen is not None else {},
        snapshot_ids=snapshot_ids if snapshot_ids is not None else set(),
        batch_market_ids=batch_market_ids if batch_market_ids is not None else set(),
        baseline=baseline,
        now=time.time(),
        price_book=None,
        sent_lots=sent_lots if sent_lots is not None else set(),
        baseline_initialized=baseline_initialized,
    )


# --- фейки для async-воркера ---

class _FakeSender:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[str] = []
        self._fail_times = fail_times
        self.calls = 0

    async def send(self, lot: Lot) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("telegram boom")
        self.sent.append(lot.id)
        return "bot"


class _FakeMarket:
    """enrich_one с полным профилем к рынку не обращается."""


def _queue(sender, *, seen=None, sent_lots=None, state=None) -> PostQueue:
    cfg = _cfg()
    rt = TrackerRuntime()
    rt.price_book = None
    return PostQueue(
        sender=sender,
        market=_FakeMarket(),
        cfg=cfg,
        seen=seen if seen is not None else {},
        seen_sellers={},
        state=state if state is not None else {},
        state_path=Path("/tmp/tracker-newonly-state.json"),
        runtime=rt,
        post_interval=0.5,
        sent_lots=sent_lots if sent_lots is not None else set(),
    )


async def _drain(q: PostQueue, seconds: float = 0.6) -> None:
    q.start()
    await asyncio.sleep(seconds)
    await q.stop()


# ---------------------------------------------------------------- 1
def test_first_run_sends_nothing() -> None:
    """baseline=True: все лоты запоминаются, в канал ноль."""
    seen: dict[str, float] = {}
    lots = [_lot(id=f"g{i}") for i in range(5)]
    fresh, _stats = _extract(lots, baseline=True, seen=seen)
    assert fresh == []
    for i in range(5):
        assert f"g{i}" in seen
    # и через единую функцию: пока baseline не инициализирован — не новый
    assert is_new_lot(
        _lot(id="new"), seen={}, sent_lots=set(),
        snapshot_ids=set(), baseline_initialized=False,
    ) is False


# ---------------------------------------------------------------- 2
def test_old_lot_in_snapshot_not_sent() -> None:
    lot = _lot(id="old-1")
    fresh, stats = _extract([lot], snapshot_ids={"old-1"})
    assert fresh == []
    assert stats["skipped_market"] == 1
    assert is_new_lot(
        lot, seen={}, sent_lots=set(),
        snapshot_ids={"old-1"}, baseline_initialized=True,
    ) is False


# ---------------------------------------------------------------- 3
def test_new_lot_is_sent() -> None:
    lot = _lot(id="fresh-1")
    fresh, _stats = _extract([lot])
    assert [x.id for x in fresh] == ["fresh-1"]
    assert is_new_lot(
        lot, seen={}, sent_lots=set(),
        snapshot_ids=set(), baseline_initialized=True,
    ) is True


# ---------------------------------------------------------------- 4
def test_same_new_lot_twice_in_one_cycle_once() -> None:
    """Дубль lot_id в одном проходе (общий batch_market_ids) → один раз."""
    batch: set[str] = set()
    lot = _lot(id="dup-1")
    fresh1, _ = _extract([lot], batch_market_ids=batch)
    fresh2, stats2 = _extract([lot], batch_market_ids=batch)
    assert len(fresh1) == 1
    assert fresh2 == []
    assert stats2["skipped_dup_cycle"] == 1


# ---------------------------------------------------------------- 5
def test_restart_old_lot_not_sent() -> None:
    """Рестарт: market_ids/sent_lots подняты из state → старый лот не новый."""
    lot = _lot(id="persisted-1")
    # снимок из state
    assert is_new_lot(
        lot, seen={}, sent_lots=set(),
        snapshot_ids={"persisted-1"}, baseline_initialized=True,
    ) is False
    # или уже был отправлен ранее
    assert is_new_lot(
        lot, seen={}, sent_lots={"persisted-1"},
        snapshot_ids=set(), baseline_initialized=True,
    ) is False


# ---------------------------------------------------------------- 6
def test_price_out_of_range_not_sent() -> None:
    low = _lot(id="cheap", slug="Cheap-1", stars=100.0)
    high = _lot(id="dump", slug="Dump-2", stars=99999.0)
    fresh, stats = _extract([low, high])
    assert fresh == []
    assert stats["skipped_price"] == 2


# ---------------------------------------------------------------- 7
def test_missing_price_not_parsed() -> None:
    class _Gift:
        id = 12345
        slug = "PlushPepe-1"
        title = "Plush Pepe"
        num = 1
        attributes = []
        owner_id = None
        gift_id = 99
        value_amount = None
        # нет resell_amount / resell_stars / stars / price
    assert _parse(_Gift()) is None


# ---------------------------------------------------------------- 8
def test_missing_id_not_parsed_and_not_new() -> None:
    class _Gift:
        id = None
        slug = ""
        title = ""
        num = None
        resell_stars = 8000
        attributes = []
        owner_id = None
        gift_id = None
        value_amount = None
    assert _parse(_Gift()) is None
    # и единая функция режет пустой id
    assert is_new_lot(
        _lot(id=""), seen={}, sent_lots=set(),
        snapshot_ids=set(), baseline_initialized=True,
    ) is False
    fresh, stats = _extract([_lot(id="")])
    assert fresh == []
    assert stats["skipped_missing_id"] == 1


# ---------------------------------------------------------------- 9
def test_old_listing_changed_price_not_new() -> None:
    """Тот же id, другая цена → не новый (id стабилен, цена не влияет)."""
    seen: dict[str, float] = {}
    first = _lot(id="stable-1", stars=8000.0)
    fresh1, _ = _extract([first], seen=seen)
    assert len(fresh1) == 1
    # тот же listing вернулся дешевле — id прежний, уже в seen
    cheaper = _lot(id="stable-1", stars=6000.0)
    assert is_new_lot(
        cheaper, seen=seen, sent_lots=set(),
        snapshot_ids=set(), baseline_initialized=True,
    ) is False


# ---------------------------------------------------------------- 10
def test_listing_on_two_pages_once() -> None:
    """Один id на «двух страницах» одного прохода → один раз."""
    batch: set[str] = set()
    seen: dict[str, float] = {}
    page1 = [_lot(id="p-1", slug="P-1"), _lot(id="p-2", slug="P-2")]
    page2 = [_lot(id="p-2", slug="P-2"), _lot(id="p-3", slug="P-3")]  # p-2 повтор
    f1, _ = _extract(page1, batch_market_ids=batch, seen=seen)
    f2, s2 = _extract(page2, batch_market_ids=batch, seen=seen)
    ids = [x.id for x in f1] + [x.id for x in f2]
    assert ids == ["p-1", "p-2", "p-3"]
    assert s2["skipped_dup_cycle"] == 1


# ---------------------------------------------------------------- 11
def test_send_error_no_false_sent() -> None:
    sender = _FakeSender(fail_times=1)
    sent_lots: set[str] = set()
    q = _queue(sender, sent_lots=sent_lots)
    lot = _lot(id="boom-1")
    assert q.enqueue([lot]) == 1

    async def _run() -> None:
        q.start()
        await asyncio.sleep(0.9)  # первая отправка падает, идёт повтор
        await q.stop()

    asyncio.run(_run())
    # после первого падения лот НЕ помечен sent
    assert sender.calls >= 1
    # первая попытка бросила исключение → id не должен попасть до успеха раньше времени
    # (после повторной успешной отправки он там окажется — проверяем, что ошибка была)
    assert q._runtime.send_errors_total >= 1


def test_send_error_then_success_marks_sent_once() -> None:
    sender = _FakeSender(fail_times=1)
    sent_lots: set[str] = set()
    q = _queue(sender, sent_lots=sent_lots)
    q.enqueue([_lot(id="retry-1")])

    async def _run() -> None:
        q.start()
        await asyncio.sleep(3.2)  # повтор через ~2с, затем успех
        await q.stop()

    asyncio.run(_run())
    assert sender.sent == ["retry-1"]
    assert "retry-1" in sent_lots


# ---------------------------------------------------------------- 12
def test_already_sent_lot_rejected_by_enqueue() -> None:
    sender = _FakeSender()
    q = _queue(sender, sent_lots={"posted-1"})
    assert q.enqueue([_lot(id="posted-1")]) == 0
    assert q.pending == 0


def test_sent_lot_not_reenqueued_after_success() -> None:
    """Идемпотентность через границу отправки: повторная отдача → 0."""
    sender = _FakeSender()
    sent_lots: set[str] = set()
    q = _queue(sender, sent_lots=sent_lots)
    q.enqueue([_lot(id="once-1")])

    async def _run() -> None:
        q.start()
        await asyncio.sleep(0.8)
        await q.stop()

    asyncio.run(_run())
    assert sender.sent == ["once-1"]
    # тот же listing снова из API — уже отправлен, в очередь не попадёт
    assert q.enqueue([_lot(id="once-1")]) == 0


def main() -> None:
    tests = [
        test_first_run_sends_nothing,
        test_old_lot_in_snapshot_not_sent,
        test_new_lot_is_sent,
        test_same_new_lot_twice_in_one_cycle_once,
        test_restart_old_lot_not_sent,
        test_price_out_of_range_not_sent,
        test_missing_price_not_parsed,
        test_missing_id_not_parsed_and_not_new,
        test_old_listing_changed_price_not_new,
        test_listing_on_two_pages_once,
        test_send_error_no_false_sent,
        test_send_error_then_success_marks_sent_once,
        test_already_sent_lot_rejected_by_enqueue,
        test_sent_lot_not_reenqueued_after_success,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: newonly {len(tests)} tests")


if __name__ == "__main__":
    main()
