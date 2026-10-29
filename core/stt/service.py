"""SpeechToText: выбор движка, откат на Google, фильтр шума, оповещения голосом и журнал времени.

Откат на Google — только по технической причине (нет пакета или модели, ошибка, модель ещё грузится) и один раз на
фразу. Шум и галлюцинации откатом не лечатся: иначе каждый шорох уходил бы в интернет. С откатом "off" звук не
уходит никуда: Jarvis говорит «Не удалось распознать речь».

Фразы, которые говорит Jarvis: о том, что локальное распознавание не установлено (один раз за запуск), что оно ещё
загружается и что распознать не удалось (без причины; не чаще раза в NOTIFY_COOLDOWN секунд, чтобы шум не превращался
в поток реплик). Текст распознанного в лог не пишется — только длина записи и время.
"""
import logging
import time

from core.stt.base import MIN_SECONDS, Engine, Loading, SttError, Unavailable

log = logging.getLogger(__name__)

SLOW_SECONDS = 8.0
NOTIFY_COOLDOWN = 20.0
NOT_INSTALLED_FALLBACK = "Локальное распознавание не установлено, использую Google."
NOT_INSTALLED_OFF = "Локальное распознавание не установлено, откат на Google выключен."
FAILED_FALLBACK = "Локальное распознавание не запустилось, использую Google."
FAILED_OFF = "Локальное распознавание не запустилось, откат на Google выключен."
LOADING_REPLY = "Распознавание загружается, секунду."
FAILED_REPLY = "Не удалось распознать речь."
DISCARDED_LOG = "Отброшено (шум/галлюцинация)"


class SpeechToText:
    def __init__(self, primary, fallback=None, prompt_provider=None, notify=None, clock=time.monotonic):
        self.primary = primary
        self.fallback = fallback              # Engine | None; для google-основного и при STT_FALLBACK=off — None
        self.prompt_provider = prompt_provider
        self.notify = notify or (lambda text: None)
        self._clock = clock
        self._announced = False               # «не установлено» уже сказано
        self._last_notice = {}

    def describe(self):
        """Строка для лога при запуске: куда идёт звук."""
        if not self.primary.local:
            return f"Распознавание речи: {self.primary.name} (звук уходит в Google)"
        fallback = "откат: Google (звук уйдёт в Google при сбое)" if self.fallback else "откат выключен (звук не уходит из Mac)"
        return f"Распознавание речи: {self.primary.name}, модель {self.primary.model_name} (локально, звук остаётся на Mac), {fallback}"

    def start(self):
        log.info(self.describe())
        if self.primary.local:
            self.primary.start_loading()

    # --- основной путь -------------------------------------------------------------------------------------------
    def recognize(self, audio):
        """Recognition с непустым текстом или None (тишина, шум, ошибка). Об ошибках Jarvis говорит сам."""
        if audio.seconds < MIN_SECONDS:
            log.info("%s: запись %.2f с", DISCARDED_LOG, audio.seconds)
            return None
        try:
            return self._finish(self._attempt(self.primary, audio))
        except Loading:
            return self._on_loading(audio)
        except Unavailable as e:
            return self._on_unavailable(e, audio)
        except SttError as e:
            log.warning("Распознавание (%s) не сработало: %s", self.primary.name, e)
            return self._fallback_or_fail(audio)

    def _attempt(self, engine, audio):
        prompt = ""
        if engine.uses_prompt and self.prompt_provider is not None:
            prompt = self.prompt_provider.get()
        return engine.recognize(audio, prompt)

    def _finish(self, result):
        log.info("Распознавание (%s): запись %.1f с, обработка %.1f с", result.engine, result.audio_seconds, result.seconds)
        if result.seconds > SLOW_SECONDS:
            log.warning("Распознавание заняло %.1f с (больше %.0f с): %s, запись %.1f с",
                        result.seconds, SLOW_SECONDS, result.engine, result.audio_seconds)
        if result.discarded:
            if result.discarded == "noise":      # эхо подсказки движок уже записал в лог со счётчиком
                log.info(DISCARDED_LOG)
            return None
        return result if result.text.strip() else None

    # --- сбои и откат --------------------------------------------------------------------------------------------
    def _on_loading(self, audio):
        if self.fallback is not None:
            log.info("Модель распознавания ещё загружается, распознаю в %s", self.fallback.name)
            return self._fallback_or_fail(audio)
        self._say("loading", LOADING_REPLY)
        return None

    def _on_unavailable(self, error, audio):
        if not self._announced:
            self._announced = True
            installed = error.kind == "not_installed"
            if self.fallback is not None:
                text = NOT_INSTALLED_FALLBACK if installed else FAILED_FALLBACK
            else:
                text = NOT_INSTALLED_OFF if installed else FAILED_OFF
            log.warning("%s (%s)", text, error.reason)
            self.notify(text)
            if self.fallback is None:
                return None            # причина названа — повторное «не удалось» сразу за ней не нужно
        return self._fallback_or_fail(audio)

    def _fallback_or_fail(self, audio):
        if self.fallback is None:
            self._say("failed", FAILED_REPLY)
            return None
        try:
            return self._finish(self._attempt(self.fallback, audio))
        except SttError as e:
            log.warning("Запасное распознавание (%s) не сработало: %s", self.fallback.name, e)
            self._say("failed", FAILED_REPLY)
            return None

    def _say(self, key, text):
        now = self._clock()
        last = self._last_notice.get(key)
        if last is not None and now - last < NOTIFY_COOLDOWN:
            return
        self._last_notice[key] = now
        self.notify(text)
