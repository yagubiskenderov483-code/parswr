"""Только девушки по имени/нику. Пацанов (Дима/Саша/ава+канал) нет."""

from __future__ import annotations

from market import Lot
from tracker import (
    is_ordinary_girl_name,
    matches_girl_criteria,
    passes_persona_filter,
    unstylize_text,
)


def _lot(**kw) -> Lot:
    base = dict(
        id="x",
        title="G",
        number=1,
        stars=1000.0,
        slug="g",
        seller="user",
        seller_id=1,
        is_premium=False,
        account_level=0,
        gifts_count=0,
        has_photo=False,
        about="",
    )
    base.update(kw)
    return Lot(**base)


def test_ordinary_names() -> None:
    for name in ("Лера", "Катя", "Настюха", "Маша", "Даша", "Юля", "Настя", "Тома"):
        lot = _lot(first_name=name)
        assert is_ordinary_girl_name(lot), name
        assert matches_girl_criteria(lot), name
        assert passes_persona_filter(lot) is None, name


def test_girl_username() -> None:
    lot = _lot(first_name="", seller="katya_228")
    assert is_ordinary_girl_name(lot)
    assert passes_persona_filter(lot) is None


def test_cringe_girl() -> None:
    lot = _lot(first_name="", seller="зайка_princess")
    assert matches_girl_criteria(lot)
    assert passes_persona_filter(lot) is None


def test_male_skip() -> None:
    lot = _lot(first_name="Вася", seller="biker99", about="мото")
    assert not matches_girl_criteria(lot)
    assert passes_persona_filter(lot) == "persona"


def test_male_diminutives_not_girls() -> None:
    for name, nick in (
        ("Дима", "dima_nft"),
        ("Рома", "roma88"),
        ("Коля", "kolyan"),
        ("Вова", "vovan"),
        ("Саша", "sasha_bro"),
        ("Паша", "pashka"),
        ("Миша", "misha"),
        ("Ваня", "vanya"),
        ("Женя", "zhenya"),
        ("Лёша", "lyosha"),
        ("Юра", "yura"),
        ("Стёпа", "stepa"),
        ("Толя", "tolik"),
        ("Витя", "vitya"),
        ("Костя", "kostya"),
        ("Макс", "max_trade"),
        ("Никита", "nikita"),
        ("Кирилл", "kirill"),
        ("Алексей", "alexey"),
    ):
        lot = _lot(first_name=name, seller=nick)
        assert not matches_girl_criteria(lot), name
        assert passes_persona_filter(lot) == "persona", name


def test_profile_alone_not_girl() -> None:
    """Ава+канал+сторис без женского имени — это не девушка (часто пацан)."""
    lot = _lot(
        first_name="",
        seller="nft_king",
        has_photo=True,
        has_personal_channel=True,
        has_stories=True,
        gifts_count=4,
    )
    assert not matches_girl_criteria(lot)
    assert passes_persona_filter(lot) == "persona"


def test_generic_nicks_not_girl() -> None:
    for seller in ("xxx_crypto", "darkangel", "baby_ton", "queen_trade", "sweet_xxx"):
        lot = _lot(first_name="", seller=seller)
        assert not matches_girl_criteria(lot), seller
        assert passes_persona_filter(lot) == "persona", seller


def test_random_digits_not_girl() -> None:
    lot = _lot(first_name="Xy", seller="crypto_king99")
    assert not matches_girl_criteria(lot)
    assert passes_persona_filter(lot) == "persona"


def test_nikita_not_girl() -> None:
    lot = _lot(first_name="Никита", seller="nikita")
    assert not matches_girl_criteria(lot)


def test_girl_name_beats_male_nick() -> None:
    lot = _lot(first_name="Катя", seller="dima_nft")
    assert is_ordinary_girl_name(lot)
    assert passes_persona_filter(lot) is None


def test_username_checked_if_first_name_unknown() -> None:
    lot = _lot(first_name="Hi", seller="katya_228")
    assert is_ordinary_girl_name(lot)
    assert passes_persona_filter(lot) is None


def test_sparkles_do_not_make_a_boy_a_girl() -> None:
    lot = _lot(first_name="", seller="nft_king", about="✨ trading")
    assert not matches_girl_criteria(lot)


def test_kate_is_girl() -> None:
    lot = _lot(first_name="Kate", seller="x")
    assert matches_girl_criteria(lot)


def test_stylized_fonts() -> None:
    assert unstylize_text("𝒦𝒶𝓉𝓎𝒶") == "Katya"
    assert unstylize_text("𝕃𝕖𝕣𝕒") == "Lera"
    assert "Катя" in unstylize_text("К̸а̸т̸я̸")
    lot = _lot(first_name="𝒦𝒶𝓉𝓎𝒶", seller="qwe123")
    assert is_ordinary_girl_name(lot)
    assert passes_persona_filter(lot) is None
    lot2 = _lot(first_name="", seller="ᴋᴀᴛʏᴀ")
    assert is_ordinary_girl_name(lot2)
    lot3 = _lot(first_name="🅺🅰🆃🆈🅰", seller="x")
    assert is_ordinary_girl_name(lot3)


def test_stylized_male_still_blocked() -> None:
    lot = _lot(first_name="𝒟𝒾𝓂𝒶", seller="x")
    assert not matches_girl_criteria(lot)


def test_profile_bundle_without_girl_nick() -> None:
    """Ник не женский, но ава+канал+TGP+эмодзи — девушка."""
    lot = _lot(
        first_name="",
        seller="qwe12345",
        has_photo=True,
        has_personal_channel=True,
        has_stories=True,
        gifts_count=3,
        about="💅",
    )
    assert matches_girl_criteria(lot)
    assert passes_persona_filter(lot) is None


def test_surname_ova() -> None:
    lot = _lot(first_name="", last_name="Иванова", seller="user99")
    assert matches_girl_criteria(lot)


def test_profile_signals_with_girl_name() -> None:
    lot = _lot(
        first_name="Тома",
        has_photo=True,
        has_personal_channel=True,
        has_stories=True,
        gifts_count=2,
        about="🎀",
    )
    assert matches_girl_criteria(lot)
    assert passes_persona_filter(lot) is None


if __name__ == "__main__":
    test_ordinary_names()
    test_girl_username()
    test_cringe_girl()
    test_male_skip()
    test_male_diminutives_not_girls()
    test_profile_alone_not_girl()
    test_generic_nicks_not_girl()
    test_random_digits_not_girl()
    test_nikita_not_girl()
    test_girl_name_beats_male_nick()
    test_username_checked_if_first_name_unknown()
    test_sparkles_do_not_make_a_boy_a_girl()
    test_kate_is_girl()
    test_stylized_fonts()
    test_stylized_male_still_blocked()
    test_profile_bundle_without_girl_nick()
    test_surname_ova()
    test_profile_signals_with_girl_name()
    print("all ok")
