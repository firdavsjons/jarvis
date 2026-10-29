"""Повторы Whisper: «Открой калькулятор. Открой калькулятор.» — модель зацикливается и дублирует сказанное.

Схлопываются только повторы предложений и фраз из двух и более слов:
- весь текст — одно и то же предложение (из 2+ слов), повторённое 2+ раза подряд → одно;
- одна и та же фраза из 2+ слов повторена 3+ раза подряд (где угодно в тексте) → одна.
Повторы ОДНОГО слова («громче громче громче», «стоп стоп») не трогаются: пользователь повторяет их нарочно.
Сравнение по нормализованным словам (регистр, «ё», знаки препинания не важны); остальной текст не меняется.
"""
import re

PHRASE_REPEATS = 3        # столько раз подряд должна повториться фраза из слов
MIN_PHRASE_WORDS = 2


def _words(text):
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def _same_sentence_repeated(text):
    sentences = [s.strip() for s in re.findall(r"[^.!?…]+[.!?…]*", text) if _words(s)]
    if len(sentences) < 2:
        return None
    first = _words(sentences[0])
    if len(first) < MIN_PHRASE_WORDS or any(_words(s) != first for s in sentences[1:]):
        return None
    return sentences[0]


def _collapse_phrase_run(text):
    """Текст без одного найденного повтора фразы или None."""
    found = list(re.finditer(r"\w+", text))
    words = [m.group().lower().replace("ё", "е") for m in found]
    for start in range(len(words)):
        for size in range(MIN_PHRASE_WORDS, (len(words) - start) // PHRASE_REPEATS + 1):
            phrase = words[start:start + size]
            runs = 1
            while words[start + runs * size:start + (runs + 1) * size] == phrase:
                runs += 1
            if runs >= PHRASE_REPEATS:
                keep_to = found[start + size - 1].end()           # конец первой копии
                cut_to = found[start + runs * size - 1].end()     # конец последней копии
                return text[:keep_to] + text[cut_to:]
    return None


def collapse_repeats(text):
    sentence = _same_sentence_repeated(text)
    if sentence is not None:
        return sentence
    while True:
        shorter = _collapse_phrase_run(text)
        if shorter is None:
            return text
        text = shorter
