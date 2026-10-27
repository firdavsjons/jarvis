"""Данные пользователя вне проекта: ~/Library/Application Support/Jarvis (папка 700, файлы 600)."""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path.home() / "Library" / "Application Support" / "Jarvis"


def ensure_private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write_json(path, data, mode=0o600):
    """Временный файл с нужными правами -> os.replace: файл не бывает наполовину записанным."""
    path = Path(path)
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, mode)  # если tmp уже существовал с другими правами
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("Не удалось прочитать %s: %s", path, e)
        return default
