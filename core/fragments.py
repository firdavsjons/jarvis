"""Обрывки речи: «то», «скрыть бо» — модель не спрашиваем, просим повторить.

Распознавание речи иногда отдаёт огрызок фразы; запрос к облачной модели на него — пустая трата лимита.
Правило узкое, чтобы не отсекать настоящие вопросы: только фразы из 1–2 слов, а три слова и больше всегда
идут модели. Ответы и управление («да», «стоп», «не надо») и известные команды обрывками не считаются.
"""
import re

from core import installed_apps

FRAGMENT_REPLY = "Не расслышал. Повторите, пожалуйста."
NOTHING_TO_CONFIRM = "Сейчас нечего подтверждать."

# Слова подтверждения и отмены (main.parse_confirmation). Фраза только из них без ожидающего действия — не к модели
CONFIRM_WORDS = {"да", "подтверждаю", "выполняй"}
CANCEL_WORDS = {"нет", "отмена"}
BARE_WORDS = CONFIRM_WORDS | CANCEL_WORDS

MIN_WORD = 4         # одно слово короче — обрывок, если оно не из известных
CUT_OFF_LETTERS = 2  # слово в 1–2 буквы в двухсловной фразе — обрезок, если оно не из обычных коротких слов

# Ответы и управление: никогда не обрывки
CONTROL_WORDS = {"да", "нет", "стоп", "хватит", "ок", "окей", "отмена", "надо", "ладно", "пока", "привет"}
# Обычные короткие слова: «не надо», «на паузу», «в загрузки»
SHORT_WORDS = {"я", "ты", "он", "мы", "вы", "на", "в", "во", "с", "со", "и", "а", "но", "не", "ни", "по", "до", "из",
               "за", "от", "об", "ко", "ли", "же", "бы", "уж", "ну", "да", "ок", "ага", "угу"}
# Слова команд и тем, которые короче четырёх букв
KNOWN_SHORT = {"ip", "cpu", "ram", "wifi", "вай", "фай", "тему", "тема", "стоп", "пауза", "трек", "мне"}


def is_bare_confirmation(text):
    """Фраза состоит только из слов подтверждения или отмены («да», «нет, отмена»)."""
    words = re.findall(r"\w+", str(text).lower())
    return bool(words) and all(w in BARE_WORDS for w in words)


def asks_question(speech):
    """Озвученный ответ заканчивается вопросом (знак «?» в конце, возможно перед кавычкой или скобкой)."""
    return bool(re.search(r"\?[\s\"»”')]*$", str(speech or "")))


def is_fragment(text):
    words = re.findall(r"\w+", str(text).lower())
    if not words or len(words) > 2:
        return False
    if any(w.isdigit() or w in CONTROL_WORDS for w in words):
        return False
    known = None
    if len(words) == 1:
        word = words[0]
        if len(word) >= MIN_WORD or word in SHORT_WORDS | KNOWN_SHORT:
            return False
        known = installed_apps.known_app_words()
        return word not in known
    # Два слова: обрезок вроде «скрыть бо» — слово в 1–2 буквы, которого нет среди обычных коротких слов
    cut = [w for w in words if len(w) <= CUT_OFF_LETTERS and w not in SHORT_WORDS]
    if not cut:
        return False
    known = installed_apps.known_app_words()
    return not all(w in known for w in cut)
