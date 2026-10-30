"""Общие типы озвучки и настройки TTS из .env."""
import logging
import os
from dataclasses import dataclass, field

from core.settings import env_value

log = logging.getLogger(__name__)

DEFAULT_ENGINE = "say"
ENGINES = ("say", "sherpa", "silero")
SILERO_VOICES = ("eugene", "aidar")           # голоса Silero v5_ru, подходящие Jarvis (мужские); первый — по умолчанию
DEFAULT_UNLOAD_MINUTES = 10
SPEED_RANGE = (0.8, 1.5)
PITCH_RANGE = (-6.0, 0.0)
REVERB_RANGE = (0.0, 0.3)
DEFAULT_SPEED, DEFAULT_THREADS, DEFAULT_PITCH, DEFAULT_REVERB = 1.0, 2, -2.0, 0.12


class TTSUnavailable(Exception):
    """Нейронный голос не может работать: нет пакета или модели (kind="not_installed") либо он не запустился ("failed")."""

    def __init__(self, reason, kind="not_installed"):
        super().__init__(reason)
        self.reason, self.kind = reason, kind


@dataclass
class NeuralResult:
    """Итог речи нейронного движка: что читали, сколько предложений прозвучало, чем кончилось, хвост эффекта (секунды)."""
    sentences: list = field(default_factory=list)
    done: int = 0
    error: bool = False
    stopped: bool = False
    tail: float = 0.0


class TTSEngine:
    """Движок озвучки. speak(text) блокирует до конца речи; stop() прерывает её. local=True — всё остаётся на Mac."""
    name = ""
    local = True
    tail_seconds = 0.0       # хвост эффекта (реверберация): SpeechEngine прибавляет его к концу речи

    def speak(self, text):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError


@dataclass
class TTSSettings:
    engine: str = DEFAULT_ENGINE
    voice: str = ""
    speed: float = DEFAULT_SPEED
    threads: int = DEFAULT_THREADS
    effect: bool = False
    pitch: float = DEFAULT_PITCH
    reverb: float = DEFAULT_REVERB
    unload_minutes: int = DEFAULT_UNLOAD_MINUTES      # Silero: через сколько минут простоя воркер завершается (0 — не завершать)
    silero_python: str = ""                           # пустое — по умолчанию (factory.silero_python)
    silero_model: str = ""


def _number(name, default, lo, hi, path=None):
    """Число из .env в пределах [lo, hi]. Не число — default, за границами — ближайшая граница; оба случая с предупреждением
    в лог (само значение — настройка, не речь). Знак «минус» Unicode принимается как обычный."""
    raw = env_value(name, None, path)
    if raw is None:
        return default
    try:
        value = float(raw.replace("−", "-").replace(",", "."))
    except ValueError:
        log.warning("%s=%s не число, использую %s", name, raw, default)
        return default
    if not lo <= value <= hi:
        clamped = max(lo, min(hi, value))
        log.warning("%s=%s вне пределов %s…%s, использую %s", name, raw, lo, hi, clamped)
        return clamped
    return value


def tts_settings(path=None):
    engine = (env_value("TTS_ENGINE", DEFAULT_ENGINE, path) or DEFAULT_ENGINE).strip().lower()
    if engine not in ENGINES:
        log.warning("TTS_ENGINE=%s не поддерживается (допустимо say, sherpa, silero), использую %s", engine, DEFAULT_ENGINE)
        engine = DEFAULT_ENGINE
    effect_raw = (env_value("TTS_EFFECT", "off", path) or "off").strip().lower()
    if effect_raw not in ("off", "jarvis"):
        log.warning("TTS_EFFECT=%s не поддерживается (допустимо off, jarvis), использую off", effect_raw)
        effect_raw = "off"
    half = max(1, (os.cpu_count() or 2) // 2)
    threads = int(_number("TTS_THREADS", DEFAULT_THREADS, 1, 64, path))
    if threads > half:
        log.info("TTS_THREADS=%d ограничено половиной ядер: %d", threads, half)
    voice = (env_value("TTS_VOICE", "", path) or "").strip()
    speed = _number("TTS_SPEED", DEFAULT_SPEED, *SPEED_RANGE, path)
    if engine == "silero":
        if voice.lower() not in SILERO_VOICES:
            if voice:
                log.warning("TTS_VOICE=%s: для Silero допустимы %s, использую %s", voice, ", ".join(SILERO_VOICES),
                            SILERO_VOICES[0])
            voice = SILERO_VOICES[0]
        voice = voice.lower()
        if speed != DEFAULT_SPEED:
            log.warning("TTS_SPEED=%s: Silero скорость не меняет, использую %s", speed, DEFAULT_SPEED)
            speed = DEFAULT_SPEED
    return TTSSettings(engine=engine, voice=voice, speed=speed, threads=min(threads, half),
                       effect=effect_raw == "jarvis", pitch=_number("TTS_PITCH", DEFAULT_PITCH, *PITCH_RANGE, path),
                       reverb=_number("TTS_REVERB", DEFAULT_REVERB, *REVERB_RANGE, path),
                       unload_minutes=int(_number("TTS_UNLOAD_MINUTES", DEFAULT_UNLOAD_MINUTES, 0, 240, path)),
                       silero_python=(env_value("TTS_SILERO_PYTHON", "", path) or "").strip(),
                       silero_model=(env_value("TTS_SILERO_MODEL", "", path) or "").strip())
