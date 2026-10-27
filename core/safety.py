"""Уровни риска действий: SAFE — сразу, CONFIRM — после «да», BLOCKED — никогда."""
import logging
import os
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from core.apps import resolve_app
from core.installed_apps import resolve_app_name

log = logging.getLogger(__name__)

SAFE = "SAFE"
CONFIRM = "CONFIRM"
BLOCKED = "BLOCKED"

# Белый список: всё остальное (sudo, shell, rm, форматирование, настройки безопасности...) — BLOCKED
ACTION_NAMES = {"open_app", "quit_app", "set_volume", "open_url", "trash_file", "find_apps", "list_apps",
                "get_time", "remember", "remember_alias", "list_memory", "forget_memory", "clear_memory"}

# Закрыть можно сразу (macOS сама спросит про несохранённое), кроме этих приложений
CAREFUL_APPS = {"Terminal", "iTerm", "iTerm2", "Visual Studio Code", "Code", "Xcode", "PyCharm",
                "Finder", "System Settings"}

SYSTEM_DIRS = ["/System", "/Library", "/usr", "/bin", "/sbin", "/private", "/Applications"]

TRUSTED_DOMAINS_FILE = Path(__file__).resolve().parent.parent / "trusted_domains.txt"


def home_dir():
    return os.path.realpath(os.path.expanduser("~"))


@lru_cache(maxsize=1)
def load_trusted_domains(path=TRUSTED_DOMAINS_FILE):
    """Домены из файла: по одному на строку, # — комментарий."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        log.warning("Нет списка доверенных доменов %s: %s", path, e)
        return frozenset()
    domains = (line.split("#", 1)[0].strip().lower().rstrip(".") for line in lines)
    return frozenset(d for d in domains if d)


def is_trusted_host(host, trusted):
    """Домен из списка или его поддомен. evil-youtube.com и youtube.com.evil.ru — нет."""
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in trusted)


def _has_control_chars(text):
    return any(unicodedata.category(c) == "Cc" for c in text)


def classify_url(url, trusted=None):
    if not isinstance(url, str) or _has_control_chars(url):
        return BLOCKED, "некорректная ссылка"
    try:
        parsed = urlparse(url.strip())
        host = parsed.hostname
    except ValueError:
        return BLOCKED, "некорректная ссылка"
    if parsed.scheme not in ("http", "https"):
        return BLOCKED, f"схема «{parsed.scheme or 'без схемы'}» запрещена, только http/https"
    if not host:
        return BLOCKED, "в ссылке нет адреса сайта"
    if is_trusted_host(host, load_trusted_domains() if trusted is None else trusted):
        return SAFE, f"доверенный сайт {host}"
    return CONFIRM, f"сайт {host} не в списке доверенных"


def resolve_path(path):
    """expanduser + realpath: «..» и симлинки раскрываются до проверки."""
    return os.path.realpath(os.path.expanduser(path.strip()))


def classify_path(path, home=None):
    """(уровень, причина, нормализованный путь) для удаления в Корзину."""
    if not isinstance(path, str) or not path.strip() or _has_control_chars(path):
        return BLOCKED, "некорректный путь", None
    stripped = path.strip()
    if not stripped.startswith(("/", "~")):
        return BLOCKED, f"относительный путь {stripped!r}: нужен полный путь", None

    home = os.path.realpath(home or home_dir())
    resolved = resolve_path(stripped)
    # Файловая система macOS не различает регистр: /SYSTEM == /System
    r, h = resolved.casefold(), home.casefold()

    if r == "/":
        return BLOCKED, "корень диска", resolved
    if r == h:
        return BLOCKED, "домашняя папка целиком", resolved
    if r.startswith(h + "/"):
        top = resolved[len(home) + 1:].split("/", 1)[0]
        if top.casefold() == "library":
            return BLOCKED, "системная папка ~/Library", resolved
        if top.startswith("."):
            return BLOCKED, f"скрытые файлы и папки ~/{top}", resolved
        return CONFIRM, "файл пользователя", resolved

    for system_dir in SYSTEM_DIRS:
        d = system_dir.casefold()
        if r == d or r.startswith(d + "/"):
            return BLOCKED, f"системная папка {system_dir}", resolved
    return BLOCKED, "вне домашней папки", resolved


def classify(action, trusted=None, home=None):
    """(уровень, причина) для действия {"name": ..., "args": {...}}."""
    if not isinstance(action, dict):
        return BLOCKED, "некорректное действие"
    name = action.get("name")
    if name not in ACTION_NAMES:
        return BLOCKED, f"действие «{name}» не входит в белый список"
    args = action.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    if name in ("open_app", "set_volume"):
        return SAFE, "безопасное действие"
    if name in ("find_apps", "list_apps", "get_time"):
        return SAFE, "только чтение"
    if name in ("remember", "remember_alias", "list_memory", "forget_memory"):
        return SAFE, "память пользователя"  # запись — только если пользователь сказал «запомни» (core/ai.py)
    if name == "clear_memory":
        return CONFIRM, "удаление всей памяти о пользователе"
    if name == "quit_app":
        app = args.get("app")
        if isinstance(app, str) and app.strip():
            # Имя определяется так же, как при выполнении: «Terminl» -> Terminal
            resolved = resolve_app_name(app) or resolve_app(app.strip())
            if resolved.casefold() in {a.casefold() for a in CAREFUL_APPS}:
                return CONFIRM, f"закрытие {resolved} требует подтверждения"
        return SAFE, "закрытие приложения"
    if name == "open_url":
        return classify_url(args.get("url"), trusted)
    if name == "trash_file":
        level, reason, _ = classify_path(args.get("path"), home)
        return level, reason
    return BLOCKED, f"нет правила для действия «{name}»"
