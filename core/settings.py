"""Настройки из файла .env проекта (не из окружения терминала)."""
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def env_value(name, default=None, path=None):
    value = dotenv_values(path or ENV_FILE).get(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def env_float(name, default, minimum, maximum, path=None):
    """Число из .env в пределах [minimum, maximum]; не число — default."""
    try:
        value = float(env_value(name, default, path))
    except (TypeError, ValueError):
        return float(default)
    return max(minimum, min(maximum, value))
