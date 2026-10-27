import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

LOG_DIR = Path.home() / "Library" / "Logs" / "Jarvis"
LOG_FILE = LOG_DIR / "jarvis.log"

QUIET_LOGGERS = ("primp", "httpx", "httpcore")
MIN_SECRET_LENGTH = 8  # короче — не секрет, а риск заменить обычные слова

_secrets = set()
_secrets_lock = Lock()


def register_secret(value):
    """Запомнить значение (ключ API), которое нельзя показывать ни в логе, ни в выводе."""
    if isinstance(value, str) and len(value.strip()) >= MIN_SECRET_LENGTH:
        with _secrets_lock:
            _secrets.add(value.strip())


def redact_secrets(text):
    """Заменяет зарегистрированные секреты на ***."""
    if not isinstance(text, str):
        return text
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


class SecretFilter(logging.Filter):
    """Убирает секреты из сообщения и трассировки исключения до того, как запись попадёт в лог."""

    def filter(self, record):
        message = record.getMessage()
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info:
            record.exc_text = redact_secrets(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


def setup_logging(level=logging.INFO):
    """Лог в консоль и в ~/Library/Logs/Jarvis/jarvis.log (1 МБ × 3 файла), без секретов."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    for handler in (console, file_handler):
        handler.addFilter(SecretFilter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [console, file_handler]
    # Шумные INFO-логи HTTP-клиентов: строка на каждый запрос (в том числе к LLM)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return LOG_FILE
