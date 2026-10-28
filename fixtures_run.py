"""Общие подмены для тестов приложений: subprocess.run со сценарием ответов, изолированные файлы и часы.
Не test*.py, поэтому сам не запускается."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import scheduler


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def scripted_run(*outputs):
    """subprocess.run, отдающий ответы по очереди (последний повторяется); Exception — выбросить."""
    queue = list(outputs)

    def fake(args, **kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, SimpleNamespace) else completed(item)
    return MagicMock(side_effect=fake)


class Clock:
    def __init__(self, t=1_792_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


SAMPLE_CONTACTS = {
    "мама": {"username": "mama_user", "phone": "", "title": "Мама"},
    "Аня": {"username": "anya_user", "phone": "", "title": "Аня Иванова"},
    "Анна": {"username": "anna_user", "phone": "", "title": "Анна Петрова"},
    "Иван Иванов": {"username": "", "phone": "+7 999 123-45-67", "title": "Иван Иванов"},
    "Иван Петров": {"username": "ivan_petrov", "phone": "", "title": "Иван Петров"},
    "себе": {"username": "my_self_user", "phone": "", "title": "Избранное"},
}


def isolated_apps(testcase, allowed=None, clock=None, contacts=SAMPLE_CONTACTS):
    """Изолирует тест от реальных данных: файл разрешённых Быстрых команд — во временной папке, планировщик —
    в памяти с подменёнными часами. Возвращает (планировщик, путь к файлу разрешённых команд)."""
    from core.apps import shortcuts
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "shortcuts_allowed.json"
    if allowed is not None:
        import json
        path.write_text(json.dumps(allowed, ensure_ascii=False), encoding="utf-8")
    patcher = patch.object(shortcuts, "SHORTCUTS_FILE", path)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    from core import contacts as contacts_store
    contacts_path = Path(tmp.name) / "contacts.json"
    if contacts is not None:
        import json as _json
        contacts_path.write_text(_json.dumps(contacts, ensure_ascii=False), encoding="utf-8")
    contacts_patcher = patch.object(contacts_store, "CONTACTS_FILE", contacts_path)
    contacts_patcher.start()
    testcase.addCleanup(contacts_patcher.stop)
    testcase.contacts_file = contacts_path
    sched = scheduler.Scheduler(clock=clock or Clock())
    scheduler.set_scheduler(sched)
    testcase.addCleanup(scheduler.set_scheduler, None)
    return sched, path
