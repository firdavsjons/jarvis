"""Журнал действий: ~/Library/Logs/Jarvis/actions.jsonl, одна строка JSON на действие."""
import json
import logging
import re
from datetime import datetime
from threading import Lock
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from core.logger import LOG_DIR

log = logging.getLogger(__name__)

JOURNAL_FILE = None  # включается setup_journal() при запуске приложения; в тестах выключен
_lock = Lock()

_SECRET_WORD = r"(?:пароль|password|passwd|pwd|token|токен|api[_-]?key|apikey|secret|секрет|ключ)"
_SECRET_RE = re.compile(rf"(?i)\b({_SECRET_WORD})(\s*[:=]\s*|\s+)([^\s&,;]+)")
_SECRET_PARAM_RE = re.compile(r"(?i)token|key|pass|secret|auth|sig")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def setup_journal(path=None):
    global JOURNAL_FILE
    JOURNAL_FILE = path or LOG_DIR / "actions.jsonl"
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    return JOURNAL_FILE


def _redact_url(match):
    url = match.group(0)
    try:
        parsed = urlparse(url)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
    except ValueError:
        return "***"
    query = urlencode([(k, "***" if _SECRET_PARAM_RE.search(k) else v)
                       for k, v in parse_qsl(parsed.query, keep_blank_values=True)], safe="*")
    return urlunparse(parsed._replace(netloc=netloc, query=query))


def redact(value):
    """Скрывает пароли и токены: «пароль 1234» -> «пароль ***», user:pass@ и ?token=... в ссылках."""
    if isinstance(value, str):
        value = _URL_RE.sub(_redact_url, value)
        return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def record(phrase, action, level, confirmed, ok, result):
    """confirmed: True/False — ответ пользователя, None — подтверждение не требовалось."""
    if JOURNAL_FILE is None:
        return
    entry = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "phrase": redact(phrase or ""),
        "action": redact(action),
        "level": level,
        "confirmed": confirmed,
        "ok": ok,
        "result": redact(str(result)),
    }
    try:
        with _lock, open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        log.warning("Не удалось записать журнал действий: %s", e)
