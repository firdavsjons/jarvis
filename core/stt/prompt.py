"""Подсказка для Whisper (initial_prompt): слова, которые он должен писать правильно. Только для локального Whisper.

Состав: «Джарвис», до 15 частых установленных приложений, имена-ключи из contacts.json (без username и телефонов),
псевдонимы приложений из памяти. До 200 токенов; обновляется раз в 10 минут. В Google подсказка не уходит никогда
(SpeechToText передаёт её только движкам с uses_prompt=True), в лог пишется только длина.
"""
import math
import re
import time

from core import contacts, installed_apps

PROMPT_TOKEN_LIMIT = 200
CHARS_PER_TOKEN = 2.0      # консервативная оценка для кириллицы: токенов не больше, чем получится по ней
MAX_APPS = 15
REFRESH_SECONDS = 600
BASE = "Джарвис."


def estimate_tokens(text):
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def contact_names():
    """Ключи contacts.json («мама», «себе»): только имена. Ключ с «@» или длинной цепочкой цифр пропускается, чтобы
    username и телефон не попали в подсказку, даже если их вписали как имя. Нет файла — пусто (файл не создаётся)."""
    if not contacts.CONTACTS_FILE.exists():
        return []
    names = []
    for contact in contacts.load_contacts().values():
        key = contact.key.strip()
        if key and "@" not in key and not re.search(r"\d{5,}", key) and key not in names:
            names.append(key)
    return names


def build_prompt():
    apps = installed_apps.popular_first(installed_apps.list_installed_apps(), MAX_APPS)
    groups = (("Контакты", contact_names()),
              ("Как я называю приложения", list(installed_apps.user_aliases())),
              ("Приложения", apps))
    text = BASE
    for label, items in groups:
        line = ""
        for item in items:
            candidate = f"{line}, {item}" if line else item
            if estimate_tokens(f"{text} {label}: {candidate}.") > PROMPT_TOKEN_LIMIT:
                break
            line = candidate
        if line:
            text = f"{text} {label}: {line}."
    return text


class PromptProvider:
    """Подсказка с кэшем на REFRESH_SECONDS: список приложений и контактов меняется редко."""

    def __init__(self, build=build_prompt, clock=time.monotonic):
        self._build, self._clock = build, clock
        self._text, self._built_at = "", None

    def get(self):
        now = self._clock()
        if self._built_at is None or now - self._built_at >= REFRESH_SECONDS:
            self._text, self._built_at = self._build(), now
        return self._text
