"""Длительности из фраз: «5 минут», «пять минут», «полчаса», «полтора часа», «1 час 30 минут», «90 секунд».

Числительные словами — 0…999. Парсер принимает только фразу целиком: лишнее слово — не длительность (None),
такую фразу разбирает модель.
"""
import re

MAX_SECONDS = 86400

_UNITS = {"ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
          "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
          "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
          "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
         "восемьдесят": 80, "девяносто": 90}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
             "семьсот": 700, "восемьсот": 800, "девятьсот": 900}

_UNIT_STEMS = (("секунд", 1), ("минут", 60), ("час", 3600))   # секунда/секунды/секунд, минуту, часа/часов
FILLERS = {"и", "ещё", "еще"}


def parse_number(words, i):
    """Число с позиции i: цифрами или словами. (значение, следующая позиция) или (None, i)."""
    if i >= len(words):
        return None, i
    if words[i].isdigit():
        return int(words[i]), i + 1
    total, j, found, had_tens = 0, i, False, False
    if j < len(words) and words[j] in _HUNDREDS:
        total, j, found = total + _HUNDREDS[words[j]], j + 1, True
    if j < len(words) and words[j] in _TENS:
        total, j, found, had_tens = total + _TENS[words[j]], j + 1, True, True
    if j < len(words) and words[j] in _UNITS:
        value = _UNITS[words[j]]
        # «двадцать пять» — 20 + 5; «сто пятнадцать» — 100 + 15; «двадцать пятнадцать» — не число
        if not had_tens or 0 < value < 10:
            total, j, found = total + value, j + 1, True
    return (total, j) if found else (None, i)


def _unit_seconds(word):
    for stem, seconds in _UNIT_STEMS:
        if word.startswith(stem):
            return seconds
    return None


def parse_duration(text):
    """Фраза целиком -> секунды (1…86400) или None."""
    words = [w for w in re.findall(r"\w+", str(text).lower().replace("ё", "е")) if w not in FILLERS]
    if not words:
        return None
    total, i = 0, 0
    while i < len(words):
        word = words[i]
        if word == "полчаса":
            total, i = total + 1800, i + 1
        elif word in ("полтора", "полторы"):
            unit = _unit_seconds(words[i + 1]) if i + 1 < len(words) else None
            if unit not in (60, 3600):
                return None
            total, i = total + unit * 3 // 2, i + 2
        elif word.isdigit() or word in _UNITS or word in _TENS or word in _HUNDREDS:
            number, j = parse_number(words, i)
            unit = _unit_seconds(words[j]) if number is not None and j < len(words) else None
            if unit is None:
                return None
            total, i = total + number * unit, j + 1
        elif _unit_seconds(word) is not None:
            total, i = total + _unit_seconds(word), i + 1   # «час», «минуту» без числа = одна единица
        else:
            return None
    return total if 1 <= total <= MAX_SECONDS else None


def duration_phrase(seconds):
    """90 -> «1 минута 30 секунд» (числа цифрами, слова делает озвучка)."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)

    def plural(n, one, few, many):
        if n % 10 == 1 and n % 100 != 11:
            return one
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return few
        return many
    parts = [f"{n} {plural(n, *forms)}" for n, forms in
             ((hours, ("час", "часа", "часов")), (minutes, ("минута", "минуты", "минут")),
              (secs, ("секунда", "секунды", "секунд"))) if n]
    return " ".join(parts) or "0 секунд"


_AFTER_RE = re.compile(r"(?:через|таймер\s+на|на)\s+(.+)", re.IGNORECASE)


def duration_hint(phrase):
    """Подсказка модели для «через 25 минут напомни…»: код считает секунды сам (в уме модель ошибается).
    Берётся самая длинная часть после «через»/«на», которая целиком разбирается как длительность."""
    words = re.findall(r"\w+", str(phrase).lower().replace("ё", "е"))
    for start, word in enumerate(words):
        if word not in ("через", "на"):
            continue
        for end in range(min(len(words), start + 7), start + 1, -1):
            seconds = parse_duration(" ".join(words[start + 1:end]))
            if seconds:
                return f"{' '.join(words[start + 1:end])} = {seconds} секунд", seconds
    return None
