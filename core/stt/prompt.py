"""Подсказка для Whisper (initial_prompt): слова, которые он должен писать правильно. Только для локального Whisper.

Порядок групп (что не помещается в 200 токенов, отбрасывается с конца): «Джарвис»; русские названия установленных
приложений (ключи APP_ALIASES и «произносимые добавки» ниже, до 15) — как их говорят вслух: «калькулятор», «сафари»;
глаголы команд; имена-ключи из contacts.json (без username и телефонов); псевдонимы приложений из памяти; английские
названия установленных приложений (до 15). Обновляется раз в 10 минут. В Google подсказка не уходит никогда
(SpeechToText передаёт её только движкам с uses_prompt=True), в лог пишется только длина.
"""
import math
import re
import time

from core import contacts, installed_apps
from core.apps import APP_ALIASES

PROMPT_TOKEN_LIMIT = 200
CHARS_PER_TOKEN = 2.0      # консервативная оценка для кириллицы: токенов не больше, чем получится по ней
MAX_APPS = 15
REFRESH_SECONDS = 600
BASE = "Джарвис."
COMMAND_VERBS = ("открой", "закрой", "включи", "выключи", "поставь", "напиши", "найди", "прочитай")
# Как говорят вслух приложения, которых нет в APP_ALIASES: только для подсказки (открытие приложений они не меняют).
# Попадают в подсказку, лишь если приложение установлено
SPOKEN_EXTRAS = (("ватсап", "WhatsApp"), ("вотсап", "WhatsApp"), ("ютуб", "YouTube"), ("хром", "Google Chrome"),
                 ("вайбер", "Viber"), ("зум", "zoom.us"), ("слак", "Slack"), ("спотифай", "Spotify"))


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


def russian_app_names(installed):
    """Русские произносимые названия установленных приложений: ключи APP_ALIASES, затем добавки; до MAX_APPS."""
    present = {name.casefold() for name in installed}
    names = []
    for spoken, app in [*APP_ALIASES.items(), *SPOKEN_EXTRAS]:
        if not re.search(r"[а-яё]", spoken) or app.casefold() not in present or spoken in names:
            continue
        names.append(spoken)
    return names[:MAX_APPS]


def build_prompt():
    installed = installed_apps.list_installed_apps()
    groups = (("Приложения", russian_app_names(installed)),
              ("Команды", list(COMMAND_VERBS)),
              ("Контакты", contact_names()),
              ("Как я называю приложения", list(installed_apps.user_aliases())),
              ("Программы", installed_apps.popular_first(installed, MAX_APPS)))
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
