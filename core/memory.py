"""Память пользователя: ~/Library/Application Support/Jarvis/memory.json (права 600, вне проекта).

Записывать — только по прямой просьбе пользователя («запомни», «запиши», «учти»): проверка идёт по реальной
фразе пользователя (core/ai.py, run_actions), а не по аргументам модели.
"""
import re
from threading import RLock

from core.installed_apps import alias_key, resolve_app_name
from core.storage import DATA_DIR, atomic_write_json, read_json

MEMORY_FILE = DATA_DIR / "memory.json"
DEFAULT_ASSISTANT_NAME = "Jarvis"

MAX_FACTS = 100
MAX_FACT_CHARS = 200
MAX_ALIAS_CHARS = 50
MAX_NAME_CHARS = 30
RECENT_FACTS_IN_PROMPT = 10
MAX_BLOCK_CHARS = 600
SPOKEN_FACTS = 5

TRIGGER_WORDS = {"запомни", "запомните", "запиши", "запишите", "учти", "учтите"}
SECRET_REFUSAL = "Такое я запоминать не буду."
NAME_REFUSAL = "Такое имя я использовать не буду."

_SECRET_WORD_RE = re.compile(
    r"(?<!\w)(парол\w*|password\w*|passwd|токен\w*|token\w*|api\w*|ключ\w*|секрет\w*|secret\w*"
    r"|cvv|cvc|пин|пин-код\w*|pin|паспорт\w*)(?!\w)", re.IGNORECASE)
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){15}\d(?!\d)")       # 16 цифр, можно с пробелами/дефисами
_PASSPORT_RE = re.compile(r"(?<!\d)\d{2} ?\d{2} \d{6}(?!\d)")  # серия и номер: 45 10 123456
_USER_NAME_RE = re.compile(r"^(?:меня зовут|моё имя|мое имя)\s+(.+)$", re.IGNORECASE | re.DOTALL)
_ASSISTANT_NAME_RE = re.compile(r"^(?:тебя зовут|твоё имя|твое имя|называй себя|зови себя)\s+(.+)$",
                                re.IGNORECASE | re.DOTALL)
_NAME_EDGE = " .!«»\"'"


class MemoryRefused(Exception):
    """Запись в память отклонена. short — причина для модели и окна, reply — что сказать вслух."""

    def __init__(self, short, reply=None):
        super().__init__(short)
        self.short = short
        self.reply = reply


def asked_to_remember(user_phrase):
    """Пользователь прямо попросил запомнить: «запомни», «запиши», «учти» в ЕГО фразе."""
    return bool(set(re.findall(r"\w+", (user_phrase or "").lower())) & TRIGGER_WORDS)


def contains_secret(text):
    text = str(text)
    return bool(_SECRET_WORD_RE.search(text) or _CARD_RE.search(text) or _PASSPORT_RE.search(text))


def valid_assistant_name(name):
    """Только буквы, цифры, пробел и дефис, 1–30 символов: имя попадает в системный промпт."""
    return (isinstance(name, str) and 0 < len(name) <= MAX_NAME_CHARS and name == name.strip()
            and all(c.isalnum() or c in " -" for c in name))


def is_clear_memory_command(text):
    """«забудь всё о себе», «забудь всё обо мне», «очисти память»."""
    words = set(re.findall(r"\w+", text.lower()))
    about_me = {"забудь", "сотри", "удали"} & words and {"всё", "все"} & words and {"себе", "мне", "меня", "обо"} & words
    clear_memory = {"очисти", "сотри"} & words and "память" in words
    return bool(about_me or clear_memory) and len(words) <= 6


class UserMemory:
    def __init__(self, path=None):
        """path=None — только в памяти (тесты); в приложении — MEMORY_FILE."""
        self.path = path
        self._lock = RLock()  # RLock: _save() под блокировкой вызывает on_change, который читает aliases
        self.on_change = None  # callback(aliases_changed: bool) — AIClient: псевдонимы и кэш
        self._data = self._normalized(read_json(path, {}) if path else {})

    @staticmethod
    def _normalized(data):
        data = data if isinstance(data, dict) else {}
        facts = [str(f)[:MAX_FACT_CHARS] for f in data.get("facts", []) if isinstance(f, str) and f.strip()]
        aliases = {alias_key(k): str(v) for k, v in (data.get("app_aliases") or {}).items()
                   if isinstance(v, str) and alias_key(k)} if isinstance(data.get("app_aliases"), dict) else {}
        name = data.get("assistant_name")
        return {"facts": facts[-MAX_FACTS:], "app_aliases": aliases,
                "assistant_name": name if valid_assistant_name(name) else DEFAULT_ASSISTANT_NAME}

    # --- Чтение ---

    @property
    def facts(self):
        with self._lock:
            return list(self._data["facts"])

    @property
    def aliases(self):
        with self._lock:
            return dict(self._data["app_aliases"])

    @property
    def assistant_name(self):
        with self._lock:
            return self._data["assistant_name"]

    def prompt_block(self):
        """«Что известно о пользователе: …» — до 10 свежих фактов и все псевдонимы, не больше 600 символов."""
        with self._lock:
            facts = self._data["facts"][-RECENT_FACTS_IN_PROMPT:]
            aliases = "; ".join(f"«{k}» — {v}" for k, v in self._data["app_aliases"].items())
            name = self._data["assistant_name"]
        extra = []
        if aliases:
            extra.append(f"Псевдонимы приложений: {aliases}.")
        if name != DEFAULT_ASSISTANT_NAME:
            extra.append(f"Имя ассистента: {name}.")

        def build(fs):
            parts = ([f"{'; '.join(fs)}."] if fs else []) + extra
            return f"Что известно о пользователе: {' '.join(parts)}" if parts else ""

        block = build(facts)
        while len(block) > MAX_BLOCK_CHARS and facts:
            facts = facts[1:]  # сначала уходят самые старые факты
            block = build(facts)
        if len(block) > MAX_BLOCK_CHARS:
            block = block[:MAX_BLOCK_CHARS - 1] + "…"
        return block

    # --- Запись ---

    def _save(self, aliases_changed=False):
        if self.path:
            atomic_write_json(self.path, self._data)
        if self.on_change:
            self.on_change(aliases_changed)

    def remember(self, text):
        """-> (сообщение для окна, фраза для голоса). Отказ — MemoryRefused."""
        raw = str(text or "").strip()
        if not raw:
            raise MemoryRefused("нечего запоминать")
        if contains_secret(raw):
            raise MemoryRefused("такое я запоминать не буду", SECRET_REFUSAL)

        assistant = _ASSISTANT_NAME_RE.match(raw)
        if assistant:
            name = assistant.group(1).strip(_NAME_EDGE)
            if not valid_assistant_name(name):  # перевод строки, «.», инструкции внутри — нет
                raise MemoryRefused("такое имя я использовать не буду", NAME_REFUSAL)
            with self._lock:
                self._data["assistant_name"] = name
                self._save()
            return f"Имя ассистента: {name}", f"Хорошо, теперь меня зовут {name}."

        fact = " ".join(raw.split())
        user = _USER_NAME_RE.match(fact)
        if user:
            fact = f"Пользователя зовут {user.group(1).strip(_NAME_EDGE)}"
        if len(fact) > MAX_FACT_CHARS:
            raise MemoryRefused(f"слишком длинно, не больше {MAX_FACT_CHARS} символов")
        with self._lock:
            facts = self._data["facts"]
            if user:
                facts[:] = [f for f in facts if not f.startswith("Пользователя зовут ")]
            if any(f.casefold() == fact.casefold() for f in facts):
                return f"Уже помню: {fact}", "Я это уже помню."
            facts.append(fact)
            del facts[:-MAX_FACTS]  # 101-й факт вытесняет самый старый
            self._save()
        return f"Запомнил: {fact}", "Запомнил."

    def remember_alias(self, spoken, app):
        key = alias_key(spoken)
        if not key or len(key) > MAX_ALIAS_CHARS:
            raise MemoryRefused("неверный псевдоним")
        if contains_secret(spoken) or contains_secret(app):
            raise MemoryRefused("такое я запоминать не буду", SECRET_REFUSAL)
        target = resolve_app_name(app, use_user_aliases=False) if isinstance(app, str) else None
        if target is None:
            raise MemoryRefused(f"приложение {app} не найдено")
        with self._lock:
            self._data["app_aliases"][key] = target
            self._save(aliases_changed=True)
        return f"Псевдоним: «{key}» → {target}", f"Запомнил: {key} — это {target}."

    def list_memory(self):
        facts, aliases, name = self.facts, self.aliases, self.assistant_name
        if not facts and not aliases and name == DEFAULT_ASSISTANT_NAME:
            return "Память пуста", "Я пока ничего о вас не помню."
        lines = [f"Факты: {'; '.join(facts)}" if facts else "",
                 "Псевдонимы: " + "; ".join(f"«{k}» — {v}" for k, v in aliases.items()) if aliases else "",
                 f"Имя ассистента: {name}" if name != DEFAULT_ASSISTANT_NAME else ""]
        spoken = []
        if facts:
            spoken.append("Я помню: " + "; ".join(facts[-SPOKEN_FACTS:]) + ".")
        if aliases:
            spoken.append(f"Псевдонимов приложений: {len(aliases)}.")
        if name != DEFAULT_ASSISTANT_NAME:
            spoken.append(f"Вы зовёте меня {name}.")
        return "\n".join(l for l in lines if l), " ".join(spoken)

    def forget(self, query):
        """Удаляет всё, что содержит query (без учёта регистра): факты, псевдонимы, имя ассистента."""
        q = str(query or "").strip().casefold()
        if not q:
            raise MemoryRefused("не указано, что забыть")
        removed, aliases_changed = [], False
        with self._lock:
            keep = []
            for fact in self._data["facts"]:
                (removed if q in fact.casefold() else keep).append(fact)
            self._data["facts"] = keep
            for key, app in list(self._data["app_aliases"].items()):
                if q in key.casefold() or q in app.casefold():
                    removed.append(f"псевдоним «{key}» — {app}")
                    del self._data["app_aliases"][key]
                    aliases_changed = True
            if self._data["assistant_name"] != DEFAULT_ASSISTANT_NAME and q in self._data["assistant_name"].casefold():
                removed.append(f"имя ассистента {self._data['assistant_name']}")
                self._data["assistant_name"] = DEFAULT_ASSISTANT_NAME
            if removed:
                self._save(aliases_changed)
        if not removed:
            raise MemoryRefused("не нашёл")  # «в памяти нет» — неудача, не успех
        return f"Забыто: {'; '.join(removed)}", f"Забыл: {'; '.join(removed[:3])}."

    def clear(self):
        with self._lock:
            self._data = self._normalized({})
            self._save(aliases_changed=True)
        return "Память очищена", "Готово, я всё забыл о вас."
