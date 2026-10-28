"""Внутренние таймеры и будильники Jarvis: фоновый планировщик, хранение в timers.json.

Срабатывают, только пока Jarvis запущен. Файл (права 600, атомарная запись) нужен, чтобы пережить перезапуск:
не сработавшие ждут дальше, пропущенные за время простоя озвучиваются одной фразой. До 20 активных.
Для надёжного будильника («разбуди меня») используется приложение «Напоминания» (core/apps/clock.py).
"""
import logging
import subprocess
import threading
import time
import uuid
from datetime import datetime

from core import journal
from core.errors import ActionError
from core.storage import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger(__name__)

TIMERS_FILE = DATA_DIR / "timers.json"
MAX_ACTIVE = 20
SOUND_FILE = "/System/Library/Sounds/Glass.aiff"
KINDS = ("timer", "alarm")
MAX_SLEEP = 30.0   # проверять не реже: часы Mac могут перевестись, Mac — проснуться

KIND_NAMES = {"timer": "Таймер", "alarm": "Будильник"}


class Scheduler:
    def __init__(self, path=None, clock=time.time):
        """path=None — только в памяти (тесты); в приложении — TIMERS_FILE. clock — время в секундах (epoch)."""
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._items = []
        self.on_fire = None     # callback(item) — звук, уведомление, озвучка
        self.on_missed = None   # callback(items) — пропущенные за время простоя, одной фразой
        self.missed = []
        self._load()

    # --- Хранение ---

    @staticmethod
    def _valid(item):
        return (isinstance(item, dict) and item.get("kind") in KINDS and isinstance(item.get("id"), str)
                and isinstance(item.get("due"), (int, float)) and not isinstance(item.get("due"), bool)
                and isinstance(item.get("label", ""), str))

    def _load(self):
        data = read_json(self.path, {}) if self.path else {}
        raw = data.get("items", []) if isinstance(data, dict) else []
        now = self._clock()
        items = [i for i in raw if self._valid(i)][:MAX_ACTIVE]
        self._items = sorted((i for i in items if i["due"] > now), key=lambda i: i["due"])
        self.missed = sorted((i for i in items if i["due"] <= now), key=lambda i: i["due"])
        if self.missed:
            self._save()   # пропущенные озвучатся один раз и больше не вернутся

    def _save(self):
        if self.path:
            atomic_write_json(self.path, {"items": self._items})

    # --- Операции ---

    def add(self, kind, due, label=""):
        with self._lock:
            if len(self._items) >= MAX_ACTIVE:
                raise ActionError(f"больше {MAX_ACTIVE} активных таймеров и будильников",
                                  f"уже {MAX_ACTIVE} активных таймеров и будильников, отмените лишние")
            item = {"id": uuid.uuid4().hex[:8], "kind": kind, "due": float(due), "label": label,
                    "created": self._clock()}
            self._items.append(item)
            self._items.sort(key=lambda i: i["due"])
            self._save()
        self._wake.set()
        return item

    def items(self, kind=None):
        with self._lock:
            return [dict(i) for i in self._items if kind is None or i["kind"] == kind]

    def remove(self, item_id):
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != item_id]
            if len(self._items) != before:
                self._save()
        self._wake.set()

    def now(self):
        return self._clock()

    def remaining(self, item):
        return max(0.0, item["due"] - self._clock())

    # --- Срабатывание ---

    def tick(self):
        """Срабатывают все наступившие. Вызывается потоком; в тестах — вручную с подменёнными часами."""
        now = self._clock()
        with self._lock:
            due = [i for i in self._items if i["due"] <= now]
            if due:
                self._items = [i for i in self._items if i["due"] > now]
                self._save()
        for item in due:
            try:
                if self.on_fire:
                    self.on_fire(item)
            except Exception as e:   # один сбой не должен остановить остальные и поток
                log.error("Сбой при срабатывании %s %s: %s", item["kind"], item["id"], type(e).__name__)
        return due

    def announce_missed(self):
        missed, self.missed = self.missed, []
        if missed and self.on_missed:
            try:
                self.on_missed(missed)
            except Exception as e:
                log.error("Сбой при объявлении пропущенных: %s", type(e).__name__)
        return missed

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _loop(self):
        self.announce_missed()
        while not self._stop.is_set():
            self.tick()
            with self._lock:
                nearest = min((i["due"] for i in self._items), default=None)
            timeout = MAX_SLEEP if nearest is None else max(0.0, min(MAX_SLEEP, nearest - self._clock()))
            self._wake.wait(timeout)
            self._wake.clear()


# --- Тексты и обработчики срабатывания ---

def fire_text(item):
    label = (item.get("label") or "").strip()
    if item["kind"] == "timer":
        return f"Таймер «{label}» сработал." if label else "Таймер сработал."
    return f"Будильник: {label}." if label else "Будильник."


def missed_phrase(items):
    """Одна фраза про всё пропущенное: «Пока Jarvis был выключен, сработали: будильник на 7:00, таймер «чай»»."""
    parts = []
    for item in items:
        label = (item.get("label") or "").strip()
        if item["kind"] == "alarm":
            when = datetime.fromtimestamp(item["due"])
            parts.append(f"будильник на {when.hour}:{when.minute:02d}" + (f" «{label}»" if label else ""))
        else:
            parts.append(f"таймер «{label}»" if label else "таймер")
    if len(parts) == 1:
        return f"Пока Jarvis был выключен, сработал {parts[0]}."
    return f"Пока Jarvis был выключен, сработали: {', '.join(parts)}."


def make_fire_handler(speak, popen=subprocess.Popen, run=subprocess.run):
    """Срабатывание: звук Glass (без ожидания), уведомление (текст через argv), озвучка через общую блокировку
    SpeechEngine.speak — поверх идущей речи не накладывается, — и запись в журнал."""
    def handle(item):
        text = fire_text(item)
        try:
            popen(["afplay", SOUND_FILE])
        except OSError as e:
            log.warning("Не удалось проиграть звук: %s", e)
        try:
            run(["osascript", "-e", "on run argv", "-e", "display notification (item 1 of argv) with title "
                 "(item 2 of argv)", "-e", "end run", text, "Jarvis"], capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("Не удалось показать уведомление: %s", e)
        journal.record("(сработал " + KIND_NAMES[item["kind"]].lower() + ")",
                       {"name": item["kind"], "args": {"action": "fire", "label": item.get("label", "")}},
                       "SAFE", None, True, text)
        speak(text)
    return handle


def make_missed_handler(speak):
    def handle(items):
        text = missed_phrase(items)
        for item in items:
            journal.record("(пропущено за время простоя)",
                           {"name": item["kind"], "args": {"action": "missed", "label": item.get("label", "")}},
                           "SAFE", None, True, fire_text(item))
        speak(text)
    return handle


# --- Общий планировщик приложения ---

_default = None
_default_lock = threading.Lock()


def get_scheduler():
    """Планировщик приложения. Не настроенный main'ом — в памяти и без потока (тесты)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Scheduler()
        return _default


def set_scheduler(scheduler):
    global _default
    with _default_lock:
        _default = scheduler
    return scheduler
