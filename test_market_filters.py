"""Фильтр девочек, прототипы коллекций, наивные продавцы."""

from __future__ import annotations

from types import SimpleNamespace

from market import (
    GiftPrototype,
    Lot,
    MarketPriceBook,
    TelegramMarket,
    _parse,
    female_filter_reason,
    is_clean_female_profile,
    looks_female,
    looks_male,
    naivety_score,
)
from tracker import Config, format_lot
from tracker_filters import (
    DEFAULT_FILTER_DATA,
    FILTER_SCHEMA,
    migrate_legacy_filters,
)


def _lot(**kwargs) -> Lot:
    base = dict(
        id="1",
        title="Snoop Dogg",
        number=1,
        stars=8000.0,
        slug="SnoopDogg-1",
    )
    base.update(kwargs)
    return Lot(**base)


def test_female_names_pass() -> None:
    for name in (
        "Катя",
        "Катя 💕",
        "Татьяна",
        "Аня",
        "Ксюша",
        "Александра",
        "Даша",
        "Полина",
        "Екатерина",
        "Соня",
        "Лиза",
        "Оля",
        "Маша",
    ):
        lot = _lot(first_name=name, seller="user123")
        assert looks_female(lot), name
        assert not looks_male(lot), name
        assert is_clean_female_profile(lot), name


def test_male_names_rejected() -> None:
    for name in ("Иван", "Дима", "Никита", "Алексей", "Максим", "Антон"):
        lot = _lot(first_name=name, seller="user123")
        assert looks_male(lot), name
        assert not looks_female(lot), name
        assert female_filter_reason(lot) == "мужской", name


def test_unisex_not_auto_female() -> None:
    lot = _lot(first_name="Саша", seller="coolguy")
    assert not looks_female(lot)
    assert not looks_male(lot)
    assert female_filter_reason(lot) == "не девочка"


def test_username_female() -> None:
    lot = _lot(first_name="", seller="masha_love")
    assert looks_female(lot)


def test_username_misha_not_female() -> None:
    lot = _lot(first_name="", seller="misha228")
    assert not looks_female(lot)


def test_ad_profile_rejected() -> None:
    lot = _lot(first_name="Катя", about="дарю гифт пиши в лс @giftdouble")
    reason = female_filter_reason(lot)
    assert reason in {"реклама", "giftdouble"}


def test_prototype_fills_generic_title() -> None:
    class StarsAmount:
        def __init__(self, amount: float) -> None:
            self.amount = amount

    gift = SimpleNamespace(
        id=99,
        slug="",
        title="Gift",
        gift_id=42,
        num=7,
        resell_amount=[StarsAmount(8000)],
        attributes=[],
        owner_id=None,
        owner_name="",
    )
    proto = GiftPrototype(id=42, title="Snoop Dogg")
    lot = _parse(gift, prototype=proto, collection_id=42)
    assert lot is not None
    assert lot.title == "Snoop Dogg"
    assert lot.collection_id == 42
    assert lot.slug == "SnoopDogg-7"


def test_owner_name_fills_first_name() -> None:
    gift = SimpleNamespace(
        id=1,
        slug="LolPop-3",
        title="Lol Pop",
        gift_id=5,
        num=3,
        resell_stars=6000,
        attributes=[],
        owner_id=None,
        owner_name="Катя Иванова",
    )
    lot = _parse(gift)
    assert lot is not None
    assert lot.first_name == "Катя"
    assert lot.last_name == "Иванова"
    assert looks_female(lot)


def test_collection_attributes_fill_model() -> None:
    class StarGiftAttributeModel:
        def __init__(self) -> None:
            self.document = SimpleNamespace(id=555)
            self.name = ""

    class StarGiftAttributeBackdrop:
        def __init__(self) -> None:
            self.document = SimpleNamespace(id=777)
            self.name = "Black"

    gift = SimpleNamespace(
        id=2,
        slug="DeskCalendar-9",
        title="",
        gift_id=10,
        num=9,
        resell_stars=7000,
        attributes=[StarGiftAttributeModel(), StarGiftAttributeBackdrop()],
        owner_id=None,
        owner_name="",
    )
    proto = GiftPrototype(id=10, title="Desk Calendar", attr_names={555: "Long Beach"})
    lot = _parse(gift, prototype=proto, collection_id=10, attr_index={555: "Long Beach"})
    assert lot is not None
    assert lot.title == "Desk Calendar"
    assert lot.model == "Long Beach"
    assert lot.backdrop == "Black"


def test_parse_resale_uses_catalog_prototype() -> None:
    class _DummyClient:
        is_connected = True

        async def connect(self) -> None:
            return None

    m = TelegramMarket(_DummyClient())  # type: ignore[arg-type]
    m._prototypes[10] = GiftPrototype(id=10, title="Plush Pepe")

    class StarsAmount:
        def __init__(self, amount: float) -> None:
            self.amount = amount

    gift = SimpleNamespace(
        id=3,
        slug="PlushPepe-1",
        title="",
        gift_id=10,
        num=1,
        resell_amount=[StarsAmount(9000)],
        attributes=[],
        owner_id=None,
        owner_name="Полина",
    )
    result = SimpleNamespace(gifts=[gift], users=[], attributes=[])
    lots = m.parse_resale(result, 10)
    assert len(lots) == 1
    assert lots[0].title == "Plush Pepe"
    assert looks_female(lots[0])


def test_naive_girls_rank_higher() -> None:
    book = MarketPriceBook()
    book._samples["snoop dogg"] = [8000.0, 8200.0, 8500.0, 9000.0]
    naive = _lot(
        first_name="Катя",
        account_level=1,
        gifts_count=2,
        is_premium=False,
        free_dm=True,
        stars=7500.0,
        title="Snoop Dogg",
    )
    trader = _lot(
        first_name="Катя",
        account_level=8,
        gifts_count=40,
        is_premium=True,
        free_dm=True,
        stars=12000.0,
        title="Snoop Dogg",
    )
    assert naivety_score(naive, book) > naivety_score(trader, book)


def test_filters_migrate_female_on() -> None:
    migrated = migrate_legacy_filters(
        {
            "filter_schema": 4,
            "female_only": False,
            "min_stars": 5000,
            "max_stars": 25000,
            "max_gifts": 20,
        }
    )
    assert migrated["female_only"] is True
    assert migrated["filter_schema"] == FILTER_SCHEMA
    assert DEFAULT_FILTER_DATA["female_only"] is True


def test_format_lot_still_matches() -> None:
    cfg = Config(
        api_id=1,
        api_hash="x",
        session_string="",
        bot_token="",
        target_channel="@test",
    )
    lot = _lot(model="Long Beach", seller="stichpermskiy", seller_id=1)
    text = format_lot(lot, cfg, ts=1_777_000_000)
    assert "Snoop Dogg" in text
    assert "Long Beach" in text
    assert "🎨 Фон" not in text


if __name__ == "__main__":
    for fn, obj in list(globals().items()):
        if fn.startswith("test_") and callable(obj):
            obj()
            print("ok", fn)
    print("ALL OK")
