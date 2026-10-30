"""Нейронный голос Silero v5 (eugene, aidar) в ОТДЕЛЬНОМ процессе (Этап 9.4).

Модуль не импортирует torch, numpy, scipy и silero: всё тяжёлое живёт в воркере `silero_worker.py`, который запускается
интерпретатором отдельного окружения (`TTS_SILERO_PYTHON`, по умолчанию `~/Library/Application Support/Jarvis/tts_env`).
Проект к нему обращается JSON-строками через stdin/stdout:
    запрос  {"id", "text", "voice"}  →  ответ {"id", "wav": путь, "seconds", "peak"} или {"id", "error": класс ошибки}
    строка {"ready": true, "torch": …, "wav_dir": …} приходит один раз после загрузки модели;
    {"fatal": класс} — модель не загрузилась, процесс завершается.

Жизнь воркера:
- запускается в фоне при start_loading() (как Whisper) и лениво при речи, если его нет (после выгрузки или падения);
- речь, начавшая запуск, ждёт готовности до READY_WAIT (5 с), дальше эту фразу читает say, а воркер догружается;
- после `TTS_UNLOAD_MINUTES` простоя (0 — никогда) завершается и освобождает память (около 0,9 ГБ);
- упал, не ответил за SYNTH_TIMEOUT (10 с на предложение) или не загрузился — перезапуск не чаще раза в RESTART_GAP (60 с),
  а три запуска подряд без готовности — «Нейронный голос не запустился» (unavailable) до перезапуска Jarvis;
- запрос идёт по одному; ответ принимается, только если WAV лежит в папке воркера (она сообщается в `ready`).

Состояния как у SherpaEngine: idle → loading → ready | unavailable. «ready» — установлен и не отказал окончательно (сам
воркер может быть выгружен: тогда speak() его запустит). Нет интерпретатора, воркера или модели — TTSUnavailable
("не установлено"), говорит say. Текст речи в лог не пишется; воркер получает только JSON-строки запросов, окружение процесса
очищено (ключи API из окружения терминала в него не попадают). Модели Silero — CC BY-NC-SA 4.0 (docs/tts.md).
"""
import atexit
import itertools
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

from core import storage
from core.tts import effect, pronounce
from core.tts.base import NeuralResult, TTSEngine, TTSUnavailable
from core.tts.player import AfplayPlayer, SentenceQueue, split_sentences

log = logging.getLogger(__name__)

WORKER_SCRIPT = Path(__file__).with_name("silero_worker.py")
READY_WAIT = 5.0              # сколько речь ждёт воркера, который она же и запустила, потом — say
LOAD_TIMEOUT = 180.0          # загрузка модели при старте (первая, «холодная» — до ~40 с)
SYNTH_TIMEOUT = 10.0          # на одно предложение
RESTART_GAP = 60.0            # перезапуск после падения — не чаще
MAX_START_FAILURES = 3        # запусков подряд без готовности до «не запустился»
WATCH_INTERVAL = 30.0
PAUSE_SECONDS = 0.12          # пауза между предложениями в образцах (как в core.tts.check)
WORKER_TAG = "[silero-worker] "
WAV_DIR_PREFIX = "jarvis-silero-"


def default_python():
    return storage.DATA_DIR / "tts_env" / "bin" / "python"


def default_model():
    return storage.DATA_DIR / "models" / "tts" / "silero" / "v5_ru.pt"


def _spawn_worker(args, env):
    """Запуск процесса воркера (список аргументов, без shell). Тесты подменяют; настоящий запуск в них запрещён."""
    return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", bufsize=1, env=env, close_fds=True)


def worker_environment(threads):
    """Чистое окружение воркера: только то, что нужно интерпретатору (без ключей из окружения терминала)."""
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "LANG": "en_US.UTF-8", "OMP_NUM_THREADS": str(threads)}
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


class NotReady(Exception):
    """Воркер сейчас не может принять речь (грузится, ждёт паузы перед перезапуском, не установлен)."""


class _Worker:
    """Процесс воркера и два читающих потока (stdout — протокол, stderr — служебные строки без текста)."""

    def __init__(self, proc, started):
        self.proc, self.started = proc, started
        self.info, self.fatal, self.responses = {}, None, {}
        self.ready, self.dead = False, False
        self.cond = threading.Condition()
        self.unknown_stderr = 0
        for target, name in ((self._read_stdout, "silero-out"), (self._read_stderr, "silero-err")):
            threading.Thread(target=target, name=name, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in iter(self.proc.stdout.readline, ""):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                with self.cond:
                    if message.get("ready"):
                        self.info, self.ready = message, True
                    elif "fatal" in message:
                        self.fatal = message
                    elif "id" in message:
                        self.responses[message["id"]] = message
                    self.cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self.cond:
                self.dead = True
                self.cond.notify_all()

    def _read_stderr(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                if line.startswith(WORKER_TAG):
                    log.info("Silero: %s", line[len(WORKER_TAG):].strip())      # свои строки воркера: текста запросов в них нет
                else:
                    self.unknown_stderr += 1                                       # чужой вывод (трассировки) в лог не пишем
        except (OSError, ValueError):
            pass

    def alive(self):
        return not self.dead and self.proc.poll() is None

    def wait_ready(self, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.ready or self.dead, timeout)
            return self.ready

    def wait_response(self, request_id, timeout):
        with self.cond:
            self.cond.wait_for(lambda: request_id in self.responses or self.dead, timeout)
            return self.responses.pop(request_id, None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()


class SileroEngine(TTSEngine):
    name = "silero"
    local = True

    def __init__(self, voice="eugene", threads=2, python=None, model=None, worker=None, unload_minutes=10,
                 effect_on=False, pitch=-2.0, reverb=0.12, player=None, clock=time.monotonic, ready_wait=READY_WAIT,
                 synth_timeout=SYNTH_TIMEOUT, load_timeout=LOAD_TIMEOUT, restart_gap=RESTART_GAP, watch=True):
        self.voice, self.threads, self.unload_minutes = voice, threads, unload_minutes
        self.python = Path(python).expanduser() if python else default_python()
        self.model = Path(model).expanduser() if model else default_model()
        self.worker_script = Path(worker) if worker else WORKER_SCRIPT
        self.effect_on, self.pitch, self.reverb = effect_on, pitch, reverb
        self._clock, self.ready_wait, self.synth_timeout = clock, ready_wait, synth_timeout
        self.load_timeout, self.restart_gap, self._watch = load_timeout, restart_gap, watch
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._worker = None
        self._state = "idle"
        self.unavailable = None
        self.load_seconds = None
        self.warmup_rtf = None
        self.info = {}                    # последнее сообщение ready воркера (версии, потоки, голоса)
        self.tail_seconds = 0.0
        self.starts = 0                   # сколько раз запускали воркер (для проверки и лога)
        self._failures = 0
        self._allowed_at = 0.0
        self._last_used = 0.0
        self._first_phrase_logged = False
        self._watcher = None
        self._atexit = False
        self._queue = SentenceQueue(self._synthesize, player or AfplayPlayer())

    @property
    def state(self):
        return self._state

    # --- установка -------------------------------------------------------------------------------------------------
    def installation_problem(self):
        """TTSUnavailable("не установлено"), если нет интерпретатора, воркера или модели; иначе None."""
        if not (self.python.is_file() and os.access(self.python, os.X_OK)):
            return TTSUnavailable(f"интерпретатор Silero не найден: {self.python}")
        if not self.worker_script.is_file():
            return TTSUnavailable(f"нет файла воркера: {self.worker_script}")
        if not self.model.is_file():
            return TTSUnavailable(f"модель Silero не найдена: {self.model}")
        return None

    def _fail(self, error):
        with self._lock:
            self._state, self.unavailable = "unavailable", error
            worker, self._worker = self._worker, None
        if worker is not None:
            self._terminate(worker, graceful=False)
        log.warning("Нейронный голос недоступен: %s", error.reason)

    # --- загрузка и жизнь процесса ---------------------------------------------------------------------------------
    def start_loading(self):
        """Фоновый запуск воркера; повторный вызов ничего не делает."""
        with self._lock:
            if self._state != "idle":
                return
            self._state = "loading"
        threading.Thread(target=self.load, name="tts-load", daemon=True).start()

    def load(self):
        """Запускает воркер и ждёт готовности (вызывается из start_loading; в `core.tts.check` — напрямую)."""
        problem = self.installation_problem()
        if problem:
            self._fail(problem)
            return
        with self._lock:
            if self._state == "unavailable":
                return
            self._state = "loading"
        started = self._clock()
        try:
            worker = self._ensure_worker(wait=self.load_timeout)
        except NotReady:
            with self._lock:
                if self._state == "loading":
                    self._state = "ready"          # временный сбой: речь повторит запуск лениво, пока читает say
            return
        with self._lock:
            if self._state != "unavailable":
                self._state, self.load_seconds = "ready", self._clock() - started
        log.info("Нейронный голос Silero (%s) загружен за %.1f с (воркер сообщил %.1f с, torch %s, потоков %s)", self.voice,
                 self.load_seconds or 0.0, float(worker.info.get("load_seconds", 0.0)), worker.info.get("torch", "?"),
                 worker.info.get("threads", "?"))

    def _launch(self):
        command = [str(self.python), "-I", str(self.worker_script), str(self.model), self.voice, str(self.threads)]
        try:
            proc = _spawn_worker(command, worker_environment(self.threads))
        except OSError as e:
            log.warning("Silero: воркер не запустился (%s)", type(e).__name__)
            self._note_failure()
            raise NotReady("запуск не удался") from e
        now = self._clock()
        worker = _Worker(proc, now)
        self._worker, self._last_used = worker, now
        self.starts += 1
        self._first_phrase_logged = False
        if not self._atexit:
            atexit.register(self.close)
            self._atexit = True
        if self._watch and (self._watcher is None or not self._watcher.is_alive()):
            self._watcher = threading.Thread(target=self._watch_loop, name="tts-silero-watch", daemon=True)
            self._watcher.start()
        return worker

    def _note_failure(self):
        """Запуск без готовности. Три подряд — голос не запустился (до перезапуска Jarvis)."""
        self._failures += 1
        if self._failures >= MAX_START_FAILURES:
            self._fail(TTSUnavailable(f"воркер Silero не запускается ({self._failures} раза подряд)", kind="failed"))

    def _drop(self, worker, crashed):
        """Убирает воркер. crashed — упал/завис: следующий запуск не раньше чем через restart_gap после этого."""
        with self._lock:
            if self._worker is worker:
                self._worker = None
            if crashed:
                self._allowed_at = worker.started + self.restart_gap
                if not worker.ready:
                    if worker.fatal:
                        log.warning("Silero: воркер не загрузился (%s%s)", worker.fatal.get("fatal"),
                                    f", нет пакета {worker.fatal['missing']}" if worker.fatal.get("missing") else "")
                    self._note_failure()
                else:
                    log.warning("Silero: воркер завершился или завис, перезапуск не раньше чем через %.0f с", self.restart_gap)
        self._terminate(worker, graceful=False)

    def _terminate(self, worker, graceful):
        """graceful — штатно: закрытый stdin, воркер завершается сам. Иначе (завис, упал, отказ) — сразу terminate."""
        proc = worker.proc
        if graceful:
            self._close_stdin(proc)                             # закрытый stdin — штатное завершение воркера
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._close_stdin(proc)
        wav_dir = worker.info.get("wav_dir")
        if wav_dir and os.path.basename(str(wav_dir)).startswith(WAV_DIR_PREFIX) and not os.path.islink(wav_dir):
            shutil.rmtree(wav_dir, ignore_errors=True)         # воркер убрал бы её сам; страхуемся, если его убили жёстко

    @staticmethod
    def _close_stdin(proc):
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _ensure_worker(self, wait):
        """Готовый воркер. Если процесса нет — запускает его и ждёт готовности до `wait` секунд; уже грузящийся не ждём
        (речь тогда сразу уходит в say). Иначе NotReady."""
        with self._lock:
            if self._state == "unavailable":
                raise NotReady("голос недоступен")
            worker = self._worker
            if worker is not None and not worker.alive():
                self._drop(worker, crashed=True)
                worker = None
            launched = False
            if worker is None:
                if self._clock() < self._allowed_at:
                    raise NotReady("перезапуск не раньше, чем через минуту после падения")
                problem = self.installation_problem()
                if problem:
                    self._fail(problem)
                    raise NotReady("не установлено")
                worker = self._launch()
                launched = True
        if not worker.wait_ready(wait if launched else 0):
            if worker.dead or not worker.alive():
                self._drop(worker, crashed=True)
            elif launched and wait >= self.load_timeout:
                log.warning("Silero: модель не загрузилась за %.0f с", wait)
                self._drop(worker, crashed=True)
            raise NotReady("воркер ещё грузится")
        with self._lock:
            if self.info is not worker.info:
                self._last_used = self._clock()            # простой считается с готовности, а не с запуска
            self.info, self._failures = worker.info, 0
            if self._state in ("idle", "loading"):
                self._state = "ready"                      # воркер ответил: голос можно использовать
        return worker

    def check_idle(self):
        """Завершает воркер после простоя TTS_UNLOAD_MINUTES. True — завершил."""
        if not self.unload_minutes:
            return False
        with self._lock:
            worker = self._worker
            if worker is None or not worker.ready or self._clock() - self._last_used < self.unload_minutes * 60:
                return False
        if not self._request_lock.acquire(blocking=False):
            return False                                       # идёт синтез
        try:
            with self._lock:
                if self._worker is not worker:
                    return False
                self._worker, self._allowed_at = None, 0.0
            log.info("Silero: воркер выгружен после %d мин простоя", self.unload_minutes)
            self._terminate(worker, graceful=True)
            return True
        finally:
            self._request_lock.release()

    def _watch_loop(self):
        while True:
            time.sleep(WATCH_INTERVAL)
            with self._lock:
                if self._worker is None:
                    self._watcher = None
                    return
            self.check_idle()

    def close(self):
        """Завершает воркер (выход из Jarvis, проверка)."""
        with self._lock:
            worker, self._worker = self._worker, None
        if worker is not None:
            self._terminate(worker, graceful=True)

    # --- запросы ---------------------------------------------------------------------------------------------------
    def request(self, text):
        """Один синтез: ответ воркера {"wav", "seconds", "peak", …}. Ошибка воркера, таймаут, нет воркера — исключение."""
        with self._request_lock:
            worker = self._ensure_worker(wait=self.ready_wait)
            request_id = next(self._ids)
            begun = self._clock()
            try:
                worker.send({"id": request_id, "text": text, "voice": self.voice})
            except (OSError, ValueError):
                self._drop(worker, crashed=True)
                raise RuntimeError("воркер не принимает запросы") from None
            response = worker.wait_response(request_id, self.synth_timeout)
            if response is None:
                if worker.dead:
                    log.warning("Silero: воркер завершился во время синтеза")
                else:
                    log.warning("Silero: синтез дольше %.1f с, воркер перезапускается", self.synth_timeout)
                self._drop(worker, crashed=True)
                raise TimeoutError("синтез не уложился в срок")
            with self._lock:
                self._last_used = self._clock()
            if "error" in response:
                raise RuntimeError(f"ошибка воркера: {response['error']}")
            if not self._first_phrase_logged:
                self._first_phrase_logged = True
                seconds = float(response.get("seconds") or 0.0)
                elapsed = self._clock() - begun
                if seconds > 0:
                    log.info("Silero: первая фраза после запуска: синтез %.2f с, звук %.2f с, RTF %.2f", elapsed, seconds,
                             elapsed / seconds)
            return response

    def wav_path(self, response):
        """Путь к WAV из ответа — только внутри папки воркера."""
        path, folder = response.get("wav"), self.info.get("wav_dir")
        if not isinstance(path, str) or not isinstance(folder, str):
            raise RuntimeError("воркер не вернул файл")
        real = os.path.realpath(path)
        if os.path.dirname(real) != os.path.realpath(folder) or not real.endswith(".wav") or not os.path.isfile(real):
            raise RuntimeError("воркер вернул недопустимый путь")
        return real

    def read_wav(self, path):
        """(samples float64 в [-1, 1], rate) из WAV воркера; файл удаляется."""
        np = effect.numpy_module()
        try:
            with wave.open(path, "rb") as wav:
                frames, rate = wav.readframes(wav.getnframes()), wav.getframerate()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0, rate

    def _synthesize(self, sentence):
        path = self.wav_path(self.request(sentence))
        if not self.effect_on:
            return path                                        # очередь проиграет файл и удалит его
        samples, rate = self.read_wav(path)
        samples, tail = effect.apply_effect(samples, rate, self.pitch, self.reverb)
        self.tail_seconds = tail
        return samples, rate

    # --- речь ------------------------------------------------------------------------------------------------------
    def speak(self, text):
        sentences = split_sentences(pronounce.apply(text))      # английские названия — русской записью (только для Silero)
        if not sentences:
            return NeuralResult()
        self.tail_seconds = 0.0
        result = self._queue.speak(sentences)
        tail = self.tail_seconds if result.done else 0.0
        return NeuralResult(sentences=sentences, done=result.done, error=result.error, stopped=result.stopped, tail=tail)

    def stop(self):
        self._queue.stop()
