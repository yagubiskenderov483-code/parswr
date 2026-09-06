"""Фильтры выдачи: девочки, level ≤ 2, ≤ 12 NFT, бесплатные ЛС."""

from __future__ import annotations

import re
from typing import Any

from market import Lot

_CYR_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")

_FEMALE_EMOJI_RE = re.compile(
    r"[👩👧💅💄🎀💖💕💗🌸🌺🌷🌹🩷💞💝👗👙👠👜💍👑🦄🦋🧚‍♀️🧚‍♀️🩷🤍]"
)
_MALE_EMOJI_RE = re.compile(r"[👨👦🧔💪🚗⚽🏀🎮🚬]")

_FEMALE_HINT_RE = re.compile(
    r"(девоч|девуш|девушка|женск|girl|woman|she/her|lady|queen|princess|"
    r"miss\b|mrs\b|babe|waifu|kawaii|pink)",
    re.IGNORECASE,
)
_MALE_HINT_RE = re.compile(
    r"(парень|мужчин|мальчик|пацан|boy\b|man\b|he/him|брат|бро\b|bro\b|"
    r"мужик|дядя)",
    re.IGNORECASE,
)

_AMBIGUOUS = frozenset(
    {"саша", "женя", "валя", "слава", "паша", "оля", "катя", "даша", "маша"}
)

_MALE_NAMES_RE = re.compile(
    r"^(?:"
    r"никита|илья|саша|женя|ваня|петя|коля|вася|дима|миша|паша|фома|лука|"
    r"савва|валера|слава|вова|лёша|леша|гоша|костя|артём|артем|макс|рома|"
    r"юра|тима|лёва|лева|сеня|федя|митя|боря|гена|стёпа|степа|кирилл|"
    r"егор|игорь|олег|влад|данил|даниил|андрей|алексей|сергей|павел|"
    r"иван|денис|роман|виктор|стас|тимур|глеб|борис|антон|ярослав|матвей|"
    r"александр|максим|дмитрий|владимир|николай|михаил|константин|"
    r"stepan|ivan|nikita|alex|max|dmitry|daniil|artem|roman|sergey|"
    r"andrey|pavel|ilya|vlad|kirill|egor|igor|oleg|denis|anton|timur"
    r")$",
    re.IGNORECASE,
)

_MALE_TRANSLIT_RE = re.compile(
    r"^(?:"
    r"nikita|ilya|ilia|misha|dima|vanya|vania|kostya|kostia|petya|petia|"
    r"vasya|vasia|kolya|kolia|tolya|tolia|gosha|grisha|lyosha|lesha|"
    r"alyosha|seryozha|serezha|danila|danya|gena|styopa|stepa|borya|"
    r"fedya|fedia|mitya|mitia|senya|yura|jura|roma|tima|sanya|sania|"
    r"savva|luka|foma|seva|lyova|leva|zhora|vova|zhenya|"
    r"mustafa|musa|isa|ali|akhmed|ahmed"
    r")$",
    re.IGNORECASE,
)

_LATIN_FEMALE_RE = re.compile(
    r"^(?:"
    r"anna|anya|ania|maria|mariya|mariia|masha|elena|lena|olga|olya|olia|"
    r"ekaterina|katerina|katya|katia|kate|katrin|yulia|julia|yuliya|"
    r"dasha|daria|darya|nastya|nastia|anastasia|anastasiya|polina|alina|"
    r"arina|diana|vika|victoria|viktoria|viktoriya|kristina|christina|"
    r"karina|marina|irina|ira|sofia|sofya|sophia|sonya|sonia|alisa|alice|"
    r"liza|lisa|elizaveta|milana|mila|kira|vera|nadya|nadia|tanya|tania|"
    r"tatiana|tatyana|natasha|natalia|nataliya|natali|sveta|svetlana|"
    r"ksenia|kseniya|ksyusha|oksana|lera|valeria|valeriya|alena|alyona|"
    r"angelina|veronika|veronica|varvara|varya|ulyana|uliana|zlata|eva|"
    r"emma|rita|margarita|nina|galya|lyuba|luba|lyudmila|ludmila|luda|"
    r"zhanna|inna|yana|jana|regina|snezhana|kamilla|camilla|amina|aliya|"
    r"elvira|albina|dinara|madina|evgenia|evgeniya|olesya|olesia|lilya|"
    r"lilia|liliya|elina|eleonora|vasilisa|taisia|taisiya|stefania|"
    r"miroslava|yaroslava|vlada|vladislava|dasha|masha|katya"
    r")$",
    re.IGNORECASE,
)

_FEMALE_NAME_END_RE = re.compile(
    r"(ия|ья|ина|ела|ёна|юна|ита|лия|ея|овна|евна|ична)$"
)
_FEMALE_USER_RE = re.compile(
    r"(girl|woman|lady|queen|princess|devoch|devush|miss|mrs|"
    r"ann|anna|maria|masha|elena|olga|kate|julia|diana|vika|nastya|"
    r"polina|alina|sofia|sonya|vera|liza|tanya|sveta|ira|yana|"
    r"маша|даша|катя|юля|настя|полина|алина|вика|лена|света|"
    r"анна|мария|елена|ольга|софия|соня|вероника|татьяна)",
    re.IGNORECASE,
)

_FEMALE_GIFTS = frozenset(
    {
        "rose",
        "roses",
        "heart",
        "kiss",
        "perfume",
        "bouquet",
        "teddy",
        "flower",
        "ring",
        "love",
        "bow",
        "butterfly",
        "unicorn",
        "princess",
        "berry",
        "lollipop",
        "candy",
        "diamond",
        "crown",
        "роза",
        "сердце",
        "поцелуй",
        "букет",
        "мишка",
        "цветок",
        "кольцо",
        "бант",
        "бабочка",
        "корона",
        "духи",
    }
)

_MALE_SHORT_NICK_RE = re.compile(r"(иша|уша|ёша|еша)$")


def _norm(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]", "", (text or "").lower().lstrip("@"))


def _first_token(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    return re.split(r"[\s_|.,\-]+", raw, maxsplit=1)[0]


def _latin_only(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def is_cyrillic_female_name(name: str) -> bool:
    fn = (name or "").strip().lower()
    if not fn or not _CYR_RE.search(fn):
        return False
    if fn in _AMBIGUOUS:
        return True
    if _MALE_NAMES_RE.search(fn) or _MALE_SHORT_NICK_RE.search(fn):
        return False
    if _FEMALE_NAME_END_RE.search(fn):
        return True
    return len(fn) >= 3 and fn.endswith(("а", "я"))


def is_latin_female_name(name: str) -> bool:
    raw = (name or "").strip()
    if not raw or _CYR_RE.search(raw):
        return False
    fn = _latin_only(raw)
    if len(fn) < 3:
        return False
    if _MALE_TRANSLIT_RE.match(fn) or _MALE_NAMES_RE.match(fn):
        return False
    if _LATIN_FEMALE_RE.match(fn) or _FEMALE_NAME_END_RE.search(fn):
        return True
    return fn.endswith(("a", "ya", "iya"))


def _username_female(username: str) -> bool:
    u = _norm(username)
    if len(u) < 3 or _MALE_NAMES_RE.search(u):
        return False
    if _FEMALE_USER_RE.search(u) or _FEMALE_NAME_END_RE.search(u):
        return True
    return u.endswith(("ka", "ya", "na", "sha", "nya", "lia", "iya"))


def _blob(lot: Lot) -> str:
    return " ".join(
        x
        for x in (
            lot.first_name,
            lot.last_name,
            lot.about,
            lot.seller,
            lot.personal_channel,
            lot.stories_text,
            lot.gifts_text,
            lot.emoji_status,
        )
        if x
    )


def looks_male(lot: Lot) -> bool:
    fn = _first_token(lot.first_name)
    if fn and (is_cyrillic_female_name(fn) or is_latin_female_name(fn)):
        return False
    blob = _blob(lot)
    if _MALE_HINT_RE.search(blob) or _MALE_EMOJI_RE.search(blob):
        return True
    if fn:
        latin = _latin_only(fn)
        if latin and _MALE_TRANSLIT_RE.match(latin):
            return True
        if _MALE_NAMES_RE.search(fn) and fn not in _AMBIGUOUS:
            return True
        if len(fn) >= 3 and fn.endswith(("ич", "он", "ил", "ём", "ем", "ур", "им")):
            if not fn.endswith(("ия", "ья")):
                return True
    ln = (lot.last_name or "").strip().lower()
    if ln.endswith(("ович", "евич", "ич")):
        return True
    seller = _norm(lot.seller)
    if seller and _MALE_NAMES_RE.search(seller):
        return True
    for part in re.split(r"[_.\-]+", (lot.seller or "").lower().lstrip("@")):
        if _norm(part) and _MALE_NAMES_RE.match(_norm(part)):
            return True
    return False


def female_score(lot: Lot) -> int:
    """Сколько женских сигналов на профиле (ник, био, канал, подарки, сторис, эмодзи)."""
    score = 0
    fn = _first_token(lot.first_name)
    ln = (lot.last_name or "").strip().lower()
    if fn and (is_cyrillic_female_name(fn) or is_latin_female_name(fn)):
        score += 4
    if ln.endswith(("овна", "евна", "ична")):
        score += 3
    if _username_female(lot.seller):
        score += 2
    blob = _blob(lot)
    if _FEMALE_HINT_RE.search(blob):
        score += 3
    if _FEMALE_EMOJI_RE.search(blob) or _FEMALE_EMOJI_RE.search(lot.first_name or ""):
        score += 2
    if lot.emoji_status and _FEMALE_EMOJI_RE.search(lot.emoji_status):
        score += 1
    if lot.personal_channel and _FEMALE_HINT_RE.search(lot.personal_channel):
        score += 1
    gifts = (lot.gifts_text or "").lower()
    if gifts:
        words = set(re.findall(r"[a-zа-яё]{3,}", gifts))
        if words & _FEMALE_GIFTS:
            score += 2
    stories = (lot.stories_text or "").lower()
    if stories and (_FEMALE_HINT_RE.search(stories) or _FEMALE_EMOJI_RE.search(stories)):
        score += 2
    if lot.has_photo and score > 0:
        score += 1
    return score


def female_reason(lot: Lot) -> str:
    if looks_male(lot):
        return "мужской"
    if female_score(lot) <= 0:
        return "нет женских признаков"
    return ""


def is_girl(lot: Lot) -> bool:
    return not female_reason(lot)


def passes_level(lot: Lot, max_level: int = 2) -> bool:
    lvl = lot.account_level
    if lvl is None:
        return False
    if lvl < 0:
        return True
    return lvl <= max_level


def passes_nfts(lot: Lot, max_nfts: int = 12) -> bool:
    n = lot.gifts_count
    if n is None:
        return False
    return n <= max_nfts


def passes_free_dm(lot: Lot) -> bool:
    return lot.free_dm is True


def filter_lot(
    lot: Lot,
    *,
    min_stars: float,
    max_stars: float,
    max_level: int = 2,
    max_nfts: int = 12,
) -> str:
    """Пустая строка = проходит. Иначе причина отказа."""
    if not (min_stars <= float(lot.stars) <= max_stars):
        return "цена"
    if not lot.seller_key:
        return "нет продавца"
    if not passes_free_dm(lot):
        return "платные ЛС" if lot.free_dm is False else "ЛС неизвестно"
    if not passes_level(lot, max_level):
        return "level"
    if not passes_nfts(lot, max_nfts):
        return "много NFT"
    reason = female_reason(lot)
    if reason:
        return reason
    return ""


def skip_stats() -> dict[str, int]:
    return {
        "price": 0,
        "no_seller": 0,
        "paid": 0,
        "unknown_dm": 0,
        "level": 0,
        "nfts": 0,
        "not_girl": 0,
        "dup": 0,
    }


def classify_skip(reason: str, stats: dict[str, int]) -> None:
    mapping = {
        "цена": "price",
        "нет продавца": "no_seller",
        "платные ЛС": "paid",
        "ЛС неизвестно": "unknown_dm",
        "level": "level",
        "много NFT": "nfts",
        "дубль": "dup",
    }
    key = mapping.get(reason, "not_girl")
    stats[key] = stats.get(key, 0) + 1


def seller_keys(lot: Lot) -> set[str]:
    keys: set[str] = set()
    if lot.seller:
        u = lot.seller.lower().lstrip("@").strip()
        if u:
            keys.add(u)
            keys.add(f"u:{u}")
    if lot.seller_id is not None:
        keys.add(f"id:{int(lot.seller_id)}")
    return keys
