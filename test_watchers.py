"""Разбиение NFT по 40 вотчерам."""

from tracker import watcher_slice


def test_watcher_slice_covers_all() -> None:
    ids = list(range(149))
    watchers = 40
    seen: set[int] = set()
    for i in range(watchers):
        part = watcher_slice(ids, i, watchers)
        assert part
        seen.update(part)
        for gid in part:
            assert gid % watchers == i
    assert seen == set(ids)


def test_first_scan_no_stale_post() -> None:
    from tracker import Config, _collect_fresh_lot
    from market import Lot
    import time

    cfg = Config(
        api_id=1,
        api_hash="x",
        session_string="",
        bot_token="",
        target_channel="",
        hot_limit=1,
        min_stars=500,
        max_stars=2000,
    )
    lot = Lot(id="old", title="G", number=1, stars=1000.0, slug="old")
    heads: dict[str, str] = {}
    seen: dict[str, float] = {}
    now = time.time()
    assert (
        _collect_fresh_lot(lot, 0, 7, seen, heads, cfg, baseline=True, now=now)
        is None
    )
    assert heads["7"] == "old"


if __name__ == "__main__":
    test_watcher_slice_covers_all()
    test_first_scan_no_stale_post()
    print("all ok")
