"""Ссылки последнего поиска: «открой первую ссылку» — адрес берёт код из этого списка, а не модель.

Только в памяти процесса (не в истории, не в промпте, не на диске): до 5 ссылок на 10 минут.
"""
import re
import time
from threading import Lock

MAX_LINKS = 5
TTL_SECONDS = 10 * 60
NO_LINK_REPLY = "У меня нет такой ссылки. Сначала попросите что-нибудь найти."

_VERBS = {"открой", "открыть", "откройте", "зайди", "зайдите", "перейди", "перейдите"}
_NOUN_RE = re.compile(r"^(ссылк|сайт|результат)")
_ORDINALS = [("перв", 1), ("втор", 2), ("трет", 3), ("четв", 4), ("пят", 5)]


_READ_VERBS = {"прочитай", "прочти", "прочитайте", "перескажи", "перескажите", "расскажи", "расскажите", "озвучь"}
_SPOKEN_TLDS = {"ком": "com", "ру": "ru", "орг": "org", "нет": "net", "ио": "io", "тж": "tj"}
_FILE_EXTENSIONS = {"txt", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "py", "js", "json", "csv", "md",
                    "jpg", "jpeg", "png", "gif", "mp3", "mp4", "mov", "zip", "dmg", "app", "rtf", "heic"}
_URL_RE = re.compile(r"https?://[^\s<>\"«»()]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"(?<![\w@.\-/])((?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,24}(?:/[^\s<>\"«»(),;!?]*)?)",
                        re.IGNORECASE)


def _number(words):
    number = None
    for i, word in enumerate(words):
        if word.isdigit() and 0 < int(word) < 100:
            number = int(word)
        elif word == "номер" and i + 1 < len(words) and words[i + 1].isdigit():
            number = int(words[i + 1])
        else:
            number = next((n for stem, n in _ORDINALS if word.startswith(stem)), number)
    return number


def urls_in_phrase(text):
    """Адреса, которые пользователь сам сказал: ссылки и латинские домены, голосом — «wikipedia точка org»,
    «example точка ком». Только из этого текста — никогда из ответа модели."""
    spoken = re.sub(r"\s+точка\s+", ".", str(text), flags=re.IGNORECASE)
    spoken = re.sub(r"\.(" + "|".join(_SPOKEN_TLDS) + r")(?![\w])",
                    lambda m: "." + _SPOKEN_TLDS[m.group(1).lower()], spoken, flags=re.IGNORECASE)
    found = [u.rstrip(".,;:!?") for u in _URL_RE.findall(spoken)]
    without_urls = _URL_RE.sub(" ", spoken)
    for domain in _DOMAIN_RE.findall(without_urls):
        domain = domain.rstrip(".,;:!?")
        if domain.split("/")[0].rsplit(".", 1)[-1].lower() not in _FILE_EXTENSIONS:  # report.txt — файл, не сайт
            found.append("https://" + domain)
    return list(dict.fromkeys(found))


def parse_read_command(text):
    """«прочитай/перескажи первую ссылку», «что там на третьей ссылке», «прочитай сайт example.com»
    -> ("number", N) | ("url", адрес из фразы) | None."""
    low = str(text).lower().replace("ё", "е")
    words = re.findall(r"\w+", low)
    asks = bool(_READ_VERBS & set(words)) or (("что" in words) and bool({"на", "там"} & set(words)))
    if not words or not asks or len(words) > 12:
        return None
    urls = urls_in_phrase(text)
    if urls:
        return ("url", urls[0])
    number = _number(words)
    if number is not None and any(_NOUN_RE.match(w) or w.startswith("страниц") for w in words):
        return ("number", number)
    return None


def parse_link_command(text):
    """«открой первую ссылку», «открой 2-ю ссылку», «открой ссылку номер 3», «зайди на первую» -> номер или None."""
    words = re.findall(r"\w+", str(text).lower().replace("ё", "е"))
    if not words or not _VERBS & set(words) or len(words) > 8:
        return None
    number = _number(words)
    if number is None:
        return None
    has_noun = any(_NOUN_RE.match(w) for w in words)
    # «зайди на первую» — без существительного, но только с «зайди»/«перейди» и порядковым словом
    goes_to = bool({"зайди", "зайдите", "перейди", "перейдите"} & set(words))
    return number if has_noun or goes_to else None


class SearchLinks:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._links, self._time = [], None
        self._lock = Lock()

    def store(self, urls):
        urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))][:MAX_LINKS]
        if urls:
            with self._lock:
                self._links, self._time = urls, self._clock()

    def get(self, number):
        """URL по номеру (с 1) или None: нет поиска, прошло больше 10 минут или номер вне списка."""
        with self._lock:
            if self._time is None or self._clock() - self._time > TTL_SECONDS:
                return None
            return self._links[number - 1] if 1 <= number <= len(self._links) else None
