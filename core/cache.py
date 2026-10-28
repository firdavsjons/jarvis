"""Кэш команд: точная фраза -> SAFE-действия, без запроса к модели.

~/Library/Application Support/Jarvis/command_cache.json — до 500 записей (вытесняются давно не использованные),
срок жизни 90 дней. Записывается только после успешного выполнения.
"""
import copy
import re
import time
from threading import Lock

from urllib.parse import urlparse

from core import installed_apps, safety
from core.apps import ALIAS_PREFIX_MIN, APP_ALIASES
from core.storage import DATA_DIR, atomic_write_json, read_json

CACHE_FILE = DATA_DIR / "command_cache.json"
MAX_ENTRIES = 500
TTL_SECONDS = 90 * 24 * 3600
MAX_ACTIONS = 3

FILLER_WORDS = {"пожалуйста", "мне", "а", "ну", "давай"}
# Фразы, смысл которых зависит от разговора: «открой его», «запусти снова» — не кэшируем
CONTEXT_WORDS = {"его", "её", "ее", "это", "этот", "эту", "эти", "то", "тот", "ту", "их", "там", "тут",
                 "снова", "ещё", "еще", "опять"}
CACHEABLE_ACTIONS = {"open_app", "quit_app", "set_volume", "open_url"}
CLEARED_REPLY = "Кэш команд очищен."


def normalize_phrase(text):
    """«Открой, пожалуйста, Telegram!» -> «открой telegram»."""
    return " ".join(w for w in re.findall(r"\w+", str(text).lower()) if w not in FILLER_WORDS)


def is_forget_cache_command(text):
    words = set(re.findall(r"\w+", text.lower()))
    return bool({"забудь", "очисти", "сотри"} & words and {"кэш", "кеш"} & words)


# Шаблоны фраз. Кэш повторяет действие по фразе без модели, поэтому действие обязано следовать из самой фразы:
# модель могла ошибиться («главный экран» -> Preview), и ошибка закрепилась бы навсегда.
OPEN_WORDS = {"открой", "открыть", "запусти", "запустить", "включи", "зайди", "перейди", "на", "в", "сайт",
              "страницу", "страница"}
VOLUME_WORDS = {"поставь", "установи", "сделай", "выставь", "громкость", "звук", "на", "до", "в", "процент",
                "процента", "процентов"}
TLD_WORDS = {"com", "ru", "org", "net", "tj", "io", "точка", "ком", "ру", "орг"}
SITE_NAMES = {"ютуб": "youtube", "гугл": "google", "яндекс": "yandex", "википедия": "wikipedia", "гитхаб": "github",
              "вк": "vk"}


def app_in_phrase(app, phrase_words, normalized):
    """Приложение действительно названо во фразе: его слово (если оно однозначное), русское название
    из словаря (с падежом), псевдоним пользователя. Нечёткого сравнения нет."""
    resolved = installed_apps.resolve_app_name(app)
    if not resolved:
        return False
    installed = installed_apps.list_installed_apps()
    for word in installed_apps.words_of(resolved):
        # «chrome» в «Google Chrome» годится, «code» (Visual Studio Code и Claude Code) — нет
        if len(word) >= 3 and word in phrase_words \
                and sum(1 for a in installed if word in installed_apps.words_of(a)) == 1:
            return True
    for key, value in APP_ALIASES.items():
        if value.casefold() == resolved.casefold() and any(
                w == key or (len(key) >= ALIAS_PREFIX_MIN and w.startswith(key)) for w in phrase_words):
            return True
    padded = f" {normalized} "
    return any(f" {k} " in padded and (installed_apps.resolve_app_name(v, use_user_aliases=False) or "").casefold()
               == resolved.casefold() for k, v in installed_apps.user_aliases().items())


def volume_template(level, phrase_words):
    """«громкость 50», «поставь громкость на 50 процентов»: ровно одно число, равное уровню."""
    numbers = [w for w in phrase_words if w.isdigit()]
    rest = [w for w in phrase_words if not w.isdigit()]
    return (numbers == [str(level)] and {"громкость", "звук"} & set(rest) and set(rest) <= VOLUME_WORDS)


def url_template(url, phrase_words):
    """«открой youtube», «открой ютуб»: после слов открытия остаётся одно название сайта из самой ссылки."""
    host = (urlparse(url).hostname or "").lower()
    labels = [p for p in host.split(".") if p and p != "www"]
    if len(labels) < 2:
        return False
    label = labels[-2]
    rest = [w for w in phrase_words if w not in OPEN_WORDS and w not in TLD_WORDS]
    return len(rest) == 1 and (rest[0] == label or SITE_NAMES.get(rest[0]) == label)


def action_follows_phrase(action, phrase, single):
    """Следует ли действие из фразы. Нет известного шаблона — не кэшируем."""
    words = re.findall(r"\w+", normalize_phrase(phrase))
    normalized = " ".join(words)
    args = action.get("args") or {}
    name = action.get("name")
    if name in ("open_app", "quit_app"):
        return isinstance(args.get("app"), str) and app_in_phrase(args["app"], set(words), normalized)
    if not single:
        return False  # громкость и ссылки — только когда вся фраза про них одних
    if name == "set_volume":
        return bool(volume_template(args.get("level"), words))
    if name == "open_url":
        return isinstance(args.get("url"), str) and url_template(args["url"], words)
    return False


def cacheable_actions(results, phrase):
    """Действия из результатов, если их можно кэшировать для этой фразы; иначе None."""
    if not results or len(results) > MAX_ACTIONS:
        return None
    actions = []
    for r in results:
        action = r.get("action")
        if not r.get("ok") or not isinstance(action, dict) or action.get("name") not in CACHEABLE_ACTIONS:
            return None
        if safety.classify(action)[0] != safety.SAFE:  # open_url — только доверенные сайты
            return None
        if not action_follows_phrase(action, phrase, single=len(results) == 1):
            return None
        actions.append({"name": action["name"], "args": dict(action.get("args") or {})})
    return actions


class CommandCache:
    def __init__(self, path=None, clock=time.time, max_entries=MAX_ENTRIES, ttl=TTL_SECONDS):
        """path=None — только в памяти (тесты); в приложении — CACHE_FILE."""
        self.path = path
        self._clock = clock
        self.max_entries = max_entries
        self.ttl = ttl
        self._lock = Lock()
        data = read_json(path, {}) if path else {}
        self._data = {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("actions"), list)} \
            if isinstance(data, dict) else {}

    def __len__(self):
        with self._lock:
            return len(self._data)

    def _save(self):
        if self.path:
            atomic_write_json(self.path, self._data)

    def lookup(self, text):
        """Действия для точно такой же (нормализованной) фразы или None. Просроченная запись удаляется."""
        key = normalize_phrase(text)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if self._clock() - entry.get("created", 0) > self.ttl:  # 90 дней с создания, даже если пользуются
                del self._data[key]
                self._save()
                return None
            return copy.deepcopy(entry["actions"])

    def mark_used(self, text):
        key = normalize_phrase(text)
        with self._lock:
            entry = self._data.get(key)
            if entry:
                entry["uses"] = entry.get("uses", 0) + 1
                entry["last_used"] = self._clock()
                self._save()

    def store(self, text, actions):
        key = normalize_phrase(text)
        if not key or not actions or len(actions) > MAX_ACTIONS or CONTEXT_WORDS & set(key.split()):
            return False
        now = self._clock()
        with self._lock:
            old = self._data.get(key, {})
            self._data[key] = {"actions": copy.deepcopy(actions), "uses": old.get("uses", 0) + 1,
                               "created": old.get("created", now), "last_used": now}
            while len(self._data) > self.max_entries:  # вытесняем самую давно использованную
                oldest = min(self._data, key=lambda k: self._data[k].get("last_used", 0))
                del self._data[oldest]
            self._save()
        return True

    def remove(self, text):
        with self._lock:
            if self._data.pop(normalize_phrase(text), None) is not None:
                self._save()

    def clear(self):
        with self._lock:
            self._data = {}
            self._save()
