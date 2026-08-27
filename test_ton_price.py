"""Цена лота строго 5–15 TON — дорогие подарки в канал не идут."""

from __future__ import annotations

import os
import time

from market import Lot
from tracker import (
    DEFAULT_MAX_STARS,
    DEFAULT_MAX_TON,
    DEFAULT_MIN_STARS,
    DEFAULT_MIN_TON,
    Config,
    _collect_fresh_lot,
    in_cheap_ton_window,
    lot_ton,
    star_window_from_ton,
)


def _cfg(**kw) -> Config:
    base = dict(
        api_id=1,
        api_hash="x",
        session_string="",
        bot_token="",
        target_channel="",
        hot_limit=1,
    )
    base.update(kw)
    return Config(**base)


def _lot(lid: str, stars: float) -> Lot:
    return Lot(
        id=lid,
        title="G",
        number=1,
        stars=stars,
        slug=lid,
        seller="u",
        seller_id=1,
    )


def test_star_window_5_15_ton() -> None:
    lo, hi = star_window_from_ton(5, 15, 0.0102)
    assert lo == 490
    assert hi == 1471
    assert DEFAULT_MIN_STARS == 490
    assert DEFAULT_MAX_STARS == 1471
    assert DEFAULT_MIN_TON == 5
    assert DEFAULT_MAX_TON == 15


def test_window_accepts_cheap_rejects_expensive() -> None:
    cfg = _cfg()
    assert in_cheap_ton_window(490, cfg)  # ~5 TON
    assert in_cheap_ton_window(800, cfg)  # ~8 TON
    assert in_cheap_ton_window(1471, cfg)  # ~15 TON
    assert not in_cheap_ton_window(400, cfg)  # ~4 TON
    assert not in_cheap_ton_window(2000, cfg)  # ~20 TON
    assert not in_cheap_ton_window(5000, cfg)


def test_legacy_max_stars_2000_still_blocks_expensive() -> None:
    """Старый .env MAX_STARS=2000 не должен пускать лоты дороже 15 TON."""
    cfg = _cfg(min_stars=500, max_stars=2000)
    assert in_cheap_ton_window(1000, cfg)
    assert not in_cheap_ton_window(2000, cfg)
    assert lot_ton(2000) > 15


def _head_change(lot: Lot, gid: int = 9) -> Lot | None:
    seen: dict[str, float] = {}
    heads: dict[str, str] = {str(gid): "old-head"}
    cfg = _cfg()
    now = time.time()
    return _collect_fresh_lot(
        lot, 0, gid, seen, heads, cfg, baseline=False, now=now
    )


def test_collect_posts_10_ton() -> None:
    out = _head_change(_lot("cheap", 1000))  # 10.2 TON
    assert out is not None
    assert out.id == "cheap"


def test_collect_skips_20_ton() -> None:
    out = _head_change(_lot("rich", 2000))  # ~20 TON
    assert out is None


def test_collect_skips_50_ton() -> None:
    out = _head_change(_lot("whale", 5000))
    assert out is None


def test_collect_skips_below_5_ton() -> None:
    out = _head_change(_lot("too-cheap", 300))
    assert out is None


def test_from_env_clamps_max_stars_2000() -> None:
    old = {k: os.environ.get(k) for k in ("MIN_STARS", "MAX_STARS", "MIN_TON", "MAX_TON")}
    os.environ["MIN_STARS"] = "500"
    os.environ["MAX_STARS"] = "2000"
    os.environ.pop("MIN_TON", None)
    os.environ.pop("MAX_TON", None)
    try:
        cfg = Config.from_env()
        assert cfg.min_ton == 5
        assert cfg.max_ton == 15
        assert cfg.max_stars <= 1471
        assert cfg.min_stars >= 490
        assert not in_cheap_ton_window(2000, cfg)
        assert in_cheap_ton_window(800, cfg)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    test_star_window_5_15_ton()
    test_window_accepts_cheap_rejects_expensive()
    test_legacy_max_stars_2000_still_blocks_expensive()
    test_collect_posts_10_ton()
    test_collect_skips_20_ton()
    test_collect_skips_50_ton()
    test_collect_skips_below_5_ton()
    test_from_env_clamps_max_stars_2000()
    print("all ok")
