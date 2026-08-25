"""Unit tests for parser filters: rating, RU, free DM, uniqueness."""

from __future__ import annotations

from types import SimpleNamespace

from market import (
    Lot,
    _extract_level_gifts,
    _interpret_contact_req,
    _parse_stars_level,
)
from main import App, _dedupe_by_seller


def _lot(**kw) -> Lot:
    data = dict(
        id="1",
        title="Pepe",
        number=1,
        stars=100,
        slug="Pepe-1",
    )
    data.update(kw)
    return Lot(**data)


def test_rating_uses_level_not_stars_points() -> None:
    assert _parse_stars_level({"level": 2, "stars": 900}) == 2
    assert _parse_stars_level({"level": 0, "stars": 0}) == 0
    assert _parse_stars_level({"level": 8, "stars": 5000}) == 8
    # очки отрицательные, но бейдж 1 — это не «всем -1»
    assert _parse_stars_level({"level": 1, "stars": -15}) == 1


def test_rating_minus_only_when_level_and_points_negative() -> None:
    assert _parse_stars_level({"level": -1, "stars": -40}) == -1
    # дефолт TL -1 без отрицательных очков → 0
    assert _parse_stars_level({"level": -1, "stars": 0}) == 0
    assert _parse_stars_level({"level": -1}) == 0


def test_rating_none_when_missing() -> None:
    assert _parse_stars_level(None) is None
    assert _parse_stars_level({}) is None


def test_gifts_total_not_displayed() -> None:
    uf = SimpleNamespace(
        stars_rating={"level": 1, "stars": 10},
        stargifts_count=3,
        stargifts_displayed=8,
    )
    level, gifts = _extract_level_gifts(uf)
    assert level == 1
    assert gifts == 3


def test_gifts_zero_when_field_exists_but_empty() -> None:
    uf = SimpleNamespace(stars_rating=None, stargifts_count=None)
    level, gifts = _extract_level_gifts(uf)
    assert level == 0
    assert gifts == 0


def test_gifts_unknown_when_schema_has_no_field() -> None:
    uf = SimpleNamespace(other=1)
    level, gifts = _extract_level_gifts(uf)
    assert gifts is None


def test_paid_dm_not_free() -> None:
    class RequirementToContactPaidMessages(SimpleNamespace):
        pass

    paid = RequirementToContactPaidMessages(stars_amount=25)
    free, stars = _interpret_contact_req(paid)
    assert free is False
    assert stars == 25


def test_paid_dm_stars_field_alias() -> None:
    class RequirementToContactPaidMessages(SimpleNamespace):
        pass

    paid = RequirementToContactPaidMessages(stars=10)
    free, stars = _interpret_contact_req(paid)
    assert free is False
    assert stars == 10


def test_premium_dm_not_free() -> None:
    class RequirementToContactPremium:
        pass

    free, _paid = _interpret_contact_req(RequirementToContactPremium())
    assert free is False


def test_empty_req_is_free() -> None:
    class RequirementToContactEmpty:
        pass

    free, paid = _interpret_contact_req(RequirementToContactEmpty())
    assert free is True
    assert paid == 0


def test_owner_key_strips_at_and_case() -> None:
    a = _lot(seller="@Ivan", seller_id=7)
    b = _lot(seller="ivan", seller_id=7)
    assert a.owner_key == "ivan"
    assert b.owner_key == "ivan"
    assert "id:7" in a.seller_keys()
    assert set(a.seller_keys()) & set(b.seller_keys())


def test_dedupe_same_user_username_and_id() -> None:
    a = _lot(id="a", seller="ivan", seller_id=7)
    b = _lot(id="b", seller="", seller_id=7)
    out = _dedupe_by_seller([a, b])
    assert len(out) == 1


def test_russian_requires_cyrillic_name() -> None:
    assert App._is_russian(_lot(first_name="Алексей", lang_code="ru")) is True
    assert App._is_russian(_lot(first_name="Alex", lang_code="ru")) is False
    assert App._is_russian(_lot(first_name="John", lang_code="en")) is False
    assert App._is_russian(_lot(first_name="Іван", lang_code="ru")) is False
    assert App._is_russian(_lot(first_name="Саша", lang_code="uk")) is False
    assert App._is_russian(_lot(first_name="Саша", lang_code="")) is True


def test_bot_mention() -> None:
    app = App.__new__(App)
    assert app._mentions_bot(_lot(about="пиши в нашего бота")) is True
    assert app._mentions_bot(_lot(seller="giftbot")) is False
    assert app._mentions_bot(_lot(about="@nftshopbot drop")) is True
    assert app._mentions_bot(_lot(about="люблю гифты", first_name="Саша")) is False
    bot_lot = _lot(first_name="Саша")
    bot_lot.is_bot = True
    assert app._mentions_bot(bot_lot) is True


def test_rate_label() -> None:
    assert App._rate_label(0) == "0"
    assert App._rate_label(1) == "1"
    assert App._rate_label(2) == "2"
    assert App._rate_label(-1) == "-1"
    assert App._rate_label(-3) == "-1"


def test_parser_stats_need_live_profile() -> None:
    app = App.__new__(App)
    lot = _lot(first_name="Саша", account_level=1, gifts_count=2)
    lot.profile_checked = False
    assert app._parser_stats_ok(lot) is False
    lot.profile_checked = True
    assert app._parser_stats_ok(lot) is True
    lot.account_level = 80
    assert app._parser_stats_ok(lot) is False
    lot.account_level = 1
    lot.gifts_count = 40
    assert app._parser_stats_ok(lot) is False


def test_free_contact_strict() -> None:
    app = App.__new__(App)
    lot = _lot()
    lot.free_dm = None
    assert app._is_free_contact(lot) is False
    lot.free_dm = True
    lot.paid_dm_stars = 0
    assert app._is_free_contact(lot) is True
    lot.paid_dm_stars = 15
    assert app._is_free_contact(lot) is False
