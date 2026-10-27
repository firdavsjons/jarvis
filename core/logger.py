import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / "Library" / "Logs" / "Jarvis"
LOG_FILE = LOG_DIR / "jarvis.log"


def setup_logging(level=logging.INFO):
    """Лог в консоль и в ~/Library/Logs/Jarvis/jarvis.log (1 МБ × 3 файла)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [console, file_handler]
    return LOG_FILE
