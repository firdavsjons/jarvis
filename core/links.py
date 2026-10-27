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


def parse_link_command(text):
    """«открой первую ссылку», «открой 2-ю ссылку», «открой ссылку номер 3», «зайди на первую» -> номер или None."""
    words = re.findall(r"\w+", str(text).lower().replace("ё", "е"))
    if not words or not _VERBS & set(words) or len(words) > 8:
        return None
    number = None
    for i, word in enumerate(words):
        if word.isdigit() and 0 < int(word) < 100:
            number = int(word)
        elif word == "номер" and i + 1 < len(words) and words[i + 1].isdigit():
            number = int(words[i + 1])
        else:
            number = next((n for stem, n in _ORDINALS if word.startswith(stem)), number)
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
