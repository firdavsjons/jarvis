"""Подсказка для Whisper (initial_prompt): образец того, как выглядят команды. Только для локального Whisper.

Whisper продолжает подсказку как предыдущий текст, поэтому метки и списки («Контакты: мама, босс») он дописывал к
фразам. Теперь подсказка — несколько обычных коротких командных предложений, собранных из шаблонов:

    Джарвис, открой калькулятор. Джарвис, закрой сафари. Напиши маме в телеграм привет. Поставь таймер на пять минут.
    Какая погода в Душанбе?

Глаголы после «Джарвис» («открой» и «закрой») чередуются при ротации: Whisper слышал их искажённо («открою», «крой»).

- русские названия установленных приложений (ключи APP_ALIASES и «произносимые добавки»), не больше 3 на подсказку,
  в винительном падеже после «открой» и «закрой»;
- ключи contacts.json (только ключи: без username и телефонов), не больше 2, в дательном падеже;
- город — DEFAULT_CITY из .env (по умолчанию Душанбе), в предложном падеже.
Английских названий и псевдонимов из памяти в подсказке нет: они провоцируют эхо.

Размер — не больше 80 токенов (оценка 1 токен ≈ 2 символа). Набор приложений и контактов меняется по кругу при каждом
построении (раз в 10 минут, REFRESH_SECONDS). В Google подсказка не уходит никогда (SpeechToText передаёт её только
движкам с uses_prompt=True), в лог пишется только длина.
"""
import math
import re
import time

from core import contacts, installed_apps
from core.apps import APP_ALIASES, SPOKEN_EXTRAS
from core.settings import env_value

PROMPT_TOKEN_LIMIT = 80
CHARS_PER_TOKEN = 2.0      # консервативная оценка для кириллицы: токенов не больше, чем получится по ней
APPS_PER_PROMPT = 3
CONTACTS_PER_PROMPT = 2
REFRESH_SECONDS = 600
BASE = "Джарвис."
DEFAULT_CITY = "Душанбе"   # как core.web.DEFAULT_CITY (web здесь не импортируем: тянет httpx)
# «Произносимые добавки» (SPOKEN_EXTRAS) живут в core/apps: их знает и разбор фраз-перечислений. Попадают в подсказку,
# лишь если приложение установлено


def estimate_tokens(text):
    return math.ceil(len(text) / CHARS_PER_TOKEN)


# --- падежи (простые правила; незнакомая форма остаётся как есть) ----------------------------------------------------
def _cyrillic_word(text):
    return bool(re.fullmatch(r"[а-яё]{3,}", text.strip().lower()))


def accusative(name):
    """«почта» -> «почту», «музыка» -> «музыку»; «калькулятор», «сафари», «заметки» не меняются."""
    word = name.strip()
    if _cyrillic_word(word) and word.lower()[-1] in "ая":
        return word[:-1] + ("у" if word.lower()[-1] == "а" else "ю")
    return word


def dative(key):
    """Ключ контакта для «Напиши … в телеграм»: «мама» -> «маме», «Аня» -> «Ане», «Иван» -> «Ивану»; «себе»,
    несколько слов и латиница остаются как есть."""
    word = key.strip()
    if not _cyrillic_word(word):
        return word
    low = word.lower()
    if low == "себе":
        return word
    if low.endswith("ия"):
        return word[:-1] + "и"
    if low[-1] in "ая":
        return word[:-1] + "е"
    if low[-1] in "йь":
        return word[:-1] + "ю"
    if low[-1] in "еоиуыэю":
        return word
    return word + "у"


def prepositional(city):
    """«Душанбе» -> «Душанбе», «Москва» -> «Москве», «Лондон» -> «Лондоне». Несколько слов и латиница — None."""
    word = city.strip()
    if not _cyrillic_word(word):
        return None
    low = word.lower()
    if low.endswith("ия"):
        return word[:-1] + "и"
    if low[-1] in "ая":
        return word[:-1] + "е"
    if low[-1] == "ь":
        return word[:-1] + "и"
    if low[-1] in "еоиуыэюй":
        return word
    return word + "е"


def weather_sentence():
    city = prepositional(env_value("DEFAULT_CITY", DEFAULT_CITY) or DEFAULT_CITY)
    return f"Какая погода в {city}?" if city else "Какая сегодня погода?"


# --- источники слов ----------------------------------------------------------------------------------------------------
def russian_app_names(installed):
    """Русские произносимые названия установленных приложений: ключи APP_ALIASES, затем добавки. Одно приложение —
    одно название («файндер» и «финдер» не занимают два места)."""
    present = {name.casefold() for name in installed}
    names, seen = [], set()
    for spoken, app in [*APP_ALIASES.items(), *SPOKEN_EXTRAS]:
        if not re.search(r"[а-яё]", spoken) or app.casefold() not in present or app.casefold() in seen:
            continue
        seen.add(app.casefold())
        names.append(spoken)
    return names


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


def rotate(items, count, rotation):
    """count элементов по кругу, начиная с rotation * count."""
    if not items:
        return []
    start = (rotation * count) % len(items)
    return [items[(start + i) % len(items)] for i in range(min(count, len(items)))]


# --- сборка ------------------------------------------------------------------------------------------------------------
def build_prompt(rotation=0):
    apps = [accusative(a) for a in rotate(russian_app_names(installed_apps.list_installed_apps()), APPS_PER_PROMPT,
                                          rotation)]
    people = [dative(c) for c in rotate(contact_names(), CONTACTS_PER_PROMPT, rotation)]
    app = lambda i: apps[i % len(apps)] if apps else None      # noqa: E731 — одно приложение на все места, если оно одно
    first, second = ("открой", "закрой") if rotation % 2 == 0 else ("закрой", "открой")   # чередуются при ротации
    sentences = [f"Джарвис, {first} {app(0)}." if apps else BASE,
                 f"Джарвис, {second} {app(1)}." if apps else None,
                 f"Напиши {people[0]} в телеграм привет." if people else None,
                 "Поставь таймер на пять минут.",
                 weather_sentence(),
                 f"Открой {app(2)}." if len(apps) > 2 else None,
                 f"Напиши {people[1]} в телеграм привет." if len(people) > 1 else None]
    text = ""
    for sentence in filter(None, sentences):
        candidate = f"{text} {sentence}".strip()
        if estimate_tokens(candidate) <= PROMPT_TOKEN_LIMIT:
            text = candidate
    return text


class PromptProvider:
    """Подсказка с кэшем на REFRESH_SECONDS; при каждом построении набор приложений и контактов сдвигается по кругу."""

    def __init__(self, build=build_prompt, clock=time.monotonic):
        self._build, self._clock = build, clock
        self._text, self._built_at, self._rotation = "", None, 0

    def get(self):
        now = self._clock()
        if self._built_at is None or now - self._built_at >= REFRESH_SECONDS:
            self._text, self._built_at = self._build(self._rotation), now
            self._rotation += 1
        return self._text
