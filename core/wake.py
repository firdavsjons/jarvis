"""Слово-активатор «Джарвис» (Этап 8.2): Jarvis отвечает только на фразы, обращённые к нему.

Whisper распознаёт всю речь рядом, поэтому без обращения Jarvis пытался выполнять «Нет, но…», «Спасибо.» и «Сибирь.».
Слово ищется в уже распознанном тексте (детектор слова без Whisper — отдельный шаг, не сделан).

Фраза обращена к Jarvis, если слово стоит
- в начале (слово 1 либо слово 2 после одного междометия: «эй», «ну», «окей», «ок», «слушай», «привет»); кроме слов
  списка допускается одна ошибка (difflib ≥ 0,8), если слово не из NOT_WAKE_WORDS;
- в самом конце («открой калькулятор, Джарвис»), только точное слово списка.
В середине слово не считается. Слово и междометие вырезаются, остаток обрабатывается как обычная команда.

Список (Этап 8.2.1) включает искажения, которые Whisper действительно выдаёт на слове «Джарвис» («червис», «джервис»,
«жарусь», «джарис»…), и слова из WAKE_WORDS в .env.

Окно диалога: после озвученного ответа на команду или вопрос Jarvis (не «Секунду.», не таймер, не оповещения
распознавания) слово не требуется WAKE_WINDOW секунд от конца озвучки; пока Jarvis ждёт ответа на вопрос — 20 секунд.
Подтверждение (main.has_pending), «стоп», «хватит», «отмена» и текстовый ввод слова не требуют.

Слово может прийти из подсказки Whisper («Джарвис, открой …»): тогда оно засчитывается, только если после него есть
команда и уверенность не ниже порога. Отброшенный текст нигде не записывается: ни в лог, ни в историю, ни в кэш, ни в
журнал действий.
"""
import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from core.settings import env_float, env_value

log = logging.getLogger(__name__)

CANONICAL_WORDS = ("джарвис", "jarvis")
# Слова, которые Whisper выдаёт на «Джарвис» (по живым данным диагностики) и обычные варианты
WAKE_WORDS = ("червис", "чарвис", "джервис", "джервез", "жарвис", "жарвес", "джарвес", "джарвус", "джарис", "джарус",
              "дарвис", "ярвис", "jarvis", "джарвис", "жарусь", "жарус")
# Настоящие слова, похожие на слова списка: нечётким поиском не засчитываются («Сервис недоступен» ≈ «червис» 0,83)
NOT_WAKE_WORDS = ("дарвин", "сервис", "парус", "червь", "жаров", "жара")
INTERJECTIONS = ("эй", "ну", "окей", "ок", "слушай", "привет")
FUZZY_CUTOFF = 0.8
STOP_WORDS = ("стоп", "хватит")         # остановить озвучку и закрыть окно, ответа нет
CANCEL_WORDS = ("отмена",)               # то же, но фраза идёт дальше обычным путём
PUNCTUATION = " \t\n,.;:!?-—–…«»\"'"

DEFAULT_WINDOW = 8.0
ANSWER_TIMEOUT = 20.0                    # окно, пока Jarvis ждёт ответа на вопрос (как таймаут подтверждения)
DROP_REPORT_EVERY = 10

# Свои слова (WAKE_WORDS в .env)
EXTRA_WORDS_MAX = 10
EXTRA_WORD_MIN_LENGTH = 4

# Слово в начале могло прийти из подсказки Whisper. Оценочные, подобрать по логу (значения — по живым логам):
PROMPT_WAKE_MAX_RECORDING = 1.2          # запись короче (с) — слишком коротка, чтобы в ней прозвучало слово
WAKE_PROMPT_MIN_CONFIDENCE = 0.3         # оценочный, по живым логам: уверенность ниже — слову из подсказки не верим
WAKE_PROMPT_MIN_CONFIDENCE_ONE_WORD = 0.4    # оценочный, по живым логам: для команды из одного слова («Джарвис, громче»)
WAKE_LISTEN_WINDOW_MIN_CONFIDENCE = 0.4  # оценочный, по живым логам: окно после «Слушаю.» открывается от этой уверенности

DROPPED_LOG = "Отброшено (нет слова «Джарвис»)"
LISTENING_REPLY = "Слушаю."


def normalize_word(word):
    return str(word).strip().lower().replace("ё", "е")


def wake_settings(path=None):
    """(включено, WAKE_WINDOW в секундах). WAKE_WORD=off/no/false/0/none выключает."""
    raw = (env_value("WAKE_WORD", "on", path) or "on").strip().lower()
    window = env_float("WAKE_WINDOW", DEFAULT_WINDOW, 3.0, 30.0, path)
    return raw not in {"off", "no", "false", "0", "none", "нет"}, window


def extra_wake_words(path=None):
    """Дополнительные слова из WAKE_WORDS (через запятую): не короче 4 символов, только буквы одного алфавита (без смеси
    кириллицы и латиницы), не больше 10. Неподходящие пропускаются с предупреждением. Читается при запуске."""
    raw = env_value("WAKE_WORDS", "", path) or ""
    words = []
    for item in raw.split(","):
        word = normalize_word(item)
        if not word:
            continue
        if len(word) < EXTRA_WORD_MIN_LENGTH:
            log.warning("WAKE_WORDS: слово «%s» пропущено (короче %d символов)", word, EXTRA_WORD_MIN_LENGTH)
        elif not re.fullmatch(r"[а-я]+|[a-z]+", word):
            log.warning("WAKE_WORDS: слово «%s» пропущено (допустимы только буквы одного алфавита, без смеси кириллицы и "
                        "латиницы)", word)
        elif word in WAKE_WORDS or word in words:
            continue
        elif len(words) >= EXTRA_WORDS_MAX:
            log.warning("WAKE_WORDS: слово «%s» пропущено (не больше %d слов)", word, EXTRA_WORDS_MAX)
        else:
            words.append(word)
    return tuple(words)


# --- поиск слова -------------------------------------------------------------------------------------------------------
@dataclass
class Token:
    word: str          # нормализованное слово
    start: int
    end: int


def tokens(text):
    return [Token(normalize_word(m.group()), m.start(), m.end()) for m in re.finditer(r"\w+", str(text))]


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def match_word(word, fuzzy, words=WAKE_WORDS):
    """(слово списка, сходство, откуда) или None. Откуда: "list" — слово из встроенного списка, "extra" — из WAKE_WORDS,
    "fuzzy" — не из списка, но похоже (≥ FUZZY_CUTOFF) на слово списка. Сходство слова списка — с «джарвис»/«jarvis»."""
    if word in words:
        if word in WAKE_WORDS:
            return word, max(_ratio(word, c) for c in CANONICAL_WORDS), "list"
        return word, 1.0, "extra"
    if not fuzzy or word in NOT_WAKE_WORDS:
        return None
    ratio, nearest = max((_ratio(word, w), w) for w in words)
    return (nearest, ratio, "fuzzy") if ratio >= FUZZY_CUTOFF else None


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
    heard: str = ""                     # какое слово услышано (нормализованное)
    nearest: str = ""                   # слово списка, с которым совпало
    ratio: float = 0.0                  # сходство с ним (1,0 — совпало полностью)
    via: str = ""                       # "list" | "extra" | "fuzzy"

    @property
    def distorted(self):
        """Слово засчитано не как «джарвис»/«jarvis», а как искажение, свой вариант или нечёткое совпадение."""
        return self.found and self.heard not in CANONICAL_WORDS

    def describe(self):
        """«червис ≈ джарвис, 0.62, из списка» — для диагностики."""
        if not self.found:
            return ""
        source = {"list": "из списка", "extra": "из WAKE_WORDS", "fuzzy": "по искажению"}[self.via]
        target = self.nearest if self.via == "fuzzy" else (self.heard if self.via == "extra" else
                                                           max(CANONICAL_WORDS, key=lambda c: _ratio(self.heard, c)))
        return f"{self.heard} ≈ {target}, {self.ratio:.2f}, {source}"


def find_wake(text, words=WAKE_WORDS):
    """Judgement без учёта подсказки: где слово и что останется после его вырезания."""
    toks = tokens(text)
    if not toks:
        return Judgement(reason="слова нет: пустой текст")
    first = 1 if toks[0].word in INTERJECTIONS and len(toks) > 1 else 0
    start = match_word(toks[first].word, True, words)
    start_idx = first if start else None
    end = match_word(toks[-1].word, False, words)
    end_idx = len(toks) - 1 if end and (start_idx is None or len(toks) - 1 > start_idx) else None
    if start_idx is None and end_idx is None:
        inside = any(match_word(t.word, False, words) for t in toks)
        return Judgement(reason="слово в середине фразы" if inside else "слова нет")
    begin = toks[start_idx].end if start_idx is not None else 0
    finish = toks[end_idx].start if end_idx is not None else len(text)
    rest = _trim(text[begin:finish]) if begin <= finish else ""
    position = "start" if start_idx is not None else "end"
    nearest, ratio, via = start if start_idx is not None else end
    heard = toks[start_idx if start_idx is not None else end_idx].word
    return Judgement(found=True, position=position, rest=rest, heard=heard, nearest=nearest, ratio=ratio, via=via,
                     reason="слово в начале" if position == "start" else "слово в конце")


def _trim(text):
    return text.strip(PUNCTUATION)


def prompt_induced(text, audio_seconds, confidence, words=WAKE_WORDS):
    """Текст начинается со слова «Джарвис», а запись слишком коротка или Whisper не уверен: слово, возможно, пришло из
    подсказки (она начинается с «Джарвис»), а не из речи."""
    found = find_wake(text, words)
    if not (found.found and found.position == "start"):
        return False
    return (audio_seconds is not None and audio_seconds < PROMPT_WAKE_MAX_RECORDING) or \
        (confidence is not None and confidence < WAKE_PROMPT_MIN_CONFIDENCE)


def judge(text, from_prompt=False, confidence=None, words=WAKE_WORDS):
    """Засчитано ли слово как обращение и почему. from_prompt — Recognition.wake_from_prompt.

    Слово, возможно пришедшее из подсказки: остаток из 2+ слов — нужна уверенность ≥ WAKE_PROMPT_MIN_CONFIDENCE, из одного
    слова — ≥ WAKE_PROMPT_MIN_CONFIDENCE_ONE_WORD, остаток пуст — не засчитывается (одно «Джарвис» из подсказки не зовёт)."""
    result = find_wake(text, words)
    if not result.found:
        return result
    result.from_prompt = bool(from_prompt)
    if not from_prompt:
        result.counted = True
        return result
    rest_words = len(_words(result.rest))
    needed = WAKE_PROMPT_MIN_CONFIDENCE if rest_words >= 2 else WAKE_PROMPT_MIN_CONFIDENCE_ONE_WORD
    why = None
    if rest_words == 0:
        why = "остаток пуст"
    elif confidence is not None and confidence < needed:
        kind = "" if rest_words >= 2 else " для остатка из 1 слова"
        why = f"уверенность {confidence:.2f} < порога {needed:.2f}{kind}"
    if why is None:
        result.counted = True
        result.reason = f"{result.reason}; возможно из подсказки, но после слова команда из {rest_words} слов"
    else:
        result.reason = f"слово возможно из подсказки Whisper ({why})"
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
    opens_window: bool = True    # для "listen": открывать ли окно диалога после «Слушаю.»


class WakeGate:
    def __init__(self, window=DEFAULT_WINDOW, answer_timeout=ANSWER_TIMEOUT, anchor=lambda: None,
                 speaking=lambda: False, expects_answer=lambda: False, stop_speech=lambda: None,
                 close_anchor=lambda: None, clock=time.monotonic, words=WAKE_WORDS):
        self.window, self.answer_timeout = window, answer_timeout
        self._anchor, self._speaking = anchor, speaking             # конец озвучки, открывающей окно / идёт ли озвучка
        self._expects_answer, self._stop_speech = expects_answer, stop_speech
        self._close_anchor, self._clock = close_anchor, clock
        self.words = tuple(words)
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
        result = judge(text, phrase.from_prompt, phrase.confidence, self.words)
        in_window = self.window_open(phrase.started_at)
        if result.found:
            if is_stop_phrase(result.rest):
                return self._stop()
            if result.counted:
                if result.distorted:
                    log.info("слово «Джарвис» засчитано по искажению (расстояние %.2f)", result.ratio)
                if not result.rest:
                    return self._listen(phrase)
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

    def _listen(self, phrase):
        opens = phrase.confidence is None or phrase.confidence >= WAKE_LISTEN_WINDOW_MIN_CONFIDENCE
        if opens:
            log.info("Обращение «Джарвис» без команды")
        else:
            log.info("Обращение «Джарвис» без команды: окно диалога не открыто (уверенность %.2f < %.2f)",
                     phrase.confidence, WAKE_LISTEN_WINDOW_MIN_CONFIDENCE)
        return Decision("listen", opens_window=opens)

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
    """Шлюз по WAKE_WORD / WAKE_WINDOW / WAKE_WORDS из .env или None, если слово выключено."""
    enabled, window = wake_settings(path)
    if not enabled:
        log.info("Слово-активатор выключено (WAKE_WORD=off): Jarvis отвечает на любую услышанную фразу")
        return None
    extra = extra_wake_words(path)
    log.info("Слово-активатор «Джарвис» включено, окно диалога %.0f с, слов в списке %d (своих: %d)", window,
             len(WAKE_WORDS) + len(extra), len(extra))
    return WakeGate(window, answer_timeout, anchor=lambda: engine.window_opened_at,
                    speaking=engine.speaking.is_set, expects_answer=ai.expects_answer,
                    stop_speech=engine.stop_speaking, close_anchor=engine.close_window,
                    words=(*WAKE_WORDS, *extra))
