"""Числа словами для голоса: «1 228 000» -> «один миллион двести двадцать восемь тысяч», «2,5» -> «две целых пять
десятых», «в 2026 году» -> «в две тысячи двадцать шестом году», «от 10 до 12» -> «от десяти до двенадцати».

Телефоны, версии (iOS 17.2), даты 27.10.2026, время 12:30 и дроби 1/2 не трогаем — их читает сам say.
"""
import re

UNITS = {"m": ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"],
         "f": ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]}
UNITS_GEN = {"m": ["", "одного", "двух", "трёх", "четырёх", "пяти", "шести", "семи", "восьми", "девяти"],
             "f": ["", "одной", "двух", "трёх", "четырёх", "пяти", "шести", "семи", "восьми", "девяти"]}
TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
         "семнадцать", "восемнадцать", "девятнадцать"]
TEENS_GEN = [w[:-1] + "и" for w in TEENS]
TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
TENS_GEN = ["", "", "двадцати", "тридцати", "сорока", "пятидесяти", "шестидесяти", "семидесяти", "восьмидесяти",
            "девяноста"]
HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
HUNDREDS_GEN = ["", "ста", "двухсот", "трёхсот", "четырёхсот", "пятисот", "шестисот", "семисот", "восьмисот",
                "девятисот"]
# (род, именительный 1 / 2–4 / 5+, родительный 1 / остальные)
SCALES = [(10 ** 9, "m", ("миллиард", "миллиарда", "миллиардов"), ("миллиарда", "миллиардов")),
          (10 ** 6, "m", ("миллион", "миллиона", "миллионов"), ("миллиона", "миллионов")),
          (10 ** 3, "f", ("тысяча", "тысячи", "тысяч"), ("тысячи", "тысяч"))]

# Порядковые: (основа, окончание им. п. м. р.) — родительный «-ого», предложный «-ом», средний род «-ое»
ORD_UNITS = ["", ("перв", "ый"), ("втор", "ой"), ("трет", "ий"), ("четвёрт", "ый"), ("пят", "ый"), ("шест", "ой"),
             ("седьм", "ой"), ("восьм", "ой"), ("девят", "ый")]
ORD_TEENS = [(w[:-1], "ый") for w in TEENS]
ORD_TENS = ["", "", ("двадцат", "ый"), ("тридцат", "ый"), ("сороков", "ой"), ("пятидесят", "ый"),
            ("шестидесят", "ый"), ("семидесят", "ый"), ("восьмидесят", "ый"), ("девяност", "ый")]
ORD_HUNDREDS = ["", ("сот", "ый")] + [(g + "сот", "ый") for g in ("двух", "трёх", "четырёх", "пяти", "шести",
                                                                   "семи", "восьми", "девяти")]

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")
FEMININE_NOUNS = re.compile(r"(минут|секунд|недел|страниц|ссылк|копе|гривн|тонн|штук|рупи|лир|крон|марк|"
                            r"тысяч|строк|задач|запис|песн|книг|статья|стать|новост|верси|модел)", re.IGNORECASE)


def _plural(n, forms):
    if 11 <= n % 100 <= 14:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _below_thousand(n, gender, genitive):
    words = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words.append((HUNDREDS_GEN if genitive else HUNDREDS)[hundreds])
    if 10 <= rest <= 19:
        words.append((TEENS_GEN if genitive else TEENS)[rest - 10])
    else:
        tens, units = divmod(rest, 10)
        if tens:
            words.append((TENS_GEN if genitive else TENS)[tens])
        if units:
            words.append((UNITS_GEN if genitive else UNITS)[gender][units])
    return words


def cardinal(n, gender="m", genitive=False):
    """Целое число словами (до сотен миллиардов). genitive — после «от/до»."""
    if n == 0:
        return "нуля" if genitive else "ноль"
    words = []
    for scale, scale_gender, nominative, gen_forms in SCALES:
        count, n = divmod(n, scale)
        if count:
            words += _below_thousand(count, scale_gender, genitive)
            if genitive:
                words.append(gen_forms[0] if count % 10 == 1 and count % 100 != 11 else gen_forms[1])
            else:
                words.append(_plural(count, nominative))
    words += _below_thousand(n, gender, genitive)
    return " ".join(words)


def _ord_word(stem_ending, form):
    stem, ending = stem_ending
    if stem == "трет":
        return stem + {"nom": "ий", "neut": "ье", "prep": "ьем", "gen": "ьего"}[form]
    return stem + {"nom": ending, "neut": "ое", "prep": "ом", "gen": "ого"}[form]


def ordinal(n, form="nom"):
    """Порядковое: 2026 -> «две тысячи двадцать шестой» (form: nom, neut, prep, gen); только 1–9999."""
    thousands, rest = divmod(n, 1000)
    if rest == 0 and thousands:
        prefix = "" if thousands == 1 else UNITS_GEN["m"][thousands]
        return _ord_word((prefix + "тысячн", "ый"), form)
    head = cardinal(thousands * 1000) + " " if thousands else ""
    hundreds, last = divmod(rest, 100)
    if last == 0:
        return head + _ord_word(ORD_HUNDREDS[hundreds], form)
    head += (HUNDREDS[hundreds] + " ") if hundreds else ""
    if 10 <= last <= 19:
        return head + _ord_word(ORD_TEENS[last - 10], form)
    tens, units = divmod(last, 10)
    if units == 0:
        return head + _ord_word(ORD_TENS[tens], form)
    return head + ((TENS[tens] + " ") if tens else "") + _ord_word(ORD_UNITS[units], form)


def decimal(integer, fraction, gender="m"):
    """«2,5» -> «две целых пять десятых» (до тысячных)."""
    fraction = fraction[:3]
    whole = cardinal(integer, "f")
    whole_word = "целая" if integer % 10 == 1 and integer % 100 != 11 else "целых"
    value = int(fraction)
    names = {1: ("десятая", "десятых"), 2: ("сотая", "сотых"), 3: ("тысячная", "тысячных")}[len(fraction)]
    part = names[0] if value % 10 == 1 and value % 100 != 11 else names[1]
    return f"{whole} {whole_word} {cardinal(value, 'f')} {part}"


# --- В тексте ---

_PROTECT_RES = [
    re.compile(r"\+\d[\d\s\-()]{6,}\d"),                                   # +7 999 123-45-67
    re.compile(r"(?<!\d)\(?\d{3}\)?[\s\-]\d{2,3}[\s\-]\d{2}[\s\-]\d{2}(?!\d)"),  # 999 123 45 67
    re.compile(r"(?<![\w.])\d+(?:\.\d+){1,3}(?![\w]|\.\d)"),             # 17.2, 3.5, 27.10.2026, 1.2.3
    re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"),                         # 2026-10-27
    re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)"),                 # 12:30
    re.compile(r"(?<!\d)\d+/\d+(?!\d)"),                                    # 1/2
]
_NUM = r"(?<![\w.,:/\-–—+])(\d{1,3}(?:[  ]\d{3})+|\d+)(?:,(\d{1,3}))?(?![\w])(?![.,:/\-–—][\d])(?![\-–—]?[^\W\d_])"
_YEAR_RE = re.compile(r"(?<![\w.,])(1\d{3}|2\d{3})(\s+)(год|году|года|годом|г\.)(?!\w)")
_DATE_RE = re.compile(r"(?<![\w.,])(\d{1,2})(\s+)(" + "|".join(MONTHS) + r")(?!\w)", re.IGNORECASE)
_RANGE_RE = re.compile(r"(?<![\w.,:/\-–—])(\d+)\s?[–—-]\s?(\d+)(?![\w]|[–—\-.,:/]\d)")
_NUMBER_RE = re.compile(_NUM)
_GENITIVE_BEFORE = re.compile(r"(?<!\w)(от|до|около|более|менее|свыше|из)\s+(?:(плюс|минус)\s+)?$", re.IGNORECASE)


def protect(text):
    """Телефоны, версии, даты, время, дроби — в заглушки, чтобы их не тронули правила ниже."""
    saved = []

    def keep(match):
        saved.append(match.group(0))
        return chr(0xE100 + len(saved) - 1)
    for pattern in _PROTECT_RES:
        text = pattern.sub(keep, text)
    return text, saved


def restore(text, saved):
    return re.sub("[-]", lambda m: saved[ord(m.group(0)) - 0xE100], text)


def ranges_to_words(text):
    """«10–12» -> «от 10 до 12» (до перевода чисел и единиц: «до 12°C» получит родительный падеж)."""
    return _RANGE_RE.sub(lambda m: f"от {m.group(1)} до {m.group(2)}", text)


def _year(match):
    n, space, word = int(match.group(1)), match.group(2), match.group(3)
    form = {"год": "nom", "году": "prep", "года": "gen", "годом": "nom", "г.": "nom"}[word.lower()]
    return ordinal(n, form) + space + word


_DATE_NOMINATIVE_BEFORE = re.compile(r"(?<!\w)(сегодня|завтра|вчера|сейчас|это|была|будет)\s*,?\s*$", re.IGNORECASE)


def _date(match):
    """«обновлено 27 октября» -> «двадцать седьмого октября»; «сегодня 27 октября» -> «двадцать седьмое»."""
    day = int(match.group(1))
    if not 1 <= day <= 31:
        return match.group(0)
    form = "neut" if _DATE_NOMINATIVE_BEFORE.search(match.string[:match.start()]) else "gen"
    return ordinal(day, form) + match.group(2) + match.group(3)


def _number(match):
    raw_int, frac = match.group(1), match.group(2)
    n = int(re.sub(r"\D", "", raw_int))
    before = match.string[:match.start()]
    after = match.string[match.end():match.end() + 30]
    noun = after.lstrip()
    gender = "f" if FEMININE_NOUNS.match(noun) else "m"
    if frac:
        return decimal(n, frac, gender)
    words = cardinal(n, gender, genitive=bool(_GENITIVE_BEFORE.search(before)))
    # «1 минуту» — винительный: «одну минуту»
    if gender == "f" and words.endswith("одна") and re.match(r"\w+[ую](?!\w)", noun):
        words = words[:-4] + "одну"
    return words


def numbers_to_words(text):
    text = re.sub(r"№\s?", "номер ", text)
    # «25° тепла» -> «25 градусов тепла» («°C» уже переведены в units_for_speech)
    text = re.sub(r"(?<![\w.,])(\d+)\s?°(?![CС])", lambda m: f"{m.group(1)} " + _plural(
        int(m.group(1)), ("градус", "градуса", "градусов")), text)
    text = _YEAR_RE.sub(_year, text)
    text = _DATE_RE.sub(_date, text)
    return _NUMBER_RE.sub(_number, text)
