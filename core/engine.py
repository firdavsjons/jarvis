import logging
import subprocess
import threading
import time

import speech_recognition as sr

from core.settings import env_float
from core.speech_text import prepare_for_speech

log = logging.getLogger(__name__)

PREFERRED_VOICE = "Milena"
DEFAULT_SPEECH_PAUSE = 1.2


def speech_pause():
    """SPEECH_PAUSE из .env (0.5–5 с); non_speaking_duration распознавателя — 0.5 с, меньше нельзя."""
    return env_float("SPEECH_PAUSE", DEFAULT_SPEECH_PAUSE, 0.5, 5.0)


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

        self.voice = _detect_voice()
        log.info("Голос озвучки: %s", self.voice or "системный по умолчанию")

        self._speak_lock = threading.Lock()  # одновременно звучит только одна фраза
        self._proc = None
        self._stop_generation = 0  # растёт при stop_speaking: фразы в очереди отменяются
        self._last_speech_end = 0.0
        self.speaking = threading.Event()

    def speak(self, text):
        # В лог — исходный текст, в say — без ссылок, путей, Markdown и эмодзи
        spoken = prepare_for_speech(text)
        if not spoken:
            return
        generation = self._stop_generation
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
                audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=5)
                text = self.recognizer.recognize_google(audio, language="ru-RU")
                log.info("👤 Вы: %s", text)
                return text.lower()
            except sr.WaitTimeoutError:
                return None
            except sr.UnknownValueError:
                return None
            except Exception as e:
                log.error("Ошибка микрофона: %s", e)
                return None
