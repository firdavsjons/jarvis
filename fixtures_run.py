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


def isolated_apps(testcase, allowed=None, clock=None):
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
    sched = scheduler.Scheduler(clock=clock or Clock())
    scheduler.set_scheduler(sched)
    testcase.addCleanup(scheduler.set_scheduler, None)
    return sched, path
