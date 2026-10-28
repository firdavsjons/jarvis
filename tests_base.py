"""Техническая защита тестов: `import tests_base` первой строкой каждого test*.py.

1. subprocess.run, subprocess.Popen (а через него call / check_output / check_call), os.system, os.popen, os.exec*,
   os.posix_spawn* по умолчанию бросают ForbiddenInTests — настоящий запуск процессов в тестах запрещён, пока тест
   явно не подменил их моком (patch(...) заменяет функцию, и защита не вызывается).
2. Запись, создание, переименование и удаление внутри ~/Library/Application Support/Jarvis, ~/Library/Logs/Jarvis,
   ~/Desktop, ~/Documents и ~/Downloads бросают ForbiddenInTests. Пути данных приложения (DATA_DIR, MEMORY_FILE,
   CACHE_FILE, TIMERS_FILE, SHORTCUTS_FILE, LOG_DIR) заменены на временную папку, поэтому код, который пишет «по
   умолчанию», пишет туда, а не к пользователю.
Исключений (osacompile, py_compile) сейчас нет: тестам они не нужны. Если понадобятся — отдельным явным
разрешением здесь, а не ослаблением защиты.

ForbiddenInTests наследуется от BaseException: `except Exception` и `except OSError` в коде приложения её не
проглатывают, а unittest честно показывает ошибку теста. Все нарушения копятся в VIOLATIONS.
"""
import atexit
import builtins
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class ForbiddenInTests(BaseException):
    """Настоящий запуск процесса или запись в домашние папки в тесте без подмены."""


VIOLATIONS = []
SUBPROCESS_MESSAGE = "настоящий запуск запрещён в тестах: подмените {name} моком (patch)"

HOME = Path.home()
FORBIDDEN_DIRS = [
    HOME / "Library" / "Application Support" / "Jarvis",
    HOME / "Library" / "Logs" / "Jarvis",
    HOME / "Desktop",
    HOME / "Documents",
    HOME / "Downloads",
]
_FORBIDDEN_KEYS = [os.path.realpath(p).casefold() for p in FORBIDDEN_DIRS]

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="jarvis-tests-")).resolve()
_real_rmtree = shutil.rmtree
atexit.register(_real_rmtree, TEMP_ROOT, ignore_errors=True)

_real = {
    "run": subprocess.run, "Popen": subprocess.Popen, "system": os.system, "popen": os.popen,
    "open": builtins.open, "io_open": io.open, "os_open": os.open, "mkdir": os.mkdir, "makedirs": os.makedirs,
    "replace": os.replace, "rename": os.rename, "remove": os.remove, "unlink": os.unlink, "rmdir": os.rmdir,
    "chmod": os.chmod,
}
_installed = False


def _violation(message):
    VIOLATIONS.append(message)
    raise ForbiddenInTests(message)


def is_forbidden_path(path):
    """Путь внутри (или равный) одной из защищённых папок. Числа (дескрипторы) и пустое — нет."""
    try:
        key = os.path.realpath(os.fsdecode(os.fspath(path))).casefold()
    except (TypeError, ValueError):
        return False
    return any(key == d or key.startswith(d + os.sep) for d in _FORBIDDEN_KEYS)


def _check_write(path, how):
    if is_forbidden_path(path):
        _violation(f"запись в домашнюю папку запрещена в тестах ({how}): {path}. Используйте временную папку.")


def _is_write_mode(mode):
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def _blocked_call(name):
    def blocked(*args, **kwargs):
        _violation(SUBPROCESS_MESSAGE.format(name=name))
    blocked.__name__ = name
    return blocked


def _redirect_app_paths():
    """Файлы приложения — во временную папку: код, пишущий «по умолчанию», не доберётся до папки пользователя."""
    data = TEMP_ROOT / "Application Support" / "Jarvis"
    logs = TEMP_ROOT / "Logs" / "Jarvis"
    from core import cache, contacts, logger, memory, scheduler, storage, telegram_check
    from core.apps import shortcuts
    storage.DATA_DIR = data
    cache.CACHE_FILE = data / "command_cache.json"
    memory.MEMORY_FILE = data / "memory.json"
    scheduler.TIMERS_FILE = data / "timers.json"
    shortcuts.SHORTCUTS_FILE = data / "shortcuts_allowed.json"
    contacts.CONTACTS_FILE = data / "contacts.json"
    telegram_check.LAST_FILE = data / "telegram_check_last.json"
    logger.LOG_DIR = logs
    logger.LOG_FILE = logs / "jarvis.log"


def install():
    """Включает защиту (один раз на процесс)."""
    global _installed
    if _installed:
        return
    _installed = True

    for name, attr in (("subprocess.run", "run"), ("subprocess.Popen", "Popen")):
        setattr(subprocess, attr, _blocked_call(name))
    os.system = _blocked_call("os.system")
    os.popen = _blocked_call("os.popen")
    for attr in ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe"):
        if hasattr(os, attr):
            setattr(os, attr, _blocked_call(f"os.{attr}"))

    def guarded_open(file, mode="r", *args, **kwargs):
        if _is_write_mode(mode):
            _check_write(file, "open")
        return _real["open"](file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if _is_write_mode(mode):
            _check_write(file, "io.open")
        return _real["io_open"](file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            _check_write(path, "os.open")
        return _real["os_open"](path, flags, *args, **kwargs)

    def one_path(name):
        def guarded(path, *args, **kwargs):
            # dir_fd: путь относительный к открытой папке (так работает shutil.rmtree) — проверить его нельзя;
            # сам вход в папку проверяет вызов с полным путём (rmtree ниже)
            if kwargs.get("dir_fd") is None:
                _check_write(path, name)
            return _real[name](path, *args, **kwargs)
        guarded.__name__ = name
        return guarded

    def two_paths(name):
        def guarded(src, dst, *args, **kwargs):
            if kwargs.get("src_dir_fd") is None:
                _check_write(src, name)
            if kwargs.get("dst_dir_fd") is None:
                _check_write(dst, name)
            return _real[name](src, dst, *args, **kwargs)
        guarded.__name__ = name
        return guarded

    real_rmtree = shutil.rmtree

    def guarded_rmtree(path, *args, **kwargs):
        _check_write(path, "shutil.rmtree")
        return real_rmtree(path, *args, **kwargs)

    shutil.rmtree = guarded_rmtree
    builtins.open = guarded_open
    io.open = guarded_io_open
    os.open = guarded_os_open
    for name in ("mkdir", "makedirs", "remove", "unlink", "rmdir", "chmod"):
        setattr(os, name, one_path(name))
    for name in ("replace", "rename"):
        setattr(os, name, two_paths(name))
    _redirect_app_paths()


install()
