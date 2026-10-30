"""Нейронный голос: sherpa-onnx (offline TTS) с моделями Piper. Колесо для Python 3.14 без сборки и без torch.

Модель — папка из архива k2-fsa (например, vits-piper-ru_RU-denis-medium-fp16) в
`~/Library/Application Support/Jarvis/models/tts/<TTS_VOICE>/`: `*.onnx`, `tokens.txt`, `espeak-ng-data/`. Jarvis модель НЕ
скачивает. Нет пакета, папки или файлов — `TTSUnavailable("не установлено")`, говорит say.

Состояния как у локального Whisper: idle (не загружена) → loading (фон, при start_stt) → ready | unavailable. Прогрев —
короткая фраза, результат отбрасывается; время загрузки, первой фразы и RTF пишутся в лог без текста.
"""
import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.tts import effect
from core.tts.base import TTSEngine, TTSUnavailable
from core.tts.player import AfplayPlayer, PlayResult, SentenceQueue, split_sentences

log = logging.getLogger(__name__)

WARMUP_TEXT = "Привет."
SILENCE_SCALE = 0.2


def _import_backend():
    try:
        return importlib.import_module("sherpa_onnx")
    except ImportError as e:
        raise TTSUnavailable("пакет sherpa-onnx не установлен") from e


@dataclass
class NeuralResult:
    sentences: list = field(default_factory=list)
    done: int = 0
    error: bool = False
    stopped: bool = False
    tail: float = 0.0


class SherpaEngine(TTSEngine):
    name = "sherpa"
    local = True

    def __init__(self, voice="", speed=1.0, threads=2, effect_on=False, pitch=-2.0, reverb=0.12, models_dir=None,
                 player=None, clock=time.monotonic):
        self.voice, self.speed, self.threads = voice, speed, threads
        self.effect_on, self.pitch, self.reverb = effect_on, pitch, reverb
        self.models_dir = Path(models_dir) if models_dir else None
        self._clock = clock
        self._lock = threading.RLock()
        self._tts = None
        self._backend = None
        self._state = "idle"               # idle | loading | ready | unavailable
        self.unavailable = None
        self.load_seconds = None
        self.warmup_rtf = None
        self.tail_seconds = 0.0
        self._queue = SentenceQueue(self._synthesize, player or AfplayPlayer())

    @property
    def state(self):
        return self._state

    @property
    def folder(self):
        return None if self.models_dir is None or not self.voice else self.models_dir / self.voice

    # --- загрузка ------------------------------------------------------------------------------------------------
    def start_loading(self):
        """Фоновая загрузка и прогрев; повторный вызов ничего не делает."""
        with self._lock:
            if self._state != "idle":
                return
            self._state = "loading"
        threading.Thread(target=self.load, name="tts-load", daemon=True).start()

    def load(self):
        """Загружает и прогревает модель (вызывается из start_loading; в проверке `core.tts.check` — напрямую)."""
        started = self._clock()
        with self._lock:
            self._state = "loading"
        try:
            backend = _import_backend()
            tts = backend.OfflineTts(self.build_config(backend, self.model_files()))
            self._backend, self._tts = backend, tts
            warm = self._clock()
            samples, rate = self._generate(WARMUP_TEXT)
            if len(samples) and rate:
                self.warmup_rtf = (self._clock() - warm) / (len(samples) / float(rate))
        except TTSUnavailable as e:
            self._fail(e)
        except Exception as e:      # битая модель, нет памяти, несовместимый пакет
            self._fail(TTSUnavailable(f"голос не загрузился: {type(e).__name__}", kind="failed"))
        else:
            with self._lock:
                self._state, self.load_seconds = "ready", self._clock() - started
            log.info("Нейронный голос (%s) загружен за %.1f с, прогрев RTF %s", self.voice, self.load_seconds,
                     f"{self.warmup_rtf:.2f}" if self.warmup_rtf is not None else "—")

    def _fail(self, error):
        with self._lock:
            self._state, self.unavailable, self._tts = "unavailable", error, None
        log.warning("Нейронный голос недоступен: %s", error.reason)

    def model_files(self):
        """(model.onnx, tokens.txt, espeak-ng-data) в папке голоса или TTSUnavailable("не установлено")."""
        if not self.voice:
            raise TTSUnavailable("голос не выбран: впишите имя папки модели в TTS_VOICE")
        folder = self.folder
        if folder is None or not folder.is_dir():
            raise TTSUnavailable(f"папка голоса не найдена: {folder}")
        models = sorted(p for p in folder.glob("*.onnx") if p.is_file())
        tokens, data = folder / "tokens.txt", folder / "espeak-ng-data"
        if not models:
            raise TTSUnavailable(f"в папке {folder} нет файла модели .onnx")
        if not tokens.is_file():
            raise TTSUnavailable(f"в папке {folder} нет tokens.txt")
        if not data.is_dir():
            raise TTSUnavailable(f"в папке {folder} нет espeak-ng-data")
        return models[0], tokens, data

    def build_config(self, backend, files):
        model, tokens, data = files
        vits = backend.OfflineTtsVitsModelConfig(model=str(model), tokens=str(tokens), data_dir=str(data))
        config = backend.OfflineTtsConfig(
            model=backend.OfflineTtsModelConfig(vits=vits, num_threads=self.threads, provider="cpu"),
            max_num_sentences=1)
        validate = getattr(config, "validate", None)
        if callable(validate) and not validate():
            raise TTSUnavailable("файлы модели не прошли проверку sherpa-onnx", kind="failed")
        return config

    # --- синтез и речь -------------------------------------------------------------------------------------------
    def model_speed(self):
        """Скорость для модели: с эффектом ускорена на коэффициент понижения тона (длительность сохраняется)."""
        return effect.compensated_speed(self.speed, self.pitch) if self.effect_on else self.speed

    def _generate(self, sentence):
        config = self._backend.GenerationConfig()
        config.sid = 0
        config.speed = self.model_speed()
        config.silence_scale = SILENCE_SCALE
        audio = self._tts.generate(sentence, config)
        return audio.samples, audio.sample_rate

    def _synthesize(self, sentence):
        samples, rate = self._generate(sentence)
        if not len(samples):
            raise RuntimeError("пустой результат синтеза")
        if self.effect_on:
            samples, tail = effect.apply_effect(samples, rate, self.pitch, self.reverb)
            self.tail_seconds = tail
        return samples, rate

    def speak(self, text):
        sentences = split_sentences(text)
        if not sentences:
            return NeuralResult()
        self.tail_seconds = 0.0
        result = self._queue.speak(sentences)
        tail = self.tail_seconds if result.done else 0.0
        return NeuralResult(sentences=sentences, done=result.done, error=result.error, stopped=result.stopped, tail=tail)

    def stop(self):
        self._queue.stop()
