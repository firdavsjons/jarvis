"""faster-whisper (CTranslate2): запасной движок, Python 3.14 без сборки и без torch (≈63 МБ с зависимостями).

На Mac работает на процессоре (int8), без Metal, поэтому большая модель медленнее, чем в whisper.cpp; по умолчанию
small. Модель — папка формата CTranslate2 `~/Library/Application Support/Jarvis/models/faster-whisper/<STT_MODEL>`
(например, small). Jarvis модель НЕ скачивает: загрузка идёт с local_files_only=True, нет модели — «не установлено».
Даёт no_speech_prob и avg_logprob у каждого куска — все критерии фильтра работают полностью.
"""
import importlib
import logging
from pathlib import Path

from core.stt.base import Segment, Unavailable
from core.stt.whisper_local import LocalWhisper

log = logging.getLogger(__name__)


def _import_backend():
    try:
        return importlib.import_module("faster_whisper")
    except ImportError as e:
        raise Unavailable("пакет faster-whisper не установлен") from e


def _numpy():
    try:
        return importlib.import_module("numpy")
    except ImportError as e:
        raise Unavailable("пакет numpy не установлен") from e


class FasterWhisperEngine(LocalWhisper):
    name = "faster_whisper"
    DEFAULT_MODEL = "small"

    def _load_model(self):
        backend = _import_backend()
        root = Path(self.models_dir) / "faster-whisper"
        local = root / self.model_name
        target = str(local) if local.is_dir() else self.model_name     # папка с моделью или имя из кэша
        log.info("Загружаю модель faster-whisper: %s (потоков %d)", self.model_name, self.threads)
        try:
            return backend.WhisperModel(target, device="cpu", compute_type="int8", cpu_threads=self.threads,
                                        download_root=str(root), local_files_only=True)
        except OSError as e:      # модели нет в папке (LocalEntryNotFoundError — подкласс OSError)
            raise Unavailable(f"модель {self.model_name} не найдена в {root}") from e

    def _transcribe(self, model, pcm, prompt):
        np = _numpy()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = model.transcribe(samples, language=self.language, beam_size=1, vad_filter=False,
                                           condition_on_previous_text=False, initial_prompt=prompt or None)
        return [Segment(text=s.text, no_speech_prob=s.no_speech_prob, avg_logprob=s.avg_logprob) for s in segments]
