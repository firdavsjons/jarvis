"""Локальный Whisper: общая часть двух движков (whisper_cpp.py и faster.py).

Модель грузится в фоновом потоке при запуске (`start_loading`) с прогревом на тихом фрагменте и выгружается из памяти
после STT_UNLOAD_MINUTES минут простоя; следующая фраза загрузит её снова (звук при этом остаётся на Mac). Конкретный
движок реализует три крючка: `_load_model`, `_transcribe`, `_free_model`.

Фильтр галлюцинаций (по каждому куску речи): no_speech_prob > 0.6 или avg_logprob < -1.0 или текст из чёрного списка
(blacklist.py) — кусок отбрасывается; не осталось ни одного — результат «шум», текст пуст.
"""
import gc
import logging
import math
import threading
import time

from core.stt.base import Engine, Loading, Recognition, Segment, SttError, Unavailable
from core.stt.blacklist import is_blacklisted

log = logging.getLogger(__name__)

NO_SPEECH_MAX = 0.6
LOGPROB_MIN = -1.0
WARMUP_SECONDS = 0.5      # тихий фрагмент для прогрева (первый запуск Metal / ядер заметно медленнее)


def filter_segments(segments):
    """(оставшиеся куски, причина отказа или None). Каждый критерий применяется к каждому куску отдельно."""
    kept = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        if segment.no_speech_prob is not None and segment.no_speech_prob > NO_SPEECH_MAX:
            continue
        if segment.avg_logprob is not None and segment.avg_logprob < LOGPROB_MIN:
            continue
        if is_blacklisted(text):
            continue
        kept.append(segment)
    text = " ".join(s.text.strip() for s in kept)
    if kept and is_blacklisted(text):      # фраза, разбитая на несколько кусков
        return [], "noise"
    if not kept and any(s.text.strip() for s in segments):
        return [], "noise"
    return kept, None


def confidence_of(segments):
    values = [math.exp(s.avg_logprob) for s in segments if s.avg_logprob is not None]
    return sum(values) / len(values) if values else None


class LocalWhisper(Engine):
    local = True
    uses_prompt = True
    DEFAULT_MODEL = ""

    def __init__(self, model="", language="ru", threads=4, unload_minutes=10, models_dir=None, clock=time.monotonic):
        self.model_name = model or self.DEFAULT_MODEL
        self.language = language
        self.threads = threads
        self.unload_seconds = max(0.0, float(unload_minutes)) * 60
        self.models_dir = models_dir
        self._clock = clock
        self._lock = threading.RLock()        # состояние и загрузка
        self._run_lock = threading.Lock()     # одно распознавание за раз; выгрузка ждёт его конца
        self._model = None
        self._state = "idle"                  # idle (не загружена) | loading | ready | unavailable
        self._timer = None
        self._last_used = clock()
        self.unavailable = None               # Unavailable, если state == unavailable
        self.load_seconds = None

    # --- крючки движка -------------------------------------------------------------------------------------------
    def _load_model(self):
        """Загруженная модель. Нет пакета или файла модели — Unavailable("not_installed"). Скачивать нельзя."""
        raise NotImplementedError

    def _transcribe(self, model, pcm, prompt):
        """[Segment] для PCM 16 кГц / 16 бит / моно."""
        raise NotImplementedError

    def _free_model(self, model):
        """Освободить ресурсы модели (кроме удаления ссылки)."""

    # --- состояние -----------------------------------------------------------------------------------------------
    @property
    def state(self):
        return self._state

    def start_loading(self):
        """Загрузка в фоновом потоке; повторный вызов ничего не делает."""
        with self._lock:
            if self._state != "idle":
                return
            self._state = "loading"
        threading.Thread(target=self._load, name="stt-load", daemon=True).start()

    def ensure_loaded(self):
        """Готова — вернуться; не загружена (выгружена после простоя) — загрузить здесь же; грузится в фоне — Loading;
        недоступна — Unavailable."""
        with self._lock:
            state = self._state
            if state == "idle":
                self._state = "loading"
            elif state == "loading":
                raise Loading("модель загружается")
            elif state == "unavailable":
                raise self.unavailable
            else:
                return
        self._load()
        if self._state == "unavailable":
            raise self.unavailable

    def _load(self):
        started = self._clock()
        try:
            model = self._load_model()
            self._warm_up(model)
        except Unavailable as e:
            self._fail(e)
        except Exception as e:      # битый файл модели, нет памяти, сбой Metal
            self._fail(Unavailable(f"модель не загрузилась: {type(e).__name__}", kind="failed"))
        else:
            with self._lock:
                self._model, self._state = model, "ready"
                self.load_seconds = self._clock() - started
                self._last_used = self._clock()
            log.info("Модель распознавания (%s, %s) загружена за %.1f с", self.name, self.model_name, self.load_seconds)
            self._schedule_unload()

    def _warm_up(self, model):
        silence = b"\x00\x00" * int(16000 * WARMUP_SECONDS)
        started = self._clock()
        self._transcribe(model, silence, "")
        log.info("Прогрев модели распознавания: %.1f с", self._clock() - started)

    def _fail(self, error):
        with self._lock:
            self._state, self.unavailable, self._model = "unavailable", error, None
        log.warning("Локальное распознавание недоступно (%s): %s", self.name, error.reason)

    # --- выгрузка после простоя ----------------------------------------------------------------------------------
    def _schedule_unload(self):
        if self.unload_seconds <= 0:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.unload_seconds, self.unload_if_idle)
            self._timer.daemon = True
            self._timer.start()

    def unload_if_idle(self):
        """Выгружает модель, если она не нужна STT_UNLOAD_MINUTES минут. Идёт распознавание — не трогает."""
        if self.unload_seconds <= 0 or not self._run_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if self._state != "ready":
                    return False
                idle = self._clock() - self._last_used
                if idle < self.unload_seconds:
                    self._schedule_unload_after(self.unload_seconds - idle)
                    return False
                model, self._model, self._state = self._model, None, "idle"
        finally:
            self._run_lock.release()
        self._free_model(model)
        del model
        gc.collect()
        log.info("Модель распознавания выгружена из памяти (простой %.0f мин)", self.unload_seconds / 60)
        return True

    def _schedule_unload_after(self, seconds):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(max(1.0, seconds), self.unload_if_idle)
        self._timer.daemon = True
        self._timer.start()

    # --- распознавание -------------------------------------------------------------------------------------------
    def recognize(self, audio, prompt=""):
        self.ensure_loaded()
        with self._run_lock:
            model = self._model
            if model is None:
                raise Loading("модель выгружена")
            started = self._clock()
            try:
                segments = self._transcribe(model, audio.pcm16k(), prompt)
            except Exception as e:
                raise SttError(f"{self.name} не сработал: {type(e).__name__}") from e
            elapsed = self._clock() - started
        with self._lock:
            self._last_used = self._clock()
        kept, reason = filter_segments(segments)
        text = " ".join(s.text.strip() for s in kept).strip()
        return Recognition(text=text, engine=self.name, seconds=elapsed, audio_seconds=audio.seconds,
                           confidence=confidence_of(kept), discarded=reason)
