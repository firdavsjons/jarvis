"""Счётчик запросов к провайдерам за сутки: в памяти и (в приложении) в ~/Library/Logs/Jarvis/llm_usage.json."""
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SWITCH_SHARE = 0.9  # на 90% дневного лимита переходим к следующему провайдеру заранее
LOG_EVERY = 10      # раз в столько запросов — строка «запросы сегодня: …» в лог
TPM_WINDOW = 60.0   # скользящее окно учёта токенов, секунд
CHARS_PER_TOKEN = 3.5


def estimate_tokens(messages, tools=None, max_tokens=None):
    """Оценка токенов запроса: символы / 3,5 + max_tokens ответа, если задан. Точный подсчёт без токенайзера
    невозможен; оценка с запасом нужна только чтобы заранее не упереться в лимит токенов в минуту."""
    chars = len(json.dumps(messages, ensure_ascii=False, default=str))
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False, default=str))
    return int(chars / CHARS_PER_TOKEN) + (int(max_tokens) if max_tokens else 0)


class UsageTracker:
    def __init__(self, limits, timezones, path=None, now=None, tpm_limits=None, clock=time.monotonic):
        """limits: {провайдер: лимит или None}; timezones: {провайдер: "America/Los_Angeles"};
        path: файл счётчика (None — только в памяти, так в тестах); now: часы для тестов;
        tpm_limits: {провайдер: токенов в минуту или None}; clock: монотонные часы окна токенов (для тестов)."""
        self.limits = dict(limits)
        self.timezones = dict(timezones)
        self.tpm_limits = dict(tpm_limits or {})
        self._clock = clock
        self._tokens = {}  # провайдер -> deque[(момент, оценка токенов)] за последние TPM_WINDOW секунд
        self.path = path
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._warned = set()  # (провайдер, дата) — предупреждение раз в сутки
        self._session_requests = 0
        self._data = self._load()

    def _today(self, name):
        return self._now().astimezone(ZoneInfo(self.timezones.get(name, "UTC"))).date().isoformat()

    def _entry(self, name):
        today = self._today(name)
        entry = self._data.get(name)
        if not isinstance(entry, dict) or entry.get("date") != today:
            entry = {"date": today, "count": 0}
            self._data[name] = entry
        return entry

    def count(self, name):
        with self._lock:
            return self._entry(name)["count"]

    def near_limit(self, name):
        """True — провайдер почти исчерпал дневной лимит, его нужно пропустить."""
        limit = self.limits.get(name)
        if not limit:
            return False
        with self._lock:
            entry = self._entry(name)
            if entry["count"] < SWITCH_SHARE * limit:
                return False
            key = (name, entry["date"])
            if key not in self._warned:
                self._warned.add(key)
                log.warning("LLM %s: %s из %s запросов за сутки (≥%d%%), переключаюсь на следующего провайдера",
                            name, entry["count"], limit, int(SWITCH_SHARE * 100))
            return True

    def _window(self, name):
        """Записи за последнюю минуту (устаревшие выбрасываются). Вызывать под замком."""
        window = self._tokens.setdefault(name, deque())
        horizon = self._clock() - TPM_WINDOW
        while window and window[0][0] <= horizon:
            window.popleft()
        return window

    def tokens_last_minute(self, name):
        with self._lock:
            return sum(t for _, t in self._window(name))

    def tpm_exceeded(self, name, tokens):
        """True — этот запрос выведет провайдера за 90% лимита токенов в минуту. Пустое окно пропускаем:
        ожидание ничего не изменит, а слишком большой запрос провайдер сам отклонит."""
        limit = self.tpm_limits.get(name)
        if not limit:
            return False
        with self._lock:
            window = self._window(name)
            used = sum(t for _, t in window)
            return bool(window) and used + tokens > SWITCH_SHARE * limit

    def record_tokens(self, name, tokens):
        if not self.tpm_limits.get(name):
            return
        with self._lock:
            self._window(name).append((self._clock(), tokens))

    def record(self, name):
        with self._lock:
            self._entry(name)["count"] += 1
            self._save()
            self._session_requests += 1
            if self._session_requests % LOG_EVERY == 0:
                names = list(self.limits) or list(self._data)
                log.info("запросы сегодня: %s", ", ".join(f"{n} {self._entry(n)['count']}" for n in names))

    def _load(self):
        if not self.path:
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("Не удалось прочитать счётчик LLM %s: %s", self.path, e)
            return {}

    def _save(self):
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Не удалось записать счётчик LLM %s: %s", self.path, e)
