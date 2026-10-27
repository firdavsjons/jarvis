"""История диалога в памяти: последние 6 сообщений и не больше ~2000 символов. На диск не пишется."""
import re
from threading import Lock

MAX_MESSAGES = 6        # 3 пары «пользователь — ответ»
MAX_CHARS = 2000
MAX_MESSAGE_CHARS = 600
MAX_TOOLS_LINE = 200

FORGET_REPLY = "Хорошо, разговор забыт."
_FORGET_OBJECTS = {"разговор", "всё", "все", "историю", "контекст", "диалог", "переписку"}


def is_forget_command(text):
    """«забудь разговор», «забудь всё», «очисти историю»."""
    words = set(re.findall(r"\w+", text.lower()))
    return bool({"забудь", "очисти", "сотри"} & words and _FORGET_OBJECTS & words) and len(words) <= 5


def tools_line(results):
    """Результаты инструментов одной строкой: «[open_app Safari: успешно; find_apps: не найдено]»."""
    parts = []
    for r in results:
        action = r.get("action") if isinstance(r.get("action"), dict) else {}
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        target = next((str(v) for v in args.values() if isinstance(v, (str, int))), "")
        outcome = "успешно" if r.get("ok") else f"ошибка: {r.get('short')}"
        parts.append(" ".join(p for p in [str(action.get("name", "?")), target] if p) + f": {outcome}")
    line = "; ".join(parts)
    if len(line) > MAX_TOOLS_LINE:
        line = line[:MAX_TOOLS_LINE - 1] + "…"
    return f"[{line}]" if line else ""


def _clip(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS - 1] + "…"


class ConversationHistory:
    def __init__(self, max_messages=MAX_MESSAGES, max_chars=MAX_CHARS):
        self.max_messages = max_messages
        self.max_chars = max_chars
        self._messages = []
        self._lock = Lock()

    def add_exchange(self, user_text, assistant_text, results=()):
        assistant = " ".join(p for p in [assistant_text, tools_line(results)] if p)
        with self._lock:
            self._messages += [{"role": "user", "content": _clip(user_text)},
                               {"role": "assistant", "content": _clip(assistant or "…")}]
            self._trim()

    def _trim(self):
        while len(self._messages) > self.max_messages:
            del self._messages[:2]
        while len(self._messages) > 2 and self.total_chars() > self.max_chars:
            del self._messages[:2]

    def total_chars(self):
        return sum(len(m["content"]) for m in self._messages)

    def messages(self):
        with self._lock:
            return [dict(m) for m in self._messages]

    def clear(self):
        with self._lock:
            self._messages = []
