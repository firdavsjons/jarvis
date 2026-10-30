"""Системная озвучка через `say` (голос Milena или другой ru_RU): прежняя логика со всеми защитами."""
import logging
import subprocess

from core.tts.base import TTSEngine

log = logging.getLogger(__name__)

PREFERRED_VOICE = "Milena"


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
    """Аргументы для say. Текст после "--", чтобы "-v ..." в тексте не стал опцией (а без текста say читает stdin)."""
    args = ["say"]
    if voice:
        args += ["-v", voice]
    return args + ["--", text]


class SayEngine(TTSEngine):
    name = "say"
    local = True

    def __init__(self, voice=None):
        self.voice = voice
        self._proc = None

    def speak(self, text):
        try:
            # Popen, а не run: процесс нужен, чтобы stop() мог его прервать
            self._proc = subprocess.Popen(say_command(text, self.voice))
            self._proc.wait()
        except OSError as e:
            log.error("Ошибка озвучки: %s", e)
        finally:
            self._proc = None

    def stop(self):
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
