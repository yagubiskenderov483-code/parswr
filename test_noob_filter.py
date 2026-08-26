"""Юнит-тесты персон и микса (без Telegram)."""

from __future__ import annotations

import time

from market import Lot
from tracker import (
    Config,
    TrackerRuntime,
    _matches_male_empty,
    _queue_priority,
    filter_for_post,
    is_female_rich,
    passes_account_level,
    passes_persona_filter,
)


def _lot(**kw) -> Lot:
    base = dict(
        id="x-1",
        title="Gift",
        number=1,
        stars=1000.0,
        slug="g-1",
        seller="nubik",
        seller_id=42,
        free_dm=True,
        account_level=1,
        is_premium=False,
        gifts_count=2,
        lang_code="ru",
        first_name="Аня",
    )
    base.update(kw)
    return Lot(**base)


def test_passes_account_level_requires_known_in_noob_mode() -> None:
    lot = _lot(account_level=None)
    assert passes_account_level(lot, 2, require_known=False) is True
    assert passes_account_level(lot, 2, require_known=True) is False
    assert passes_account_level(_lot(account_level=3), 2) is False
    assert passes_account_level(_lot(account_level=-1), 2) is True


def test_passes_persona_filter() -> None:
    assert passes_persona_filter(_lot(), max_gifts=5) is None
    assert passes_persona_filter(_lot(is_premium=True), max_gifts=5) is None
    assert passes_persona_filter(_lot(gifts_count=20), max_gifts=5) == "pro"
    assert (
        passes_persona_filter(
            _lot(first_name="Ира", is_premium=True, gifts_count=12),
            max_gifts=5,
        )
        is None
    )
    male_empty = _lot(
        first_name="Вася",
        seller="biker99",
        about="мото cross",
        has_photo=False,
        is_premium=False,
        gifts_count=0,
        has_personal_channel=False,
    )
    assert passes_persona_filter(male_empty, max_gifts=5) is None
    assert (
        passes_persona_filter(
            _lot(
                first_name="Вася",
                is_premium=True,
                has_photo=False,
                about="",
            ),
            max_gifts=5,
        )
        == "premium"
    )
    assert (
        passes_persona_filter(
            _lot(
                first_name="Вася",
                is_premium=True,
                has_photo=True,
                about="коллекционер nft",
            ),
            max_gifts=5,
        )
        is None
    )


def test_matches_male_empty() -> None:
    assert _matches_male_empty(
        _lot(first_name="Петя", has_photo=False, about="", is_premium=False)
    )
    assert not _matches_male_empty(_lot(first_name="Аня", has_photo=True))


def test_female_rich_and_queue_priority() -> None:
    rich = _lot(
        seller="princess_xxx",
        has_photo=True,
        about="канал в био",
        has_personal_channel=True,
        gifts_count=3,
        is_premium=True,
    )
    assert is_female_rich(rich)
    cfg = Config(
        api_id=1,
        api_hash="x",
        session_string="",
        bot_token="",
        target_channel="",
        female_mix_target=0.30,
    )
    rt = TrackerRuntime(posted_total=20, posted_female_rich=2)
    assert _queue_priority(rich, cfg, rt) < _queue_priority(
        _lot(id="plain", seller="plain"), cfg, rt
    )


def test_filter_for_post_persona_mode() -> None:
    now = time.time()
    lots = [
        _lot(id="ok", seller="a"),
        _lot(id="prem", seller="b", is_premium=True),
        _lot(id="pro", seller="c", gifts_count=99, first_name="Игорь"),
        _lot(id="lvl", seller="d", account_level=5, first_name="Олег"),
    ]
    out, stats = filter_for_post(
        lots,
        {},
        now=now,
        strict_ru=False,
        noob_mode=True,
        max_gifts_count=5,
        max_account_level=2,
    )
    assert len(out) == 2
    assert {x.id for x in out} == {"ok", "prem"}
    assert stats["pro"] == 1
    assert stats["level"] == 1


if __name__ == "__main__":
    test_passes_account_level_requires_known_in_noob_mode()
    test_passes_persona_filter()
    test_matches_male_empty()
    test_female_rich_and_queue_priority()
    test_filter_for_post_persona_mode()
    print("all ok")
