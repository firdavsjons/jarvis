"""Google (recognize_google): запасной движок и прежнее поведение. Звук уходит в интернет."""
import logging
import time

import speech_recognition as sr

from core.stt.base import Engine, Recognition, SttError

log = logging.getLogger(__name__)

LANGUAGE_CODES = {"ru": "ru-RU", "en": "en-US", "uk": "uk-UA", "tg": "tg-TJ"}


def google_language(language):
    """«ru» -> «ru-RU» (Google ждёт код с регионом); «ru-RU» остаётся как есть."""
    language = str(language or "ru").strip()
    if "-" in language:
        return language
    return LANGUAGE_CODES.get(language.lower(), f"{language.lower()}-{language.upper()}")


class GoogleEngine(Engine):
    name = "google"
    local = False
    uses_prompt = False

    def __init__(self, recognizer, language="ru", clock=time.monotonic):
        self.recognizer = recognizer
        self.language = google_language(language)
        self._clock = clock

    def recognize(self, audio, prompt=""):
        # prompt игнорируется: ни подсказка, ни её часть в Google не уходят
        started = self._clock()
        try:
            text = self.recognizer.recognize_google(audio.source, language=self.language)
        except sr.UnknownValueError:
            text = ""
        except Exception as e:     # нет сети, лимит, не работает flac
            raise SttError(f"Google не распознал: {type(e).__name__}") from e
        return Recognition(text=text.strip(), engine=self.name, seconds=self._clock() - started,
                           audio_seconds=audio.seconds)
