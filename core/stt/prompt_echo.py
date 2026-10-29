"""Эхо подсказки: Whisper иногда дописывает к фразе куски подсказки или повторяет её вместо фразы.

Живой лог: «Контакты: Да.», «Контакты: закрой, как вы видите?». От этого «закрой калькулятор» закрыло «Контакты».
Две защиты:
1. `strip_labels` — метки подсказки (контакты:, приложения:, команды:, программы:, как я называю приложения:) с двоеточием
   удаляются из текста в любом месте, регистр не важен (подсказка теперь без меток, но защита остаётся).
2. `is_prompt_echo` — текст целиком совпал с предложением подсказки (по нормализованной строке). Это эхо, только если
   запись слишком коротка для такого текста или Whisper не уверен: предложения подсказки — настоящие команды
   («поставь таймер на пять минут»), и сказанную вслух такую команду отбрасывать нельзя.
"""
import re

from core.stt.blacklist import normalize

LABELS = ("как я называю приложения", "контакты", "приложения", "команды", "программы")
_LABEL_RE = re.compile(r"(?<!\w)(?:" + "|".join(LABELS) + r")\s*:\s*", re.IGNORECASE)

# Оценочные, подобрать по логу. Живая речь — около 12–15 символов в секунду; запись включает и тишину по краям, поэтому
# порог с запасом: больше 20 символов текста на секунду записи значит, что столько сказать было невозможно
ECHO_MAX_CHARS_PER_SECOND = 20
# Оценочные, подобрать по логу. Уверенность Whisper (e^avg_logprob) ниже — текст ненадёжен
ECHO_MAX_CONFIDENCE = 0.5


def strip_labels(text):
    """(текст без меток подсказки, сколько меток убрано)."""
    cleaned, count = _LABEL_RE.subn(" ", text)
    return " ".join(cleaned.split()), count


def prompt_sentences(prompt):
    """Нормализованные предложения подсказки; для первого ещё и вариант без обращения «Джарвис»."""
    result = set()
    for sentence in re.findall(r"[^.!?]+[.!?]*", prompt or ""):
        words = normalize(sentence)
        if not words:
            continue
        result.add(words)
        if words.startswith("джарвис "):
            result.add(words[len("джарвис "):])
    return result


def is_prompt_echo(text, prompt, audio_seconds, confidence):
    if normalize(text) not in prompt_sentences(prompt):
        return False
    too_fast = len(text.strip()) / max(audio_seconds, 0.001) > ECHO_MAX_CHARS_PER_SECOND
    unsure = confidence is not None and confidence < ECHO_MAX_CONFIDENCE
    return too_fast or unsure
