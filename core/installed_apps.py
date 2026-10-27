"""Какие приложения установлены на этом Mac. Только чтение папок, без subprocess."""
import difflib
import os
import re
import time
from threading import Lock

from core.apps import resolve_app

APP_DIRS = [
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    "~/Applications",
]
VENDOR_DIRS = ["/Applications"]  # здесь смотрим ещё на уровень глубже: /Applications/MAMP/MAMP.app
EXTRA_APPS = ["/System/Library/CoreServices/Finder.app"]  # Finder не лежит в папках выше

CACHE_SECONDS = 600
FUZZY_CUTOFF = 0.75   # resolve_app_name: только уверенные совпадения
FUZZY_MARGIN = 0.05   # и лучший вариант должен опережать второй хотя бы на столько
SEARCH_CUTOFF = 0.6   # find_apps: поиск может быть мягче
FIND_LIMIT = 10
LIST_LIMIT = 30

# Популярные — первыми в list_apps и в системном промпте
POPULAR_APPS = [
    "Safari", "Telegram", "Google Chrome", "WhatsApp", "YouTube", "Mail", "Calendar", "Notes",
    "Messages", "Photos", "Music", "Spotify", "Finder", "Maps", "Weather", "Reminders", "Calculator",
    "System Settings", "App Store", "FaceTime", "Preview", "TextEdit", "Terminal", "Visual Studio Code",
    "Microsoft Word", "Microsoft Excel", "VLC", "Shazam", "ChatGPT", "Claude",
]

_cache = {"time": None, "apps": None}
_lock = Lock()
_user_aliases = {}  # из памяти пользователя (core/memory.py): «как говорю» -> имя приложения


def alias_key(spoken):
    """Ключ псевдонима: слова в нижнем регистре через пробел («Мой  Браузер!» -> «мой браузер»)."""
    return " ".join(re.findall(r"\w+", str(spoken).lower()))


def set_user_aliases(aliases):
    global _user_aliases
    with _lock:
        _user_aliases = {alias_key(k): v for k, v in dict(aliases).items() if alias_key(k)}


def _words(text):
    return set(re.findall(r"\w+", text.casefold()))


def _scan_dir(path, depth):
    try:
        entries = list(os.scandir(os.path.expanduser(path)))
    except OSError:
        return []
    names = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.name.endswith(".app") and entry.is_dir():
                names.append(entry.name[:-4])
            elif depth > 0 and entry.is_dir(follow_symlinks=False):
                names += _scan_dir(entry.path, depth - 1)
        except OSError:
            continue
    return names


def scan_apps():
    names = []
    for path in APP_DIRS:
        names += _scan_dir(path, 1 if path in VENDOR_DIRS else 0)
    names += [os.path.basename(p)[:-4] for p in EXTRA_APPS if os.path.isdir(p)]
    unique = {}
    for name in names:
        unique.setdefault(name.casefold(), name)
    return sorted(unique.values(), key=str.casefold)


def list_installed_apps():
    """Имена установленных приложений (без .app). Кэш в памяти на CACHE_SECONDS."""
    with _lock:
        now = time.monotonic()
        if _cache["apps"] is None or now - _cache["time"] > CACHE_SECONDS:
            _cache["apps"], _cache["time"] = scan_apps(), now
        return list(_cache["apps"])


def clear_cache():
    with _lock:
        _cache["apps"], _cache["time"] = None, None


def resolve_app_name(name, use_user_aliases=True):
    """Название от пользователя или модели -> имя установленного приложения или None.

    0) псевдонимы пользователя из памяти; 1) словарь APP_ALIASES; 2) точное совпадение без учёта
    регистра; 3) совпадение по слову («Chrome» -> Google Chrome, если такое одно); 4) нечёткое (difflib).
    use_user_aliases=False — при запоминании псевдонима (без цепочек псевдоним -> псевдоним).
    """
    if not isinstance(name, str) or not name.strip():
        return None
    if use_user_aliases:
        with _lock:
            name = _user_aliases.get(alias_key(name), name)
    name = resolve_app(name.strip())
    installed = list_installed_apps()
    if not installed:
        return name  # папки не прочитались — не мешаем, как раньше
    by_key = {app.casefold(): app for app in installed}
    key = name.casefold()
    if key in by_key:
        return by_key[key]

    words = _words(key)
    hits = [app for app in installed if words and words <= _words(app)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return None  # «Code»: Visual Studio Code и Claude Code — не угадываем (иначе нечётко вышел бы Xcode)

    # Нечётко — только если лучший вариант заметно лучше второго: «microsoft» ~ Word и Excel -> None
    ratios = sorted(((difflib.SequenceMatcher(None, key, k).ratio(), k) for k in by_key), reverse=True)
    if not ratios or ratios[0][0] < FUZZY_CUTOFF:
        return None
    if len(ratios) > 1 and ratios[0][0] - ratios[1][0] < FUZZY_MARGIN:
        return None
    return by_key[ratios[0][1]]


def find_apps(query, limit=FIND_LIMIT):
    """До limit установленных приложений, похожих на запрос: точные, по слову/подстроке, нечёткие."""
    query = query.strip()
    keys = {query.casefold(), resolve_app(query).casefold()}
    scored = []
    for app in list_installed_apps():
        key = app.casefold()
        if key in keys:
            score = 3.0
        elif any(k in key or _words(k) & _words(key) for k in keys):
            score = 2.0
        else:
            score = max(difflib.SequenceMatcher(None, k, key).ratio() for k in keys)
            if score < SEARCH_CUTOFF:
                continue
        scored.append((-score, key, app))
    return [app for _, _, app in sorted(scored)[:limit]]


def popular_first(apps, limit=LIST_LIMIT):
    by_key = {app.casefold(): app for app in apps}
    popular = [by_key[p.casefold()] for p in POPULAR_APPS if p.casefold() in by_key]
    rest = [app for app in apps if app not in popular]
    return (popular + rest)[:limit]
