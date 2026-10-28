"""Быстрые команды (приложение «Команды»): list и run через `shortcuts` со списком аргументов.

Запускать можно только имена из ~/Library/Application Support/Jarvis/shortcuts_allowed.json. Файл меняет только
пользователь, руками: ни один инструмент, действие, ответ модели или быстрое правило в него не пишут (единственное
исключение — один раз создаётся с настройками по умолчанию, если файла ещё нет). Уровень берётся из файла:
SAFE — сразу, CONFIRM — после «да»; имя не из файла — BLOCKED. Команда должна существовать в `shortcuts list`.
"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from core.apps import common
from core.apps.registry import BLOCKED, CONFIRM, SAFE, AppTool
from core.errors import ActionError
from core.storage import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger(__name__)

SHORTCUTS_FILE = DATA_DIR / "shortcuts_allowed.json"
DEFAULT_ALLOWED = {"Jarvis Alarm": SAFE, "Jarvis Timer": SAFE, "Jarvis Focus": SAFE}
RUN_TIMEOUT = 30
LIST_TIMEOUT = 10
MAX_INPUT = 200
MAX_NAME = 100
LEVELS = {SAFE, CONFIRM, BLOCKED}
DOCS_HINT = "инструкция в файле docs/shortcuts.md"


def load_allowed(path=None):
    """{имя: уровень}. Нет файла — создаётся с настройками по умолчанию (единственная запись Jarvis в него).
    Файл — словарь {имя: SAFE|CONFIRM} или список имён (все CONFIRM)."""
    path = Path(path or SHORTCUTS_FILE)
    if not path.exists():
        try:
            atomic_write_json(path, DEFAULT_ALLOWED)
        except OSError as e:
            log.warning("Не удалось создать %s: %s", path, e)
        return dict(DEFAULT_ALLOWED)
    data = read_json(path, None)
    if isinstance(data, list):
        data = {name: CONFIRM for name in data}
    if not isinstance(data, dict):
        log.warning("%s: ожидался словарь или список, разрешённых команд нет", path)
        return {}
    allowed = {}
    for name, level in data.items():
        if isinstance(name, str) and name.strip():
            level = str(level).strip().upper()
            allowed[name.strip()] = level if level in LEVELS else CONFIRM
    return allowed


def installed_shortcuts():
    """Имена из `shortcuts list`."""
    try:
        result = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ActionError("shortcuts list: таймаут", "Быстрые команды не ответили вовремя")
    except OSError as e:
        raise ActionError(str(e), "приложение «Быстрые команды» недоступно")
    if result.returncode != 0:
        raise ActionError(f"shortcuts list: {(result.stderr or '').strip()}", "не удалось получить список команд")
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def allowed_level(name):
    return load_allowed().get(name, BLOCKED)


def _level(args):
    if args.get("action") == "run":
        name = args.get("name")
        if not isinstance(name, str):
            return BLOCKED, "не указано имя команды"
        level = allowed_level(name.strip())
        reason = {SAFE: "команда из списка разрешённых", CONFIRM: "команда требует подтверждения",
                  BLOCKED: "команды нет в списке разрешённых"}[level]
        return level, reason
    return None


def run_shortcut(name, text="", timeout=RUN_TIMEOUT, installed=None):
    """Запуск по имени. Вызывающий обязан проверить уровень (SAFE или подтверждение): здесь — повторная проверка
    списка и существования команды. Возвращает вывод (≤300 символов)."""
    name = common.clean_text(name, "имя команды", MAX_NAME)
    text = common.clean_text(text, "вход команды", MAX_INPUT, required=False)
    if allowed_level(name) == BLOCKED:
        raise ActionError(f"shortcuts: {name!r} не в списке разрешённых", "этой команды нет в списке разрешённых")
    if name not in (installed_shortcuts() if installed is None else installed):
        raise ActionError(f"shortcuts: {name!r} нет в `shortcuts list`", f"команды «{name}» нет в приложении")
    with tempfile.TemporaryDirectory(prefix="jarvis-shortcut-") as tmp:   # папка 700
        args = ["shortcuts", "run", name]
        if text:
            source = Path(tmp) / "input.txt"
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            args += ["--input-path", str(source)]
        target = Path(tmp) / "output.txt"
        args += ["--output-path", str(target), "--output-type", "public.utf8-plain-text"]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ActionError(f"shortcuts run {name}: таймаут {timeout} с", f"команда «{name}» не ответила вовремя")
        except OSError as e:
            raise ActionError(str(e), "команда не запустилась")
        if result.returncode != 0:
            raise common.permission_error("Shortcuts", result.stderr) or ActionError(
                f"shortcuts run {name}: {common.limit_text(result.stderr, 120)}", f"команда «{name}» не выполнилась")
        output = ""
        try:
            output = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        except OSError:
            pass
        return common.limit_text(output or result.stdout or "")


def run(action, name, input, phrase=""):
    if action == "list":
        allowed = load_allowed()
        names = [n for n in installed_shortcuts() if allowed.get(n, BLOCKED) != BLOCKED]
        if not names:
            return common.done("Разрешённых быстрых команд нет")
        return common.done("Доступны команды: " + ", ".join(names[:common.MAX_LINES]))
    if action == "run":
        output = run_shortcut(name, input)
        name = name.strip()
        return common.done(output or f"Команда «{name}» выполнена", external=True)
    raise ActionError(f"shortcuts: неизвестное действие {action!r}", "неизвестное действие")


def preview(action, name, input, phrase=""):
    text = common.clean_text(input, "вход команды", MAX_INPUT, required=False)
    return f"запустить быструю команду «{common.clean_text(name, 'имя команды', MAX_NAME)}»" + \
        (f" с входом «{text}»" if text else "")


TOOLS = [AppTool(
    name="shortcuts",
    description="Быстрые команды: список разрешённых и запуск по имени. Вход и имя пустые, если не нужны.",
    topic="apps",
    keywords=("быстр", "шорткат", "shortcut", "фокус", "беспокоить", "jarvis"),
    actions={"list": SAFE, "run": CONFIRM},
    action_description="list — разрешённые; run — запустить",
    params={"name": {"type": "string", "description": "Имя команды из списка; для list пустая строка"},
            "input": {"type": "string", "description": "Вход команды (до 200 символов) или пустая строка"}},
    run=run,
    external=frozenset({"run"}),
    destructive=frozenset({"run"}),
    preview=preview,
    level_fn=_level,
)]
