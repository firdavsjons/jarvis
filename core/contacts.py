"""Контакты для сообщений: ~/Library/Application Support/Jarvis/contacts.json.

{"имя или псевдоним": {"username": "...", "phone": "...", "title": "как зовётся в Telegram"}}

Файл правит только пользователь, руками. Ни один инструмент, действие, быстрое правило или ответ модели в него не
пишут (единственное исключение — один раз создаётся пустой пример, если файла нет; проверяет test_registry.py).
Ключи, начинающиеся с «_», — примеры, они игнорируются.
"""
import difflib
import logging
import re
from pathlib import Path

from core.storage import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger(__name__)

CONTACTS_FILE = DATA_DIR / "contacts.json"
EXAMPLE = {"_пример": {"username": "durov", "phone": "", "title": "Павел Дуров"},
           "_себе": {"username": "ваш_username_без_@", "phone": "", "title": "Избранное"}}
FUZZY_CUTOFF = 0.85
STEM_MIN_LENGTH = 4    # от скольких букв имя склоняется по основе (мама → маме, мамой)
MAX_ENDING = 3         # длина окончания после основы

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")   # username Telegram: 5–32 символа
_PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")             # с кодом страны: 8–15 цифр


class Contact:
    def __init__(self, key, username, phone, title):
        self.key, self.username, self.phone, self.title = key, username, phone, title

    def __repr__(self):  # без телефона в логах
        return f"<Contact {self.key}>"


def _words(text):
    return re.findall(r"\w+", str(text).lower().replace("ё", "е"))


def load_contacts(path=None):
    """{ключ в нижнем регистре: Contact}. Нет файла — создаётся пример (единственная запись Jarvis в него)."""
    path = Path(path or CONTACTS_FILE)
    if not path.exists():
        try:
            atomic_write_json(path, EXAMPLE)
        except OSError as e:
            log.warning("Не удалось создать %s: %s", path, e)
        return {}
    data = read_json(path, None)
    if not isinstance(data, dict):
        log.warning("%s: ожидался словарь, контактов нет", path)
        return {}
    contacts = {}
    for key, entry in data.items():
        if not isinstance(key, str) or not key.strip() or key.startswith("_") or not isinstance(entry, dict):
            continue
        username = str(entry.get("username") or "").strip().lstrip("@")
        phone = re.sub(r"[\s\-()]", "", str(entry.get("phone") or ""))
        if username and not _USERNAME_RE.match(username):
            log.warning("Контакт %r: username не подходит, пропущен", key)
            continue
        if phone and not _PHONE_RE.match(phone):
            log.warning("Контакт %r: телефон не подходит, пропущен", key)
            continue
        if not username and not phone:
            continue
        title = str(entry.get("title") or "").strip() or key.strip()
        contacts[" ".join(_words(key))] = Contact(key.strip(), username, phone.lstrip("+"), title)
    return contacts


def word_matches(word, key_word):
    """Слово фразы — это слово ключа, возможно в другом падеже: основа — ключ без последней буквы."""
    if word == key_word:
        return True
    if len(key_word) < STEM_MIN_LENGTH:
        return False
    stem = key_word[:-1]
    return word.startswith(stem) and len(word) - len(stem) <= MAX_ENDING


def matches_phrase(key, phrase):
    """Ключ (одно или несколько слов) встречается во фразе: слова подряд, с падежами."""
    key_words, words = _words(key), _words(phrase)
    n = len(key_words)
    return bool(key_words) and any(all(word_matches(words[i + j], key_words[j]) for j in range(n))
                                   for i in range(len(words) - n + 1))


def find(name, contacts):
    """Кандидаты для имени, как его слышит модель: точное → падежи → нечётко (≥0.85). Список Contact."""
    spoken = " ".join(_words(name))
    if not spoken:
        return []
    if spoken in contacts:
        return [contacts[spoken]]
    # «Анне» → «анна»; «Иван» → «Иван Иванов» и «Иван Петров» (имя — часть ключа: несколько кандидатов — переспросить)
    by_case = [c for key, c in contacts.items() if matches_phrase(key, spoken) or matches_phrase(spoken, key)]
    if by_case:
        return by_case
    scored = sorted(((difflib.SequenceMatcher(None, spoken, key).ratio(), c) for key, c in contacts.items()),
                    key=lambda x: -x[0])
    return [c for ratio, c in scored if ratio >= FUZZY_CUTOFF]
