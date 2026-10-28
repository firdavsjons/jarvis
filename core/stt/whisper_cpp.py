"""whisper.cpp через pywhispercpp: Metal (GPU), колесо для Python 3.14 без сборки и без torch (≈17 МБ с зависимостями).

Модель — файл ggml в `~/Library/Application Support/Jarvis/models/ggml-<STT_MODEL>.bin` (по умолчанию
large-v3-turbo-q5_0, 574 МБ). Jarvis модель НЕ скачивает: pywhispercpp сам скачал бы ненайденную модель, поэтому ему
всегда отдаётся путь к существующему файлу. Нет файла — «не установлено».

Метрики: pywhispercpp отдаёт у куска только среднюю вероятность токенов (`probability`, с extract_probability=True),
no_speech_prob в Python не доступен. avg_logprob = ln(probability); отсечение по no_speech_prob делает сам whisper.cpp
(параметры no_speech_thold и logprob_thold), наш фильтр добавляет avg_logprob, чёрный список и длину записи.
"""
import importlib
import logging
import math
from pathlib import Path

from core.stt.base import Segment, Unavailable
from core.stt.whisper_local import LOGPROB_MIN, NO_SPEECH_MAX, LocalWhisper

log = logging.getLogger(__name__)


def _import_backend():
    try:
        return importlib.import_module("pywhispercpp.model")
    except ImportError as e:
        raise Unavailable("пакет pywhispercpp не установлен") from e


def _numpy():
    try:
        return importlib.import_module("numpy")
    except ImportError as e:
        raise Unavailable("пакет numpy не установлен") from e


class WhisperCppEngine(LocalWhisper):
    name = "whisper_cpp"
    DEFAULT_MODEL = "large-v3-turbo-q5_0"

    def model_path(self):
        name = self.model_name
        if Path(name).is_file():
            return Path(name)
        return Path(self.models_dir) / f"ggml-{name}.bin"

    def _load_model(self):
        backend = _import_backend()
        path = self.model_path()
        if not path.is_file():
            raise Unavailable(f"модель не найдена: {path}")
        log.info("Загружаю модель whisper.cpp: %s (потоков %d)", path.name, self.threads)
        return backend.Model(str(path), n_threads=self.threads, print_progress=False, print_realtime=False,
                             print_timestamps=False, redirect_whispercpp_logs_to=None)

    def _transcribe(self, model, pcm, prompt):
        np = _numpy()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        options = dict(language=self.language, n_threads=self.threads, extract_probability=True,
                       print_progress=False, print_realtime=False, print_timestamps=False,
                       no_speech_thold=NO_SPEECH_MAX, logprob_thold=LOGPROB_MIN)
        if prompt:
            options["initial_prompt"] = prompt
        return [self._segment(s) for s in model.transcribe(samples, **options)]

    @staticmethod
    def _segment(raw):
        probability = getattr(raw, "probability", None)
        logprob = None
        if isinstance(probability, (int, float)) and not math.isnan(probability) and probability > 0:
            logprob = math.log(probability)
        return Segment(text=raw.text, avg_logprob=logprob)
