"""Слово-активатор «Джарвис» (Этап 8.2): Jarvis отвечает только на фразы, обращённые к нему.

Whisper распознаёт всю речь рядом, поэтому без обращения Jarvis пытался выполнять «Нет, но…», «Спасибо.» и «Сибирь.».
Слово ищется в уже распознанном тексте (детектор слова без Whisper — отдельный шаг, не сделан).

Фраза обращена к Jarvis, если слово стоит
- в начале (слово 1 либо слово 2 после одного междометия: «эй», «ну», «окей», «ок», «слушай», «привет»); допускается
  одна ошибка (difflib ≥ 0,8);
- в самом конце («открой калькулятор, Джарвис»), только точное слово.
В середине слово не считается. Слово и междометие вырезаются, остаток обрабатывается как обычная команда.

Окно диалога: после озвученного ответа на команду или вопрос Jarvis (не «Секунду.», не таймер, не оповещения
распознавания) слово не требуется WAKE_WINDOW секунд от конца озвучки; пока Jarvis ждёт ответа на вопрос — 20 секунд.
Подтверждение (main.has_pending), «стоп», «хватит», «отмена» и текстовый ввод слова не требуют.

Слово может прийти из подсказки Whisper («Джарвис, открой …»): тогда оно засчитывается, только если после него есть
остаток из 2+ слов и уверенность не ниже порога. Отброшенный текст нигде не записывается: ни в лог, ни в историю,
ни в кэш, ни в журнал.
"""
import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from core.settings import env_float, env_value

log = logging.getLogger(__name__)

WAKE_WORDS = ("джарвис", "jarvis", "жарвис", "джарвес", "дарвис", "чарвис", "ярвис")
INTERJECTIONS = ("эй", "ну", "окей", "ок", "слушай", "привет")
FUZZY_CUTOFF = 0.8
STOP_WORDS = ("стоп", "хватит")         # остановить озвучку и закрыть окно, ответа нет
CANCEL_WORDS = ("отмена",)               # то же, но фраза идёт дальше обычным путём
PUNCTUATION = " \t\n,.;:!?-—–…«»\"'"

DEFAULT_WINDOW = 8.0
ANSWER_TIMEOUT = 20.0                    # окно, пока Jarvis ждёт ответа на вопрос (как таймаут подтверждения)
DROP_REPORT_EVERY = 10

# Слово в начале могло прийти из подсказки Whisper. Оценочные, подобрать по логу:
PROMPT_WAKE_MAX_RECORDING = 1.2          # запись короче (с) — слишком коротка, чтобы в ней прозвучало слово
PROMPT_WAKE_MIN_CONFIDENCE = 0.5         # уверенность Whisper ниже — слову не верим
PROMPT_WAKE_MIN_REST_WORDS = 2           # такое слово засчитывается, только если после него команда из 2+ слов

DROPPED_LOG = "Отброшено (нет слова «Джарвис»)"
LISTENING_REPLY = "Слушаю."


def wake_settings(path=None):
    """(включено, WAKE_WINDOW в секундах). WAKE_WORD=off/no/false/0/none выключает."""
    raw = (env_value("WAKE_WORD", "on", path) or "on").strip().lower()
    window = env_float("WAKE_WINDOW", DEFAULT_WINDOW, 3.0, 30.0, path)
    return raw not in {"off", "no", "false", "0", "none", "нет"}, window


# --- поиск слова -------------------------------------------------------------------------------------------------------
@dataclass
class Token:
    word: str          # нормализованное слово
    start: int
    end: int


def tokens(text):
    return [Token(m.group().lower().replace("ё", "е"), m.start(), m.end()) for m in re.finditer(r"\w+", str(text))]


def is_wake_word(word, fuzzy):
    if word in WAKE_WORDS:
        return True
    return fuzzy and any(difflib.SequenceMatcher(None, word, w).ratio() >= FUZZY_CUTOFF for w in WAKE_WORDS)


def _trim(text):
    return text.strip(PUNCTUATION)


def _words(text):
    return re.findall(r"\w+", str(text).lower().replace("ё", "е"))


@dataclass
class Judgement:
    found: bool = False
    position: Optional[str] = None      # "start" | "end"
    rest: str = ""                      # текст без слова и междометия
    counted: bool = False               # слово засчитано как обращение
    reason: str = ""
    from_prompt: bool = False


def find_wake(text):
    """Judgement без учёта подсказки: где слово и что останется после его вырезания."""
    toks = tokens(text)
    if not toks:
        return Judgement(reason="слова нет: пустой текст")
    first = 1 if toks[0].word in INTERJECTIONS and len(toks) > 1 else 0
    start_idx = first if is_wake_word(toks[first].word, fuzzy=True) else None
    end_idx = len(toks) - 1 if toks[-1].word in WAKE_WORDS and (start_idx is None or len(toks) - 1 > start_idx) else None
    if start_idx is None and end_idx is None:
        inside = any(t.word in WAKE_WORDS for t in toks)
        return Judgement(reason="слово в середине фразы" if inside else "слова нет")
    begin = toks[start_idx].end if start_idx is not None else 0
    finish = toks[end_idx].start if end_idx is not None else len(text)
    rest = _trim(text[begin:finish]) if begin <= finish else ""
    position = "start" if start_idx is not None else "end"
    return Judgement(found=True, position=position, rest=rest, reason="слово в начале" if position == "start"
                     else "слово в конце")


def prompt_induced(text, audio_seconds, confidence):
    """Текст начинается со слова «Джарвис», а запись слишком коротка или Whisper не уверен: слово, возможно, пришло из
    подсказки (она начинается с «Джарвис»), а не из речи."""
    found = find_wake(text)
    if not (found.found and found.position == "start"):
        return False
    return (audio_seconds is not None and audio_seconds < PROMPT_WAKE_MAX_RECORDING) or \
        (confidence is not None and confidence < PROMPT_WAKE_MIN_CONFIDENCE)


def judge(text, from_prompt=False, confidence=None):
    """Засчитано ли слово как обращение и почему. from_prompt — Recognition.wake_from_prompt."""
    result = find_wake(text)
    if not result.found:
        return result
    result.from_prompt = bool(from_prompt)
    rest_words = len(_words(result.rest))
    if from_prompt:
        sure = confidence is None or confidence >= PROMPT_WAKE_MIN_CONFIDENCE
        if rest_words >= PROMPT_WAKE_MIN_REST_WORDS and sure:
            result.counted = True
            result.reason = f"{result.reason}; возможно из подсказки, но после слова команда из {rest_words} слов"
        else:
            why = []
            if rest_words < PROMPT_WAKE_MIN_REST_WORDS:
                why.append(f"остаток короче {PROMPT_WAKE_MIN_REST_WORDS} слов")
            if not sure:
                why.append(f"уверенность {confidence:.2f} < {PROMPT_WAKE_MIN_CONFIDENCE:.2f}")
            result.reason = "слово возможно из подсказки Whisper (" + ", ".join(why) + ")"
        return result
    result.counted = True
    return result


def is_stop_phrase(text):
    words = _words(text)
    return bool(words) and all(w in STOP_WORDS for w in words)


def is_cancel_phrase(text):
    words = _words(text)
    return bool(words) and all(w in CANCEL_WORDS for w in words)


# --- шлюз -------------------------------------------------------------------------------------------------------------
@dataclass
class Phrase:
    """Сведения о распознанной фразе, которые нужны шлюзу."""
    started_at: Optional[float] = None       # time.monotonic() начала речи (конец записи − длина записи)
    audio_seconds: Optional[float] = None
    confidence: Optional[float] = None
    from_prompt: bool = False                # Recognition.wake_from_prompt


@dataclass
class Decision:
    action: str          # "process" | "drop" | "listen" | "stop"
    text: str = ""


class WakeGate:
    def __init__(self, window=DEFAULT_WINDOW, answer_timeout=ANSWER_TIMEOUT, anchor=lambda: None,
                 speaking=lambda: False, expects_answer=lambda: False, stop_speech=lambda: None,
                 close_anchor=lambda: None, clock=time.monotonic):
        self.window, self.answer_timeout = window, answer_timeout
        self._anchor, self._speaking = anchor, speaking             # конец озвучки, открывающей окно / идёт ли озвучка
        self._expects_answer, self._stop_speech = expects_answer, stop_speech
        self._close_anchor, self._clock = close_anchor, clock
        self.dropped = 0                                           # за запуск

    # окно диалога
    def window_open(self, started_at=None):
        if self._speaking():
            return True
        end = self._anchor()
        if end is None:
            return False
        started = started_at if started_at is not None else self._clock()
        limit = self.answer_timeout if self._expects_answer() else self.window
        return max(0.0, started - end) <= limit

    def close_window(self):
        self._close_anchor()

    # решение
    def check(self, text, phrase=None):
        phrase = phrase or Phrase()
        if is_stop_phrase(text):
            return self._stop()
        if is_cancel_phrase(text):
            self._stop_speech()
            self.close_window()
            return Decision("process", text)
        result = judge(text, phrase.from_prompt, phrase.confidence)
        in_window = self.window_open(phrase.started_at)
        if result.found:
            if is_stop_phrase(result.rest):
                return self._stop()
            if result.counted:
                if not result.rest:
                    log.info("Обращение «Джарвис» без команды")
                    return Decision("listen")
                if is_cancel_phrase(result.rest):
                    self._stop_speech()
                    self.close_window()
                return Decision("process", result.rest)
            log.info("Слово «Джарвис» не засчитано: %s", result.reason)
            if result.rest and in_window:
                return Decision("process", result.rest)
            return self._drop()
        if in_window:
            return Decision("process", text)
        return self._drop()

    def _stop(self):
        self._stop_speech()
        self.close_window()
        log.info("Стоп: озвучка остановлена, окно диалога закрыто")
        return Decision("stop")

    def _drop(self):
        self.dropped += 1
        log.info(DROPPED_LOG)
        if self.dropped % DROP_REPORT_EVERY == 0:
            log.info("за запуск отброшено %d фраз без обращения", self.dropped)
        return Decision("drop")


def build_gate(engine, ai, answer_timeout=ANSWER_TIMEOUT, path=None):
    """Шлюз по WAKE_WORD / WAKE_WINDOW из .env или None, если слово выключено."""
    enabled, window = wake_settings(path)
    if not enabled:
        log.info("Слово-активатор выключено (WAKE_WORD=off): Jarvis отвечает на любую услышанную фразу")
        return None
    log.info("Слово-активатор «Джарвис» включено, окно диалога %.0f с", window)
    return WakeGate(window, answer_timeout, anchor=lambda: engine.window_opened_at,
                    speaking=engine.speaking.is_set, expects_answer=ai.expects_answer,
                    stop_speech=engine.stop_speaking, close_anchor=engine.close_window)
