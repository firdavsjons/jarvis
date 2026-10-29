"""Настройки распознавания из .env и сборка SpeechToText.

STT_ENGINE=whisper_cpp|faster_whisper|google (по умолчанию whisper_cpp; «whisper» — то же, что whisper_cpp)
STT_FALLBACK=google|off      (по умолчанию google; off — звук никуда не уходит, при сбое «Не удалось распознать речь»)
STT_MODEL                    (пусто — по умолчанию движка: large-v3-turbo-q5_0 / small)
STT_LANGUAGE=ru
STT_PROMPT=on|off           (по умолчанию on; off — подсказка Whisper вообще не строится и не передаётся)
STT_THREADS=4                (не больше половины ядер процессора)
STT_UNLOAD_MINUTES=10        (простой, после которого модель выгружается из памяти; 0 — не выгружать)
"""
import logging
import os
from dataclasses import dataclass

from core import storage
from core.settings import env_float, env_value
from core.stt.faster import FasterWhisperEngine
from core.stt.google import GoogleEngine
from core.stt.prompt import PromptProvider
from core.stt.service import SpeechToText
from core.stt.whisper_cpp import WhisperCppEngine

log = logging.getLogger(__name__)

DEFAULT_ENGINE = "whisper_cpp"
ENGINE_ALIASES = {"whisper": "whisper_cpp", "whispercpp": "whisper_cpp", "whisper_cpp": "whisper_cpp",
                  "faster_whisper": "faster_whisper", "fasterwhisper": "faster_whisper", "google": "google"}
FALLBACK_OFF = {"off", "no", "false", "0", "none", "нет"}
PROMPT_OFF = FALLBACK_OFF      # те же слова: off, no, false, 0, none
DEFAULT_THREADS = 4
DEFAULT_UNLOAD_MINUTES = 10


@dataclass
class SttSettings:
    engine: str
    fallback: bool
    model: str
    language: str
    prompt: bool
    threads: int
    unload_minutes: float


def models_dir():
    return storage.DATA_DIR / "models"


def stt_settings():
    raw = (env_value("STT_ENGINE", DEFAULT_ENGINE) or DEFAULT_ENGINE).strip().lower().replace("-", "_")
    engine = ENGINE_ALIASES.get(raw)
    if engine is None:
        log.warning("STT_ENGINE=%s не поддерживается (допустимо whisper_cpp, faster_whisper, google), использую %s",
                    raw, DEFAULT_ENGINE)
        engine = DEFAULT_ENGINE
    fallback = (env_value("STT_FALLBACK", "google") or "google").strip().lower() not in FALLBACK_OFF
    half = max(1, (os.cpu_count() or 2) // 2)
    wanted = int(env_float("STT_THREADS", DEFAULT_THREADS, 1, 64))
    if wanted > half:
        log.info("STT_THREADS=%d ограничено половиной ядер: %d", wanted, half)
    prompt = (env_value("STT_PROMPT", "on") or "on").strip().lower() not in PROMPT_OFF
    return SttSettings(engine=engine, fallback=fallback, model=env_value("STT_MODEL", "") or "",
                       language=env_value("STT_LANGUAGE", "ru") or "ru", prompt=prompt, threads=min(wanted, half),
                       unload_minutes=env_float("STT_UNLOAD_MINUTES", DEFAULT_UNLOAD_MINUTES, 0, 1440))


def build_stt(recognizer, notify=None, settings=None):
    """SpeechToText по настройкам. Ничего не загружает: загрузку модели запускает `SpeechToText.start()`."""
    settings = settings or stt_settings()
    google = GoogleEngine(recognizer, settings.language)
    if settings.engine == "google":
        return SpeechToText(google, None, None, notify)
    engines = {"whisper_cpp": WhisperCppEngine, "faster_whisper": FasterWhisperEngine}
    primary = engines[settings.engine](model=settings.model, language=settings.language, threads=settings.threads,
                                       unload_minutes=settings.unload_minutes, models_dir=models_dir())
    return SpeechToText(primary, google if settings.fallback else None, PromptProvider() if settings.prompt else None,
                        notify)
