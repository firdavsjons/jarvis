"""Перечисления команд во фразе: «закрой календарь, чатгпт и настройки». Нужно и ответу ai («Остальное я не сделал»),
и кэшу команд (фраза с невыполненной частью не должна закрепиться как короткая команда)."""
import re

LIST_VERBS = {"открой", "открыть", "откройте", "закрой", "закрыть", "закройте", "запусти", "запустить", "выйди",
              "выйти"}
LIST_FILLERS = {"приложение", "приложении", "приложения", "приложений", "программу", "программа", "программы",
                "пожалуйста", "мне", "все", "всё", "ещё", "еще", "тоже", "также", "потом", "и"}
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|\bа также\b|\bи\b)\s*", re.IGNORECASE)


def requested_apps(phrase):
    """Что перечислено в «закрой календарь, чатгпт и настройки»: сегменты после глагола (≥2), иначе []."""
    words = re.findall(r"[\w\-]+|[,;]", str(phrase).lower())
    start = next((i for i, w in enumerate(words) if w in LIST_VERBS), None)
    if start is None:
        return []
    text = " ".join(words[start + 1:]).replace(" ,", ",").replace(" ;", ";")
    segments = []
    for part in _LIST_SPLIT_RE.split(text):
        kept = [w for w in re.findall(r"[\w\-]+", part) if w not in LIST_FILLERS and w not in LIST_VERBS]
        if kept:
            segments.append(" ".join(kept))
    return segments if len(segments) >= 2 else []
