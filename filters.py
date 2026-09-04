"""Фильтры выдачи: девочки, level ≤ 2, ≤ 12 NFT, бесплатные ЛС."""

from __future__ import annotations

import re
from typing import Any

import config
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
    r"мужик|дядя|батя|папа\b|отец|муж\b|сын\b|dude\b|guy\b)",
    re.IGNORECASE,
)

_AMBIGUOUS = frozenset(
    {"саша", "женя", "валя", "слава", "паша"}
)

_MALE_NAMES_RE = re.compile(
    r"^(?:"
    r"никита|илья|саша|женя|ваня|петя|коля|вася|дима|миша|паша|фома|лука|"
    r"савва|валера|слава|вова|лёша|леша|гоша|костя|артём|артем|макс|рома|"
    r"юра|тима|лёва|лева|сеня|федя|митя|боря|гена|стёпа|степа|кирилл|"
    r"егор|игорь|олег|влад|данил|даниил|андрей|алексей|сергей|павел|"
        r"иван|денис|роман|виктор|стас|тимур|глеб|борис|антон|ярослав|матвей|"
        r"александр|максим|дмитрий|владимир|николай|михаил|константин|"
        r"данила|никола|саня|жека|толян|гриша|серёжа|сережа|колян|вован|"
        r"димон|тоха|сашок|"
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
    r"mustafa|musa|isa|ali|akhmed|ahmed|reza|rezaa|amir|mehdi|mahdi|"
    r"mohammad|muhammad|hossein|hussein|hassan|hasan|saeed|said|"
    r"arman|nima|pouya|pooya|parsa|arash|kasra|kian|farhad|milad|"
    r"kamran|omid|javad|babak|dariush|kaveh|siavash|peyman|"
    r"ahmad|hamid|majid|vahid|navid|behzad|hooman|keyvan"
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
        return False
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
    if _LATIN_FEMALE_RE.match(fn):
        return True
    return False


def _username_female(username: str) -> bool:
    u = _norm(username)
    if len(u) < 3 or _MALE_NAMES_RE.search(u):
        return False
    return bool(_FEMALE_USER_RE.search(u))


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


def _api_gender_male(lot: Lot) -> bool:
    g = str(getattr(lot, "api_gender", "") or "").strip().lower()
    return g in {"male", "m", "man", "1"}


def looks_male(lot: Lot) -> bool:
    """Anti-male gate. Женское имя НЕ освобождает от мужского username/bio/API."""
    if _api_gender_male(lot):
        return True
    fn = _first_token(lot.first_name)
    blob = _blob(lot)
    if _MALE_HINT_RE.search(blob) or _MALE_EMOJI_RE.search(blob):
        return True
    if fn:
        latin = _latin_only(fn)
        if latin and _MALE_TRANSLIT_RE.match(latin):
            return True
        if _MALE_NAMES_RE.search(fn):
            return True
        if _MALE_SHORT_NICK_RE.search(fn):
            return True
        if _CYR_RE.search(fn) and not is_cyrillic_female_name(fn):
            return True
        if len(fn) >= 3 and fn.endswith(("ич", "он", "ил", "ём", "ем", "ур", "им")):
            if not fn.endswith(("ия", "ья")):
                return True
    ln = (lot.last_name or "").strip().lower()
    if ln.endswith(("ович", "евич", "ич")) and not ln.endswith(("овна", "евна", "ична")):
        return True
    seller = _norm(lot.seller)
    if seller and (_MALE_NAMES_RE.search(seller) or _MALE_TRANSLIT_RE.search(seller)):
        return True
    for part in re.split(r"[_.\-]+", (lot.seller or "").lower().lstrip("@")):
        n = _norm(part)
        if n and (_MALE_NAMES_RE.match(n) or _MALE_TRANSLIT_RE.match(n)):
            return True
    return False


def male_reject_reason(lot: Lot) -> str:
    """Код male-reject без PII. Пусто если не male."""
    if not looks_male(lot):
        return ""
    if _api_gender_male(lot):
        return "male_explicit"
    blob = _blob(lot)
    if _MALE_HINT_RE.search(blob):
        return "male_bio"
    if _MALE_EMOJI_RE.search(blob):
        return "male_explicit"
    fn = _first_token(lot.first_name)
    if fn:
        latin = _latin_only(fn)
        if latin and _MALE_TRANSLIT_RE.match(latin):
            return "male_name"
        if _MALE_NAMES_RE.search(fn) or _MALE_SHORT_NICK_RE.search(fn):
            return "male_name"
        if _CYR_RE.search(fn) and not is_cyrillic_female_name(fn):
            return "male_name"
    seller = _norm(lot.seller)
    if seller and (_MALE_NAMES_RE.search(seller) or _MALE_TRANSLIT_RE.search(seller)):
        return "male_username"
    for part in re.split(r"[_.\-]+", (lot.seller or "").lower().lstrip("@")):
        n = _norm(part)
        if n and (_MALE_NAMES_RE.match(n) or _MALE_TRANSLIT_RE.match(n)):
            return "male_username"
    return "male_explicit"


def _username_female_name(username: str) -> bool:
    """Токен ника — настоящее женское имя (masha_nft). Не био-хинты."""
    raw = (username or "").lower().lstrip("@")
    for part in re.split(r"[_.\-]+", raw):
        tok = _first_token(part)
        if not tok:
            continue
        if is_cyrillic_female_name(tok) or is_latin_female_name(tok):
            return True
    return False


def is_ambiguous_gender(lot: Lot) -> bool:
    """Саша/Женя без женского отчества/female-name в нике — не девушка."""
    fn = _first_token(lot.first_name)
    if not fn or fn not in _AMBIGUOUS:
        return False
    ln = (lot.last_name or "").strip().lower()
    if ln.endswith(("овна", "евна", "ична")):
        return False
    if _username_female_name(lot.seller):
        return False
    return True


def is_empty_profile(lot: Lot) -> bool:
    if (lot.first_name or "").strip():
        return False
    if (lot.last_name or "").strip():
        return False
    if (lot.about or "").strip():
        return False
    if _username_female_name(lot.seller):
        return False
    return True


def has_female_identity(lot: Lot) -> bool:
    """Сильный якорь: женское имя / отчество / женское имя в токене username.

    girl/queen в нике, эмодзи, подарки, аватар — не якорь.
    """
    fn = _first_token(lot.first_name)
    if is_cyrillic_female_name(fn) or is_latin_female_name(fn):
        return True
    ln = (lot.last_name or "").strip().lower()
    if ln.endswith(("овна", "евна", "ична")):
        return True
    if _username_female_name(lot.seller):
        return True
    return False


def female_confident(lot: Lot) -> bool:
    """Conservative gate: лучше пропустить сомнительную, чем отправить мужчину."""
    if looks_male(lot):
        return False
    if is_ambiguous_gender(lot):
        return False
    if is_empty_profile(lot):
        return False
    if not has_female_identity(lot):
        return False
    return True


def girl_reject_reason(lot: Lot) -> str:
    if looks_male(lot):
        return "male"
    if is_ambiguous_gender(lot):
        return "ambiguous"
    if is_empty_profile(lot) or not has_female_identity(lot):
        return "no_identity"
    if female_confident(lot):
        return "ok"
    return "no_identity"


def female_reason(lot: Lot) -> str:
    """Сначала anti-male, потом якорь. Эмодзи/подарки/фото не делают девушкой."""
    if looks_male(lot):
        return "мужской"
    if is_ambiguous_gender(lot):
        return "нет женских признаков"
    if not female_confident(lot):
        return "нет женских признаков"
    return ""


def female_score(lot: Lot) -> int:
    """Текстовые сигналы для логов. Не используется как единственный pass."""
    score = 0
    fn = _first_token(lot.first_name)
    ln = (lot.last_name or "").strip().lower()
    if fn and (is_cyrillic_female_name(fn) or is_latin_female_name(fn)):
        score += 5
    if ln.endswith(("овна", "евна", "ична")):
        score += 4
    if _username_female_name(lot.seller):
        score += 5
    elif _username_female(lot.seller):
        score += 3
    blob = _blob(lot)
    if _FEMALE_HINT_RE.search(blob):
        score += 3
    return score


def is_girl(lot: Lot) -> bool:
    return not female_reason(lot)


_FOREIGN_LANG = frozenset(
    {
        "ar",
        "fa",
        "tr",
        "hi",
        "th",
        "vi",
        "id",
        "zh",
        "ja",
        "ko",
        "ms",
        "tl",
        "ur",
        "bn",
        "he",
        "am",
    }
)


def _name_bio(lot: Lot) -> str:
    return " ".join(
        x for x in (lot.first_name, lot.last_name, lot.about) if x
    ).strip()


def russian_why(lot: Lot) -> str:
    """Почему is_russian дал True / False / None — для DEBUG, не для фильтра."""
    name_bio = _name_bio(lot)
    lang = (lot.lang_code or "").strip().lower()
    fn = _first_token(lot.first_name)
    if lang in _FOREIGN_LANG:
        return f"FAIL lang={lang!r} in FOREIGN (name/bio ignored)"
    if _CYR_RE.search(name_bio):
        return "OK cyrillic in first/last/bio"
    if _CYR_RE.search(lot.seller or ""):
        return "OK cyrillic in username"
    if is_latin_female_name(fn):
        return f"OK latin female first_name={fn!r}"
    if _username_female_name(lot.seller):
        return f"OK latin female username={lot.seller!r}"
    if not name_bio and not lang:
        return "NONE empty first/last/bio + no lang_code (profile not loaded)"
    return (
        f"FAIL no cyrillic, first={fn!r} not latin-female, "
        f"user={lot.seller!r} lang={lang!r} name_bio={name_bio[:60]!r}"
    )


def is_russian(lot: Lot) -> bool | None:
    """Кириллица в имени/био/username, либо латинское женское имя (СНГ).

    lang_code пустой ≠ not_ru. Telegram почти никогда не отдаёт lang_code
    чужих юзеров. not_ru только если: явный fa/ar/… ИЛИ есть имя/био
    без кириллицы и без латинского женского имени.
    Пустой профиль (ещё не enrich) → None, не режем.
    """
    name_bio = _name_bio(lot)
    lang = (lot.lang_code or "").strip().lower()
    if lang in _FOREIGN_LANG:
        return False
    if _CYR_RE.search(name_bio) or _CYR_RE.search(lot.seller or ""):
        return True
    fn = _first_token(lot.first_name)
    if is_latin_female_name(fn):
        return True
    if _username_female_name(lot.seller):
        return True
    if not name_bio and not lang:
        return None
    return False


def explain_filters(
    lot: Lot,
    *,
    min_stars: float,
    max_stars: float,
    max_level: int = 2,
    max_nfts: int = 6,
) -> dict[str, Any]:
    """Срез всех стадий фильтра — для DEBUG-лога."""
    price_ok = min_stars <= float(lot.stars) <= max_stars
    male = looks_male(lot)
    ru = is_russian(lot)
    girl = female_reason(lot)
    dm = passes_free_dm(lot)
    lvl = passes_level(lot, max_level)
    nfts = passes_nfts(lot, max_nfts)
    return {
        "price": price_ok,
        "male": male,
        "ru": ru,
        "ru_why": russian_why(lot),
        "girl": girl or "ok",
        "dm": dm,
        "level": lvl,
        "nfts": nfts,
    }


def passes_level(lot: Lot, max_level: int = 2) -> bool | None:
    """True/False если знаем; None — ещё нет данных (не режем навсегда)."""
    lvl = lot.account_level
    if lvl is None:
        return None
    if lvl < 0:
        return True
    return lvl <= max_level


def passes_nfts(lot: Lot, max_nfts: int = 6) -> bool | None:
    n = lot.gifts_count
    if n is None:
        return None
    return n <= max_nfts


def passes_free_dm(lot: Lot) -> bool | None:
    if lot.free_dm is None:
        return None
    return lot.free_dm is True


def filter_lot(
    lot: Lot,
    *,
    min_stars: float,
    max_stars: float,
    max_level: int = 2,
    max_nfts: int = 6,
) -> str:
    """Пустая строка = проходит. Иначе причина отказа.

    Неизвестные level / NFT / ЛС не режем: Telegram часто не отдаёт
    stars_rating, и лот с lvl=None нельзя сжигать как «level».
    Floor модели проверяется только если scanner его проставил
    (model_id / model_floor). Тесты без этих полей не меняются.
    """
    if not (min_stars <= float(lot.stars) <= max_stars):
        return "цена"
    mid = getattr(lot, "model_id", None)
    floor = getattr(lot, "model_floor", None)
    if mid is not None or floor is not None:
        from floors import listing_and_floor_reason

        floor_reason = listing_and_floor_reason(
            listing_stars=float(lot.stars), floor=floor
        )
        if floor_reason and floor_reason != "цена":
            return floor_reason
    if looks_male(lot):
        return "мужской"
    ru = is_russian(lot)
    if ru is False:
        return "не русский"
    if ru is None:
        return "нет данных"
    reason = female_reason(lot)
    if reason:
        return reason
    dm = passes_free_dm(lot)
    if dm is False:
        return "платные ЛС"
    lvl = passes_level(lot, max_level)
    if lvl is False:
        return "level"
    nfts = passes_nfts(lot, max_nfts)
    if nfts is False:
        return "много NFT"
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
        "not_ru": 0,
        "dup": 0,
        "dup_seller": 0,
        "dup_listing": 0,
        "incomplete": 0,
        "bad_model": 0,
        "floor_unknown": 0,
        "floor_high": 0,
    }


def classify_skip(reason: str, stats: dict[str, int]) -> None:
    mapping = {
        "цена": "price",
        "нет продавца": "no_seller",
        "платные ЛС": "paid",
        "ЛС неизвестно": "incomplete",
        "нет данных": "incomplete",
        "level": "level",
        "много NFT": "nfts",
        "не русский": "not_ru",
        "дубль": "dup_seller",
        "дубль продавца": "dup_seller",
        "дубль лота": "dup_listing",
        "REJECT_BAD_MODEL_VALUE": "bad_model",
        "floor неизвестен": "floor_unknown",
        "floor выше макс": "floor_high",
    }
    key = mapping.get(reason, "not_girl")
    stats[key] = stats.get(key, 0) + 1
    if key in {"dup_seller", "dup_listing"}:
        stats["dup"] = stats.get("dup", 0) + 1


def canonical_owner_key(lot: Lot) -> str | None:
    """Стабильный ключ владельца: Telegram user ID. Username сюда не входит."""
    if lot.seller_id is None:
        return None
    try:
        sid = int(lot.seller_id)
    except (TypeError, ValueError):
        return None
    return f"id:{sid}"


def owner_alias_keys(lot: Lot) -> set[str]:
    """Username только как alias. Пустой / UNKNOWN ключа не даёт.

    Голый username — lookup старых seen_sellers; новые записи пишут u:.
    """
    keys: set[str] = set()
    if not lot.seller:
        return keys
    u = lot.seller.lower().lstrip("@").strip()
    if not u:
        return keys
    keys.add(f"u:{u}")
    keys.add(u)
    return keys


def seller_keys(lot: Lot) -> set[str]:
    keys = owner_alias_keys(lot)
    canon = canonical_owner_key(lot)
    if canon:
        keys.add(canon)
    return keys


def owner_is_blocked(lot: Lot, blocked: set[str]) -> bool:
    """Пустые ключи (UNKNOWN без id и username) не схлопываются."""
    keys = seller_keys(lot)
    if not keys:
        return False
    return bool(keys & blocked)
