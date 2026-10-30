"""Выбор голоса из .env и `SpeechVoice` — единая точка озвучки для SpeechEngine.

TTS_ENGINE=say|sherpa (по умолчанию say; нейронная часть при say не импортируется), TTS_VOICE (папка модели), TTS_SPEED
(0.8–1.5), TTS_THREADS (по умолчанию 2), TTS_EFFECT=off|jarvis, TTS_PITCH (−6…0 полутонов), TTS_REVERB (0–0.3).
"""
import logging

from core import storage
from core.tts.base import TTSSettings, tts_settings

log = logging.getLogger(__name__)

NOT_INSTALLED_NOTICE = "Нейронный голос не установлен, использую системный."
FAILED_NOTICE = "Нейронный голос не запустился, использую системный."


def models_dir():
    return storage.DATA_DIR / "models" / "tts"


class SpeechVoice:
    """Нейронный голос, пока он готов, иначе say. Ошибка посреди фразы — оставшиеся предложения читает say, прозвучавшие
    не повторяются. Оповещение «не установлено» — один раз за запуск, голосом say (не нейронным: иначе зациклится)."""

    def __init__(self, say, neural=None):
        self.say, self.neural = say, neural
        self._announced = False

    def start(self):
        """Фоновая загрузка нейронного голоса (с say ничего не грузится)."""
        if self.neural is not None:
            self.neural.start_loading()

    def take_notice(self):
        """Оповещение для SpeechEngine (один раз за запуск) или None."""
        neural = self.neural
        if neural is None or self._announced or neural.state != "unavailable":
            return None
        self._announced = True
        error = neural.unavailable
        log.warning("Нейронный голос не используется: %s", error.reason)
        return NOT_INSTALLED_NOTICE if error.kind == "not_installed" else FAILED_NOTICE

    def speak(self, text):
        """Озвучивает text, блокируя до конца. Возвращает длину хвоста эффекта (секунды) для _last_speech_end."""
        neural = self.neural
        if neural is None or neural.state != "ready":
            self.say.speak(text)
            return 0.0
        try:
            result = neural.speak(text)
        except Exception as e:
            log.warning("Нейронный голос: ошибка (%s), говорит системный", type(e).__name__)
            self.say.speak(text)
            return 0.0
        if result.error and not result.stopped:
            rest = " ".join(result.sentences[result.done:])
            if rest:
                log.warning("Нейронный голос: дочитываю системным (%d из %d предложений)", result.done, len(result.sentences))
                self.say.speak(rest)
        return result.tail

    def stop(self):
        self.say.stop()
        if self.neural is not None:
            self.neural.stop()


def build_neural(settings, models=None, player=None):
    """SherpaEngine по настройкам (импорт модуля только здесь, чтобы TTS_ENGINE=say его не загружал)."""
    from core.tts import effect
    from core.tts.sherpa import SherpaEngine
    use_effect = settings.effect
    if use_effect and not effect.available():
        log.warning("TTS_EFFECT=jarvis: numpy недоступен, эффект выключен")
        use_effect = False
    return SherpaEngine(voice=settings.voice, speed=settings.speed, threads=settings.threads, effect_on=use_effect,
                        pitch=settings.pitch, reverb=settings.reverb, models_dir=models or models_dir(), player=player)


def build_tts(say, settings=None, models=None, player=None):
    settings = settings or tts_settings()
    if settings.engine == "sherpa":
        return SpeechVoice(say, build_neural(settings, models, player))
    return SpeechVoice(say)
