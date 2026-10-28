import difflib
import logging
import re
import subprocess
import threading
import time
from collections import deque

import speech_recognition as sr

from core import journal
from core.settings import env_float
from core.speech_text import prepare_for_speech
from core.stt.base import Audio
from core.stt.factory import build_stt

log = logging.getLogger(__name__)

PREFERRED_VOICE = "Milena"
DEFAULT_SPEECH_PAUSE = 1.2
DEFAULT_TAIL_PAUSE = 0.8      # SPEECH_TAIL_PAUSE: пауза после озвучки перед прослушиванием
DEFAULT_PHRASE_LIMIT = 20     # SPEECH_PHRASE_LIMIT: самая длинная фраза, секунд (раньше 5)
ECHO_RATIO = 0.75             # фраза похожа на недавнюю речь ассистента (difflib)
ECHO_SHARE = 0.6              # или столько слов фразы (от 3 слов) есть в ней
ECHO_PHRASES = 3              # сколько последних озвученных фраз помним
# Короткие ответы пользователя, которые никогда не считаются эхом
NEVER_ECHO = {"да", "нет", "отмена", "стоп"}


def speech_pause():
    """SPEECH_PAUSE из .env (0.5–5 с); non_speaking_duration распознавателя — 0.5 с, меньше нельзя."""
    return env_float("SPEECH_PAUSE", DEFAULT_SPEECH_PAUSE, 0.5, 5.0)


def speech_tail_pause():
    """SPEECH_TAIL_PAUSE из .env (0–5 с): после речи ассистента микрофон ещё секунду слышит хвост и комнату."""
    return env_float("SPEECH_TAIL_PAUSE", DEFAULT_TAIL_PAUSE, 0.0, 5.0)


def speech_phrase_limit():
    """SPEECH_PHRASE_LIMIT из .env (3–60 с): запись фразы обрывается через столько секунд, даже если пауза не наступила."""
    return env_float("SPEECH_PHRASE_LIMIT", DEFAULT_PHRASE_LIMIT, 3.0, 60.0)


def pick_voice(say_voices_output):
    """Голос из вывода `say -v '?'`: Milena, иначе любой ru_RU, иначе None (системный)."""
    russian = []
    for line in say_voices_output.splitlines():
        # Формат строки: "Milena              ru_RU    # Здравствуйте! Меня зовут Милена."
        head = line.split("#", 1)[0].split()
        if len(head) < 2:
            continue
        name, locale = " ".join(head[:-1]), head[-1]
        if locale.replace("-", "_").lower().startswith("ru_"):
            russian.append(name)
    if PREFERRED_VOICE in russian:
        return PREFERRED_VOICE
    return russian[0] if russian else None


def say_command(text, voice):
    """Аргументы для say. Текст после "--", чтобы "-v ..." в тексте не стал опцией."""
    args = ["say"]
    if voice:
        args += ["-v", voice]
    return args + ["--", text]


def _detect_voice():
    try:
        result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Не удалось получить список голосов: %s", e)
        return None
    return pick_voice(result.stdout)


class SpeechEngine:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        # Пауза, после которой фраза считается законченной: 1.2 с, чтобы не обрезать на середине
        self.recognizer.pause_threshold = speech_pause()
        self.microphone = sr.Microphone()
        self.phrase_limit = speech_phrase_limit()

        self.voice = _detect_voice()
        log.info("Голос озвучки: %s", self.voice or "системный по умолчанию")

        self._speak_lock = threading.Lock()  # одновременно звучит только одна фраза
        self._proc = None
        self._stop_generation = 0  # растёт при stop_speaking: фразы в очереди отменяются
        self._last_speech_end = 0.0
        self.speaking = threading.Event()
        self.tail_pause = speech_tail_pause()
        self.recent_spoken = deque(maxlen=ECHO_PHRASES)   # последние озвученные фразы — для фильтра эха
        # Распознавание речи (core/stt): модель не грузится, пока не вызван start_stt(). Голосовые оповещения движка
        # («не установлено, использую Google») откладываются до flush_notices(): озвученные внутри listen(), они
        # заставили бы цикл отбросить саму фразу как «ассистент говорил во время записи»
        self.notices = []
        self.stt = build_stt(self.recognizer, notify=self.notices.append)

    def flush_notices(self):
        """Озвучивает накопленные оповещения распознавания; вызывает цикл прослушивания после проверок фразы."""
        notices, self.notices[:] = list(self.notices), []
        for text in notices:
            self.speak(text)

    def start_stt(self):
        """Запускает фоновую загрузку локальной модели (с Google модель не нужна)."""
        self.stt.start()

    def speak(self, text):
        # В лог — исходный текст, в say — без ссылок, путей, Markdown и эмодзи
        spoken = prepare_for_speech(text)
        if not spoken:
            return
        generation = self._stop_generation
        self.recent_spoken.append(spoken)
        with self._speak_lock:
            if generation != self._stop_generation:
                return  # речь прервали, пока фраза ждала своей очереди
            log.info("🗣 Assistant: %s", text)
            self.speaking.set()
            try:
                # Popen, а не run: процесс нужен, чтобы stop_speaking мог его прервать
                self._proc = subprocess.Popen(say_command(spoken, self.voice))
                self._proc.wait()
            except OSError as e:
                log.error("Ошибка озвучки: %s", e)
            finally:
                self._proc = None
                self._last_speech_end = time.monotonic()
                self.speaking.clear()

    def stop_speaking(self):
        """Прерывает текущую фразу и отменяет ждущие."""
        self._stop_generation += 1
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def tail_remaining(self):
        """Сколько ещё секунд после конца озвучки не слушать (SPEECH_TAIL_PAUSE)."""
        return max(0.0, self._last_speech_end + self.tail_pause - time.monotonic())

    def is_echo(self, text):
        """Распознанная фраза — это эхо ассистента: похожа (difflib ≥ 0.75) на одну из последних 3 озвученных или
        60% её слов (от 3 слов) есть в такой фразе. «да», «нет», «отмена», «стоп» эхом не считаются."""
        words = re.findall(r"\w+", str(text).lower())
        if not words or set(words) <= NEVER_ECHO:
            return False
        heard = " ".join(words)
        for spoken in list(self.recent_spoken):
            spoken_words = re.findall(r"\w+", spoken.lower())
            if difflib.SequenceMatcher(None, heard, " ".join(spoken_words)).ratio() >= ECHO_RATIO:
                return True
            if len(words) >= 3 and sum(1 for w in words if w in set(spoken_words)) / len(words) >= ECHO_SHARE:
                return True
        return False

    def speech_overlaps(self, since):
        """Говорил ли ассистент после момента since (time.monotonic())."""
        return self.speaking.is_set() or self._last_speech_end > since

    def calibrate(self):
        """Калибровка шума — один раз при старте прослушивания."""
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)

    def listen(self):
        with self.microphone as source:
            log.debug("🎤 Слушаю...")
            try:
                audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=self.phrase_limit)
            except sr.WaitTimeoutError:
                return None
            except Exception as e:
                log.error("Ошибка микрофона: %s", e)
                return None
        # Микрофон уже свободен: распознавание (до нескольких секунд) его не держит
        try:
            result = self.stt.recognize(Audio(audio))
        except Exception as e:
            log.error("Ошибка распознавания: %s", type(e).__name__)
            return None
        if result is None:
            return None
        log.info("👤 Вы: %s", journal.loggable_phrase(result.text))
        return result.text.lower()
