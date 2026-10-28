"""Общие типы распознавания речи: запись, результат, ошибки, интерфейс движка."""
from dataclasses import dataclass
from typing import Optional

SAMPLE_RATE = 16000     # Whisper принимает 16 кГц, 16 бит, моно
SAMPLE_WIDTH = 2
MIN_SECONDS = 0.4       # запись короче — щелчок или шум, а не команда


class SttError(Exception):
    """Техническая ошибка распознавания (исключение движка, нет сети): можно попробовать запасной движок."""


class Unavailable(SttError):
    """Движок не может работать: пакет или модель не установлены (kind="not_installed") либо модель не загрузилась
    ("failed"). Пока Jarvis не перезапущен, повторять загрузку бессмысленно."""

    def __init__(self, reason, kind="not_installed"):
        super().__init__(reason)
        self.reason, self.kind = reason, kind


class Loading(SttError):
    """Модель ещё загружается в фоне."""


@dataclass
class Segment:
    """Кусок распознанной речи с оценками движка (None — движок такой оценки не даёт)."""
    text: str
    no_speech_prob: Optional[float] = None
    avg_logprob: Optional[float] = None


@dataclass
class Recognition:
    text: str
    engine: str
    seconds: float = 0.0                     # сколько заняло распознавание
    audio_seconds: float = 0.0               # длина записи
    confidence: Optional[float] = None       # 0–1, exp(avg_logprob) по оставшимся кускам
    discarded: Optional[str] = None          # "noise": шум или галлюцинация (текст пуст)


class Audio:
    """Запись с микрофона: исходный sr.AudioData (для Google) и PCM 16 кГц / 16 бит / моно (для Whisper)."""

    def __init__(self, source):
        self.source = source
        self._pcm = None

    @property
    def seconds(self):
        data = self.source
        return len(data.frame_data) / float(data.sample_rate * data.sample_width)

    def pcm16k(self):
        if self._pcm is None:
            self._pcm = self.source.get_raw_data(convert_rate=SAMPLE_RATE, convert_width=SAMPLE_WIDTH)
        return self._pcm


class Engine:
    """Движок распознавания. local=True — звук остаётся на этом Mac."""
    name = ""
    local = False
    uses_prompt = False                      # понимает ли подсказку (initial_prompt); Google её не получает никогда

    def recognize(self, audio, prompt=""):
        raise NotImplementedError
