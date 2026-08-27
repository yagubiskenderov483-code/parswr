"""Большой словарь женских имён, уменьшительных и транслита."""

from __future__ import annotations

_BASE = """
авдотья аврора агата аглая агния агриппина ада адель адриана аида аксинья
алевтина александра алена алёна алина алиса алла альбина амелия анастасия
ангелина анита анжела анжелика анна антонина анфиса ариадна арина
ассоль астра ася беатриса белла берта богдана валентина валерия ванда
варвара василиса вера вероника викторина виктория виолетта виталина виталия
влада владислава галина гела гелена глафира дарина дарья даша диана дина
доминика ева евгения евдокия екатерина елена елизавета есения жанна
зинаида злата зоя инга инна иоанна ирина ия камилла камила капитолина
карина каролина катерина кира клавдия клара кристина ксения лада лариса
лаура лея лиана лидия лилия лина лия лолита любава любовь людмила майя
маргарита марианна марина мария марфа мила милана милена мирослава
надежда наталья наталия нелли ника нина нонна оксана олеся ольга
полина рада раиса регина рената римма роза роксана руслана сабина сара
светлана серафима снежана софия софья стелла стефания таисия тамара
татьяна ульяна фаина эвелина элина элла эльвира эльза эмилия эмма
юлиана юлия яна ярослава
""".split()

_DIMIN = """
анечка анжурка анюта аня ариша аська аленка алиночка алиска
валя варя верочка вика викуля викуся галка галочка даша дашенька дашуля
тома томуся томочка томаша
дианочка катя катюша катюха катька катенька ксюша ксюха лара леночка
лера лерочка лиза лизка лилечка люба любаша мариша маруся маша машка
машенька мила милаша милашка надя наташа наташка настя настенька
настюха настюша ника олеся оля олечка олька поля рита риточка
света светик светка соня сонечка таня танюша танька тася уля
юля юлька юлечка яна яночка ира ирочка ирка танюха веруня
""".split()

_LATIN = """
anya anyuta anna anechka angelina alina alisa alla alyona alena
arina asya bella daria darya dasha dashenka diana dina eva
ekaterina katya katyusha katusha katerina karina karolina kira
kate kathy katherine alexandra
ksenia ksusha lada larisa lera lerochka lena lenochka liza lilia
lina lolita lyuba lyubov margo margarita marina masha mashka
mashenka maria marusya mila milana milena nadya nastya nastena
nastyuha natalia natasha nina oksana olesya olya olga polina
polya rita sveta svetlana sonya sofia tamara tanya tanyusha
ulyana yulya yulia yulka yana vika viktoria victoria vera
veronica varvara vasilisa princess kitty baby angel milashka
zayka kisa nyasha
""".split()

_EXTRA_NICK = """
зайка зайчик киса киска няша няшка милашка солнышко лапочка крошка
принцесса малышка куколка зайчонок кошечка девочка девчонка девчуля
sweety cutie babe babygirl princessa queen kittycat angelbaby
""".split()


def _variants(name: str) -> set[str]:
    n = name.strip().lower().replace("ё", "е")
    if len(n) < 2:
        return set()
    out = {n}
    for suf in ("ка", "ша", "ха", "уха", "юша", "ечка", "очка", "енька", "уля"):
        out.add(n + suf)
    if n.endswith("я") and len(n) >= 3:
        stem = n[:-1]
        out.update({stem + "юха", stem + "енька", stem + "юша", stem + "ка"})
    if n.endswith("а") and len(n) >= 3:
        stem = n[:-1]
        out.update({stem + "ка", stem + "уша", stem + "уха", stem + "очка"})
    return {x for x in out if 2 <= len(x) <= 24}


def build_girl_names() -> frozenset[str]:
    names: set[str] = set()
    for raw in (*_BASE, *_DIMIN, *_LATIN, *_EXTRA_NICK):
        names.update(_variants(raw))
        names.add(raw.strip().lower().replace("ё", "е"))
    return frozenset(x for x in names if x)


GIRL_NAMES = build_girl_names()
