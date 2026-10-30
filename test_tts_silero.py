"""Этап 9.4: Silero в отдельном процессе. Настоящий воркер, torch и звук не нужны: процесс подменён фальшивкой, которая
эмулирует JSON-протокол (ready / запрос / ответ), WAV пишутся во временную папку тестов."""
import array
import ast
import importlib.util
import json
import logging
import math
import os
import queue
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков, загрузки моделей и записи в домашние папки
import numpy as np

from core.tts import check, pronounce
from core.tts import silero as silero_module
from core.tts.base import SILERO_VOICES, TTSSettings, tts_settings
from core.tts.player import SentenceQueue, TempWavs
from core.tts.factory import FAILED_NOTICE, NOT_INSTALLED_NOTICE, SpeechVoice, build_tts
from core.tts.silero import MAX_START_FAILURES, NotReady, SileroEngine, default_model, default_python, worker_environment
from test_tts import FakePlayer, FakeSay, env_file, wait_until

PROJECT = Path(__file__).resolve().parent
SECRET = "совершенносекретнаяфраза"
SECRET_KEY = "ключ-не-для-воркера-12345"


def make_wav(directory, seconds=0.2, peak=0.5, rate=48000):
    path = os.path.join(directory, secrets.token_hex(8) + ".wav")
    data = array.array("h", (int(peak * 32767 * math.sin(i / 7.0)) for i in range(int(rate * seconds))))
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data.tobytes())
    return path


class FakePipe:
    """stdout / stderr фальшивого процесса: readline блокирует, пока тест не положит строку; "" — конец потока."""

    def __init__(self):
        self.lines = queue.Queue()

    def readline(self):
        return self.lines.get()

    def push(self, line):
        self.lines.put(line)


class FakeStdin:
    def __init__(self, proc):
        self.proc, self.closed = proc, False

    def write(self, data):
        if self.closed:
            raise ValueError("write to closed file")
        for line in data.splitlines():
            self.proc.on_request(json.loads(line))

    def flush(self):
        pass

    def close(self):
        self.closed = True
        self.proc.exit(0)               # настоящий воркер при закрытом stdin завершается сам


class FakeProc:
    """Процесс воркера. mode: ok | hang (молчит) | error (ответ с ошибкой) | crash (умирает на запросе) | outside (WAV вне
    своей папки) | garbage (не JSON)."""

    def __init__(self, mode="ok", ready=True, fatal=None, info=None, peak=0.5):
        self.mode, self.peak = mode, peak
        self.stdout, self.stderr, self.stdin = FakePipe(), FakePipe(), FakeStdin(self)
        self.returncode, self.requests, self.terminated, self.killed = None, [], False, False
        self._exited = threading.Event()
        self.wav_dir = tempfile.mkdtemp(prefix="jarvis-silero-", dir=tests_base.TEMP_ROOT)
        self.info = {"ready": True, "torch": "2.15.0", "numpy": "2.5.4", "scipy": "1.18.1", "threads": 2, "load_seconds": 1.3,
                     "voices": ["aidar", "eugene"], "wav_dir": self.wav_dir, **(info or {})}
        self.stderr.push("[silero-worker] готов за 1.3 с, потоков torch 2\n")
        self.stderr.push("Traceback: " + SECRET + "\n")             # чужой вывод: в лог попадать не должен
        if fatal:
            self.emit({"fatal": fatal, "missing": "scipy" if fatal == "ModuleNotFoundError" else ""})
            self.exit(1)
        elif ready:
            self.become_ready()

    def become_ready(self):
        self.emit(self.info)

    def emit(self, message):
        self.stdout.push(json.dumps(message, ensure_ascii=False) + "\n")

    def on_request(self, request):
        self.requests.append(request)
        if self.mode == "hang":
            return
        if self.mode == "crash":
            self.exit(139)
            return
        if self.mode == "garbage":
            self.stdout.push("это не JSON\n")
            return
        if self.mode == "error":
            self.emit({"id": request["id"], "error": "RuntimeError"})
            return
        folder = self.wav_dir
        if self.mode == "outside":
            folder = tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)
        self.emit({"id": request["id"], "wav": make_wav(folder, peak=self.peak), "seconds": 0.2, "peak": self.peak,
                   "raw_peak": 1.04})

    def exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.stdout.push("")
            self.stderr.push("")
            self._exited.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("silero", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.exit(-15)

    def kill(self):
        self.killed = True
        self.exit(-9)


class FakeSpawn:
    """Подмена core.tts.silero._spawn_worker: запоминает аргументы и окружение, создаёт FakeProc по сценарию."""

    def __init__(self):
        self.calls, self.procs, self.script, self.fail_with = [], [], [], None

    def __call__(self, args, env):
        self.calls.append((list(args), dict(env)))
        if self.fail_with:
            raise self.fail_with
        proc = FakeProc(**(self.script.pop(0) if self.script else {}))
        self.procs.append(proc)
        return proc


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SileroCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT))
        self.python, self.model = self.home / "python", self.home / "v5_ru.pt"
        self.python.write_text("#!/bin/sh\n")
        self.python.chmod(0o755)
        self.model.write_bytes(b"x")
        self.spawn, self.clock, self.player = FakeSpawn(), FakeClock(), FakePlayer()
        patcher = patch("core.tts.silero._spawn_worker", self.spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, **kwargs):
        options = dict(voice="eugene", threads=2, python=self.python, model=self.model, unload_minutes=10, player=self.player,
                       clock=self.clock, ready_wait=0.4, synth_timeout=0.3, load_timeout=1.0, watch=False)
        options.update(kwargs)
        engine = SileroEngine(**options)
        self.addCleanup(engine.close)
        self.addCleanup(engine._queue.wavs.cleanup)
        return engine


# --- протокол ----------------------------------------------------------------------------------------------------------
class TestProtocol(SileroCase):
    def test_worker_is_started_with_an_argument_list_in_isolated_mode(self):
        engine = self.engine(voice="aidar", threads=3)
        engine.speak("Готово.")
        args, env = self.spawn.calls[0]
        self.assertEqual(args, [str(self.python), "-I", str(silero_module.WORKER_SCRIPT), str(self.model), "aidar", "3"])
        self.assertTrue(all(isinstance(a, str) for a in args))

    def test_worker_environment_is_clean(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": SECRET_KEY, "GROQ_API_KEY": SECRET_KEY, "TMPDIR": "/tmp/x/"}):
            env = worker_environment(2)
        self.assertNotIn(SECRET_KEY, " ".join(env.values()))
        self.assertEqual(set(env), {"PATH", "HOME", "LANG", "OMP_NUM_THREADS", "TMPDIR"})
        self.assertEqual(env["OMP_NUM_THREADS"], "2")
        engine = self.engine()
        with patch.dict(os.environ, {"GEMINI_API_KEY": SECRET_KEY}):
            engine.speak("Готово.")
        self.assertNotIn(SECRET_KEY, json.dumps(self.spawn.calls[0][1]))

    def test_request_response_and_playback(self):
        engine = self.engine()
        result = engine.speak("Готово. Закрыто приложение Safari.")
        self.assertEqual((result.done, result.error, result.stopped), (2, False, False))
        proc = self.spawn.procs[0]
        self.assertEqual([r["id"] for r in proc.requests], [1, 2])
        self.assertEqual({r["voice"] for r in proc.requests}, {"eugene"})
        self.assertEqual(set(proc.requests[0]), {"id", "text", "voice"})
        self.assertEqual(proc.requests[1]["text"], "Закрыто приложение Сафари.")           # словарь произношения применён
        self.assertEqual(len(self.player.played), 2)
        self.assertTrue(all(p["exists"] for p in self.player.played))
        self.assertTrue(all(p["path"].startswith(os.path.realpath(proc.wav_dir)) for p in self.player.played))
        self.assertEqual(os.listdir(proc.wav_dir), [])                                      # проигранные WAV удалены

    def test_ready_message_is_kept_for_the_check_tool(self):
        engine = self.engine()
        engine.load()
        self.assertEqual(engine.info["torch"], "2.15.0")
        self.assertEqual(engine.info["threads"], 2)
        self.assertEqual(engine.state, "ready")
        self.assertIsNotNone(engine.load_seconds)

    def test_wav_outside_the_worker_folder_is_refused(self):
        self.spawn.script.append({"mode": "outside"})
        engine = self.engine()
        result = engine.speak("Готово.")
        self.assertTrue(result.error)
        self.assertEqual(result.done, 0)
        self.assertEqual(self.player.played, [])

    def test_non_json_lines_from_the_worker_are_ignored(self):
        self.spawn.script.append({"mode": "garbage"})
        engine = self.engine()
        result = engine.speak("Готово.")
        self.assertTrue(result.error)                                       # ответа нет -> таймаут, а не падение чтения

    def test_empty_text_makes_no_request(self):
        engine = self.engine()
        self.assertEqual(engine.speak("   ").sentences, [])
        self.assertEqual(self.spawn.calls, [])

    def test_effect_reads_the_wav_and_removes_it(self):
        engine = self.engine(effect_on=True, pitch=-2.0, reverb=0.1)
        result = engine.speak("Готово.")
        self.assertEqual(result.done, 1)
        self.assertGreater(result.tail, 0)
        self.assertEqual(os.listdir(self.spawn.procs[0].wav_dir), [])
        self.assertEqual(len(self.player.played), 1)                         # проиграл уже новый файл очереди


# --- запуск, готовность, выгрузка -------------------------------------------------------------------------------------
class TestLifecycle(SileroCase):
    def test_lazy_start_nothing_runs_until_the_first_speech(self):
        engine = self.engine()
        self.assertEqual((engine.state, self.spawn.calls), ("idle", []))
        engine.speak("Готово.")
        self.assertEqual(len(self.spawn.calls), 1)
        engine.speak("Слушаю.")
        self.assertEqual(len(self.spawn.calls), 1)                           # тот же воркер

    def test_background_start_loads_once(self):
        engine = self.engine()
        engine.start_loading()
        engine.start_loading()
        self.assertTrue(wait_until(lambda: engine.state == "ready"))
        self.assertEqual(len(self.spawn.calls), 1)
        engine.speak("Готово.")
        self.assertEqual(len(self.spawn.calls), 1)

    def test_speech_waits_for_a_worker_it_started_then_says_goes_on(self):
        self.spawn.script.append({"ready": False})
        engine = self.engine(ready_wait=0.15)
        begun = time.monotonic()
        result = engine.speak("Готово.")
        self.assertLess(time.monotonic() - begun, 1.0)
        self.assertTrue(result.error)
        self.assertEqual(result.done, 0)                                      # SpeechVoice прочитает фразу say
        proc = self.spawn.procs[0]
        self.assertIsNone(proc.returncode)                                    # воркер не убит: догружается
        proc.become_ready()
        self.assertTrue(wait_until(lambda: engine._worker.ready))
        self.assertEqual(engine.speak("Слушаю.").done, 1)
        self.assertEqual(len(self.spawn.calls), 1)

    def test_a_loading_worker_started_earlier_is_not_waited_for(self):
        self.spawn.script.append({"ready": False})
        engine = self.engine(ready_wait=5.0)
        with self.assertRaises(NotReady):
            engine._ensure_worker(wait=0.05)                                  # запуск и короткое ожидание
        begun = time.monotonic()
        self.assertTrue(engine.speak("Готово.").error)
        self.assertLess(time.monotonic() - begun, 1.0)                        # не ждали 5 с: воркер уже грузится

    def test_idle_unload_and_lazy_restart(self):
        engine = self.engine(unload_minutes=10)
        engine.speak("Готово.")
        proc = self.spawn.procs[0]
        self.clock.advance(599)
        self.assertFalse(engine.check_idle())
        self.assertIsNone(proc.returncode)
        self.clock.advance(2)
        self.assertTrue(engine.check_idle())
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(proc.stdin.closed)                                    # штатно: закрытым stdin
        self.assertFalse(proc.terminated)
        self.assertFalse(os.path.exists(proc.wav_dir))                        # папка воркера убрана
        self.assertEqual(engine.state, "ready")                               # голос остаётся доступным
        self.assertEqual(engine.speak("Слушаю.").done, 1)                     # воркер запущен заново, без паузы 60 с
        self.assertEqual(len(self.spawn.calls), 2)

    def test_unload_minutes_zero_never_unloads(self):
        engine = self.engine(unload_minutes=0)
        engine.speak("Готово.")
        self.clock.advance(10 ** 6)
        self.assertFalse(engine.check_idle())
        self.assertIsNone(self.spawn.procs[0].returncode)

    def test_use_postpones_the_unload(self):
        engine = self.engine(unload_minutes=10)
        engine.speak("Готово.")
        self.clock.advance(500)
        engine.speak("Слушаю.")
        self.clock.advance(500)
        self.assertFalse(engine.check_idle())
        self.clock.advance(101)
        self.assertTrue(engine.check_idle())

    def test_busy_worker_is_not_unloaded(self):
        engine = self.engine(unload_minutes=1)
        engine.speak("Готово.")
        self.clock.advance(3600)
        with engine._request_lock:
            self.assertFalse(engine.check_idle())
        self.assertTrue(engine.check_idle())

    def test_close_stops_the_worker(self):
        engine = self.engine()
        engine.speak("Готово.")
        engine.close()
        self.assertIsNotNone(self.spawn.procs[0].returncode)


class TestRestartRules(SileroCase):
    def test_restart_after_a_crash_not_more_often_than_once_a_minute(self):
        self.spawn.script.append({"mode": "crash"})
        engine = self.engine()
        first = engine.speak("Готово.")
        self.assertTrue(first.error)
        self.assertEqual(len(self.spawn.calls), 1)
        self.clock.advance(10)
        second = engine.speak("Готово.")
        self.assertTrue(second.error)
        self.assertEqual(len(self.spawn.calls), 1)                            # слишком рано: говорит say
        self.clock.advance(51)                                                # с запуска прошло 61 с
        third = engine.speak("Готово.")
        self.assertEqual((third.error, third.done), (False, 1))
        self.assertEqual(len(self.spawn.calls), 2)
        self.assertEqual(engine.state, "ready")

    def test_timeout_kills_the_worker_and_follows_the_same_rule(self):
        self.spawn.script.append({"mode": "hang"})
        engine = self.engine(synth_timeout=0.2)
        begun = time.monotonic()
        result = engine.speak("Готово.")
        self.assertLess(time.monotonic() - begun, 2.0)
        self.assertTrue(result.error)
        proc = self.spawn.procs[0]
        self.assertTrue(proc.terminated)
        self.assertTrue(engine.speak("Слушаю.").error)                        # пауза после падения
        self.assertEqual(len(self.spawn.calls), 1)
        self.clock.advance(61)
        self.assertEqual(engine.speak("Слушаю.").done, 1)
        self.assertEqual(len(self.spawn.calls), 2)

    def test_error_answer_keeps_the_worker(self):
        self.spawn.script.append({"mode": "error"})
        engine = self.engine()
        result = engine.speak("Готово.")
        self.assertTrue(result.error)
        self.assertIsNone(self.spawn.procs[0].returncode)                     # воркер жив: ошибка была в запросе
        self.assertEqual(len(self.spawn.calls), 1)

    def test_three_failed_starts_make_the_voice_unavailable(self):
        self.spawn.script.extend([{"fatal": "ModuleNotFoundError"}] * 3)
        engine = self.engine()
        voice = SpeechVoice(FakeSay(), engine)
        engine.load()
        self.assertEqual(engine.state, "ready")                               # одна неудача — ещё не отказ
        for _ in range(MAX_START_FAILURES - 1):
            self.clock.advance(61)
            engine.speak("Готово.")
        self.assertEqual(engine.state, "unavailable")
        self.assertEqual(engine.unavailable.kind, "failed")
        self.assertEqual(len(self.spawn.calls), MAX_START_FAILURES)
        self.assertEqual(voice.take_notice(), FAILED_NOTICE)
        self.assertIsNone(voice.take_notice())                                # один раз
        self.clock.advance(1000)
        engine.speak("Готово.")
        self.assertEqual(len(self.spawn.calls), MAX_START_FAILURES)           # больше не пытаемся

    def test_successful_start_resets_the_failure_counter(self):
        self.spawn.script.extend([{"fatal": "RuntimeError"}, {"fatal": "RuntimeError"}, {}, {"fatal": "RuntimeError"}])
        engine = self.engine()
        for _ in range(2):
            engine.speak("Готово.")
            self.clock.advance(61)
        self.assertEqual(engine.speak("Готово.").done, 1)                     # третий запуск удался
        engine.close()
        self.clock.advance(61)
        engine.speak("Готово.")                                               # четвёртый упал: счёт заново, не «третий подряд»
        self.assertNotEqual(engine.state, "unavailable")

    def test_spawn_error_is_a_failure_not_a_crash_of_jarvis(self):
        self.spawn.fail_with = OSError("нет такого файла")
        engine = self.engine()
        result = engine.speak("Готово.")
        self.assertTrue(result.error)
        self.assertEqual(self.player.played, [])

    def test_fatal_message_logs_the_missing_package_only(self):
        self.spawn.script.append({"fatal": "ModuleNotFoundError"})
        engine = self.engine()
        with self.assertLogs("core.tts.silero", level="WARNING") as logs:
            engine.speak("Готово.")
        joined = "\n".join(logs.output)
        self.assertIn("ModuleNotFoundError", joined)
        self.assertIn("scipy", joined)


# --- не установлено ----------------------------------------------------------------------------------------------------
class TestNotInstalled(SileroCase):
    def check_missing(self, **kwargs):
        engine = self.engine(**kwargs)
        voice = SpeechVoice(FakeSay(), engine)
        engine.load()
        self.assertEqual(engine.state, "unavailable")
        self.assertEqual(engine.unavailable.kind, "not_installed")
        self.assertEqual(voice.take_notice(), NOT_INSTALLED_NOTICE)
        self.assertIsNone(voice.take_notice())
        self.assertEqual(self.spawn.calls, [])
        return engine.unavailable.reason

    def test_missing_interpreter(self):
        self.assertIn("интерпретатор Silero не найден", self.check_missing(python=self.home / "нет"))

    def test_interpreter_not_executable(self):
        self.python.chmod(0o644)
        self.assertIn("интерпретатор Silero не найден", self.check_missing())

    def test_missing_model(self):
        self.assertIn("модель Silero не найдена", self.check_missing(model=self.home / "нет.pt"))

    def test_missing_worker_script(self):
        self.assertIn("нет файла воркера", self.check_missing(worker=self.home / "нет.py"))

    def test_speech_without_an_installation_goes_to_say(self):
        say, engine = FakeSay(), self.engine(model=self.home / "нет.pt")
        voice = SpeechVoice(say, engine)
        voice.speak("Готово.")
        self.assertEqual(say.spoken, ["Готово."])

    def test_default_paths_are_inside_the_jarvis_folder(self):
        self.assertEqual(default_python().parts[-3:], ("tts_env", "bin", "python"))
        self.assertEqual(default_model().parts[-4:], ("models", "tts", "silero", "v5_ru.pt"))


# --- совместная работа с say --------------------------------------------------------------------------------------------
class TestWithSay(SileroCase):
    def test_error_in_the_middle_say_finishes_without_repeating(self):
        engine = self.engine()
        real = engine.request
        calls = []

        def flaky(text):
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError("синтез упал")
            return real(text)

        say = FakeSay()
        with patch.object(engine, "request", flaky):
            voice = SpeechVoice(say, engine)
            engine.load()                                                     # state=ready: SpeechVoice доверяет движку
            voice.speak("Первое. Второе. Третье.")
        self.assertEqual(len(self.player.played), 1)                          # прозвучало первое
        self.assertEqual(say.spoken, ["Второе. Третье."])                     # остаток — say, первое не повторяется

    def test_not_ready_engine_leaves_everything_to_say(self):
        engine = self.engine()
        say = FakeSay()
        SpeechVoice(say, engine).speak("Готово.")                             # state=idle: нейронный голос не трогаем
        self.assertEqual(say.spoken, ["Готово."])
        self.assertEqual(self.spawn.calls, [])

    def test_stop_in_the_middle_of_speech(self):
        engine = self.engine()
        engine.load()
        gate = threading.Event()
        self.player.gates[0] = gate
        say, results = FakeSay(), []
        voice = SpeechVoice(say, engine)
        thread = threading.Thread(target=lambda: results.append(voice.speak("Первое. Второе. Третье.")))
        thread.start()
        self.assertTrue(self.player.started.wait(3))
        voice.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(say.spoken, [])                                       # остановка не повод дочитывать say
        self.assertEqual(len(self.player.played), 1)
        self.assertGreaterEqual(say.stopped, 1)
        proc = self.spawn.procs[0]
        self.assertIsNone(proc.returncode)                                     # воркер остался жить для следующей фразы
        wait_until(lambda: not os.listdir(proc.wav_dir))
        self.assertEqual(os.listdir(proc.wav_dir), [])                         # недопроигранные файлы удалены

    def test_effect_tail_is_returned_to_the_speech_engine(self):
        engine = self.engine(effect_on=True)
        voice = SpeechVoice(FakeSay(), engine)
        engine.load()
        self.assertGreater(voice.speak("Готово."), 0)


# --- журнал без текста -------------------------------------------------------------------------------------------------
class TestNoTextInLogs(SileroCase):
    def test_speech_text_never_reaches_the_log(self):
        self.spawn.script.extend([{}, {"mode": "hang"}, {"mode": "error"}, {"fatal": "RuntimeError"}])
        text = f"Пользователь сказал {SECRET}. Ещё предложение про {SECRET}."
        with self.assertLogs("core.tts", level="DEBUG") as logs:
            logging.getLogger("core.tts.silero").info("старт проверки журнала")
            for _ in range(4):
                engine = self.engine(synth_timeout=0.15)
                engine.load()
                engine.speak(text)
                engine.close()
                self.clock.advance(61)
            time.sleep(0.1)                                                    # читающие потоки дописывают строки stderr
        joined = "\n".join(logs.output)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("Traceback", joined)                                  # чужой stderr не пишем
        self.assertIn("Silero: готов за 1.3 с", joined)                        # свои строки воркера — пишем


# --- словарь произношения --------------------------------------------------------------------------------------------
class TestPronounce(unittest.TestCase):
    def test_the_requested_names(self):
        cases = {"Telegram": "Телеграм", "WhatsApp": "Ватсап", "Safari": "Сафари", "Chrome": "Хром",
                 "Visual Studio Code": "Вижуал Студио Код", "YouTube": "Ютуб", "Spotify": "Спотифай",
                 "Calculator": "Калькулятор", "Notes": "Заметки", "Mail": "Почта", "App Store": "Апп Стор",
                 "macOS": "мак о эс", "Apple Music": "Эппл Мьюзик", "Preview": "Превью", "Finder": "Файндер",
                 "Terminal": "Терминал", "Music": "Музыка", "Photos": "Фото", "Messages": "Сообщения",
                 "Reminders": "Напоминания", "Calendar": "Календарь", "TextEdit": "Текстэдит", "Claude": "Клод",
                 "ChatGPT": "Чат джи пи ти", "Open-Meteo": "Опен метео", "Wi-Fi": "Вай фай", "Bluetooth": "Блютуз",
                 "Excel": "Эксель", "Word": "Ворд"}
        for name, spoken in cases.items():
            with self.subTest(name=name):
                self.assertEqual(pronounce.apply(f"Открыто приложение {name}."), f"Открыто приложение {spoken}.")
        self.assertEqual(set(cases), set(pronounce.PRONUNCIATION))

    def test_case_is_ignored(self):
        self.assertEqual(pronounce.apply("запусти safari и TELEGRAM, потом youtube"), "запусти Сафари и Телеграм, потом Ютуб")
        self.assertEqual(pronounce.apply("MACOS"), "мак о эс")

    def test_only_whole_words(self):
        for text in ("Gmail", "Mailbox", "Word2", "Notes2", "Telegrams", "Wordpress", "Safari3", "xChrome", "Photoshop"):
            with self.subTest(text=text):
                self.assertEqual(pronounce.apply(text), text)

    def test_numbers_and_time_are_untouched(self):
        for text in ("Сейчас вторник, двадцать седьмого октября, 17:12.", "Громкость 50.", "Версия 1.2.3 установлена.",
                     "Курс 94,5 рубля", "Позвоните +7 900 123-45-67"):
            with self.subTest(text=text):
                self.assertEqual(pronounce.apply(text), text)
        self.assertEqual(pronounce.apply("Telegram 5 и Word 2024"), "Телеграм 5 и Ворд 2024")

    def test_longest_names_go_first(self):
        self.assertEqual(pronounce.apply("Включаю Apple Music"), "Включаю Эппл Мьюзик")
        self.assertEqual(pronounce.apply("Включаю Music"), "Включаю Музыка")
        self.assertEqual(pronounce.apply("Visual Studio Code и Studio"), "Вижуал Студио Код и Studio")

    def test_russian_text_and_punctuation_are_unchanged(self):
        text = "Отправить маме сообщение: «привет, буду через десять минут»?"
        self.assertEqual(pronounce.apply(text), text)
        self.assertEqual(pronounce.apply(""), "")

    def test_only_silero_uses_the_dictionary(self):
        for name in ("say.py", "sherpa.py", "player.py", "factory.py", "base.py", "effect.py"):
            source = (PROJECT / "core" / "tts" / name).read_text(encoding="utf-8")
            self.assertNotIn("pronounce", source, name)
        self.assertIn("pronounce.apply", (PROJECT / "core" / "tts" / "silero.py").read_text(encoding="utf-8"))


# --- воркер: чистые функции и границы процесса ----------------------------------------------------------------------
def load_worker_module():
    spec = importlib.util.spec_from_file_location("silero_worker_under_test", PROJECT / "core" / "tts" / "silero_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def imported_modules(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level:
            names.add("." * node.level + (node.module or ""))
    return names


class TestWorkerFunctions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = load_worker_module()

    def test_module_loads_without_torch(self):
        self.assertNotIn("torch", sys.modules)

    def test_peak_gain_only_lowers_the_peak(self):
        w = self.worker
        self.assertAlmostEqual(w.peak_gain(1.044), 0.89 / 1.044)
        self.assertAlmostEqual(w.peak_gain(1.044) * 1.044, 0.89)
        self.assertEqual(w.peak_gain(0.89), 1.0)
        self.assertEqual(w.peak_gain(0.5), 1.0)                                # тихое не усиливается
        for peak in (0.0, float("nan"), float("inf")):
            with self.subTest(peak=peak):
                self.assertEqual(w.peak_gain(peak), 1.0)

    def test_scaled_samples_stay_within_the_limit_and_keep_their_shape(self):
        w = self.worker
        samples = [0.0, 0.5, -1.044, 0.25, 0.9]
        gain = w.peak_gain(max(abs(s) for s in samples))
        scaled = [s * gain for s in samples]
        self.assertLessEqual(max(abs(s) for s in scaled), 0.89 + 1e-12)
        self.assertAlmostEqual(scaled[1] / scaled[2], samples[1] / samples[2])        # пропорционально, без обрезки

    def test_limit_value(self):
        self.assertEqual(self.worker.PEAK_MAX, 0.89)
        self.assertEqual(self.worker.SAMPLE_RATE, 48000)

    def test_voices(self):
        self.assertTrue(self.worker.valid_voice("eugene"))
        self.assertTrue(self.worker.valid_voice("aidar"))
        for name in ("baya", "kseniya", "xenia", "", "../eugene", "EUGENE"):
            self.assertFalse(self.worker.valid_voice(name), name)
        self.assertEqual(set(self.worker.VOICES), set(SILERO_VOICES))

    def test_worker_imports_nothing_from_the_project(self):
        standard = set(sys.stdlib_module_names)
        extra = imported_modules(PROJECT / "core" / "tts" / "silero_worker.py") - standard - {"torch"}
        self.assertEqual(extra, set())
        source = (PROJECT / "core" / "tts" / "silero_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("from core", source)
        self.assertNotIn("import core", source)

    def test_worker_imports_torch_only_inside_main(self):
        tree = ast.parse((PROJECT / "core" / "tts" / "silero_worker.py").read_text(encoding="utf-8"))
        top_level = {a.name for node in tree.body if isinstance(node, ast.Import) for a in node.names}
        self.assertNotIn("torch", top_level)

    def test_stdout_is_json_only(self):
        source = (PROJECT / "core" / "tts" / "silero_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("print(", source)
        self.assertIn("sys.stdout = sys.stdout, sys.stderr", source)


class TestProjectSideIsolation(unittest.TestCase):
    def test_silero_module_imports_no_heavy_packages(self):
        names = imported_modules(PROJECT / "core" / "tts" / "silero.py")
        for heavy in ("torch", "numpy", "scipy", "silero", "torchaudio"):
            self.assertNotIn(heavy, names)
        for heavy in ("torch", "scipy", "silero"):
            self.assertNotIn(heavy, sys.modules)

    def test_factory_imports_silero_only_inside_a_function(self):
        for line in (PROJECT / "core" / "tts" / "factory.py").read_text(encoding="utf-8").splitlines():
            if "core.tts.silero" in line and "import" in line:
                self.assertTrue(line.startswith(" "), line)

    def test_engine_say_never_starts_the_worker(self):
        spawn = FakeSpawn()
        with patch("core.tts.silero._spawn_worker", spawn), patch("core.settings.ENV_FILE", env_file("TTS_ENGINE=say\n")), \
                patch("core.tts.factory.build_neural") as build:
            voice = build_tts(FakeSay())
            voice.start()
            voice.speak("Готово.")
        build.assert_not_called()
        self.assertIsNone(voice.neural)
        self.assertEqual(spawn.calls, [])

    def test_the_real_spawn_function_is_forbidden_in_tests(self):
        with self.assertRaises(tests_base.ForbiddenInTests):
            silero_module._spawn_worker(["python"], {})


# --- настройки -----------------------------------------------------------------------------------------------------------
class TestSettings(unittest.TestCase):
    def settings(self, text=""):
        with patch("core.tts.base.os.cpu_count", return_value=8):
            return tts_settings(env_file(text))

    def test_defaults_do_not_change(self):
        self.assertEqual(self.settings(), TTSSettings())
        self.assertEqual(TTSSettings().unload_minutes, 10)
        self.assertFalse(TTSSettings().effect)

    def test_silero_engine_and_default_voice(self):
        s = self.settings("TTS_ENGINE=silero\n")
        self.assertEqual((s.engine, s.voice, s.unload_minutes, s.silero_python, s.silero_model), ("silero", "eugene", 10, "", ""))

    def test_voices(self):
        self.assertEqual(self.settings("TTS_ENGINE=silero\nTTS_VOICE=aidar\n").voice, "aidar")
        self.assertEqual(self.settings("TTS_ENGINE=silero\nTTS_VOICE=Eugene\n").voice, "eugene")
        with self.assertLogs("core.tts.base", level="WARNING") as logs:
            self.assertEqual(self.settings("TTS_ENGINE=silero\nTTS_VOICE=vits-piper-ru_RU-denis-medium\n").voice, "eugene")
        self.assertIn("для Silero допустимы eugene, aidar", logs.output[0])
        with self.assertLogs("core.tts.base", level="WARNING"):
            self.assertEqual(self.settings("TTS_ENGINE=silero\nTTS_VOICE=baya\n").voice, "eugene")

    def test_voice_is_not_checked_for_other_engines(self):
        self.assertEqual(self.settings("TTS_ENGINE=sherpa\nTTS_VOICE=vits-x\n").voice, "vits-x")

    def test_speed_is_ignored_with_a_warning(self):
        with self.assertLogs("core.tts.base", level="WARNING") as logs:
            self.assertEqual(self.settings("TTS_ENGINE=silero\nTTS_SPEED=1.3\n").speed, 1.0)
        self.assertIn("Silero скорость не меняет", logs.output[0])
        self.assertEqual(self.settings("TTS_ENGINE=sherpa\nTTS_SPEED=1.3\n").speed, 1.3)

    def test_unload_minutes(self):
        self.assertEqual(self.settings("TTS_UNLOAD_MINUTES=0\n").unload_minutes, 0)
        self.assertEqual(self.settings("TTS_UNLOAD_MINUTES=25\n").unload_minutes, 25)
        with self.assertLogs("core.tts.base", level="WARNING") as logs:
            self.assertEqual(self.settings("TTS_UNLOAD_MINUTES=мусор\n").unload_minutes, 10)
        self.assertIn("TTS_UNLOAD_MINUTES=мусор не число", logs.output[0])
        with self.assertLogs("core.tts.base", level="WARNING"):
            self.assertEqual(self.settings("TTS_UNLOAD_MINUTES=-5\n").unload_minutes, 0)
        with self.assertLogs("core.tts.base", level="WARNING"):
            self.assertEqual(self.settings("TTS_UNLOAD_MINUTES=100000\n").unload_minutes, 240)

    def test_paths(self):
        s = self.settings("TTS_SILERO_PYTHON=~/env/bin/python\nTTS_SILERO_MODEL=/models/v5_ru.pt\n")
        self.assertEqual((s.silero_python, s.silero_model), ("~/env/bin/python", "/models/v5_ru.pt"))
        engine = SileroEngine(python=s.silero_python, model=s.silero_model)
        self.assertEqual(engine.python, Path.home() / "env" / "bin" / "python")                  # «~» раскрывается

    def test_unknown_engine_message_names_silero(self):
        with self.assertLogs("core.tts.base", level="WARNING") as logs:
            self.assertEqual(self.settings("TTS_ENGINE=мусор\n").engine, "say")
        self.assertIn("say, sherpa, silero", logs.output[0])

    def test_effect_stays_off_by_default_for_silero(self):
        self.assertFalse(self.settings("TTS_ENGINE=silero\n").effect)

    def test_env_example_lists_the_new_settings(self):
        text = (PROJECT / ".env.example").read_text(encoding="utf-8")
        for line in ("TTS_ENGINE=say", "TTS_UNLOAD_MINUTES=10", "TTS_SILERO_PYTHON=", "TTS_SILERO_MODEL="):
            self.assertIn(line, text)
        self.assertIn("silero", text)


class TestFactory(SileroCase):
    def setUp(self):
        super().setUp()
        patcher = patch("core.tts.base.os.cpu_count", return_value=8)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_silero_builds_the_engine_from_settings_and_loads_nothing(self):
        text = (f"TTS_ENGINE=silero\nTTS_VOICE=aidar\nTTS_THREADS=2\nTTS_UNLOAD_MINUTES=7\nTTS_SILERO_PYTHON={self.python}\n"
                f"TTS_SILERO_MODEL={self.model}\nTTS_EFFECT=jarvis\nTTS_PITCH=-3\nTTS_REVERB=0.2\n")
        with patch("core.settings.ENV_FILE", env_file(text)):
            voice = build_tts(FakeSay())
        neural = voice.neural
        self.assertIsInstance(neural, SileroEngine)
        self.assertEqual((neural.voice, neural.threads, neural.unload_minutes, neural.effect_on, neural.pitch, neural.reverb),
                         ("aidar", 2, 7, True, -3.0, 0.2))
        self.assertEqual((neural.python, neural.model), (self.python, self.model))
        self.assertEqual((neural.state, self.spawn.calls), ("idle", []))

    def test_start_runs_the_worker_in_the_background(self):
        text = f"TTS_ENGINE=silero\nTTS_SILERO_PYTHON={self.python}\nTTS_SILERO_MODEL={self.model}\n"
        with patch("core.settings.ENV_FILE", env_file(text)):
            voice = build_tts(FakeSay())
        self.addCleanup(voice.neural.close)
        voice.start()
        self.assertTrue(wait_until(lambda: voice.neural.state == "ready"))
        self.assertEqual(len(self.spawn.calls), 1)

    def test_effect_is_disabled_without_numpy(self):
        with patch("core.settings.ENV_FILE", env_file("TTS_ENGINE=silero\nTTS_EFFECT=jarvis\n")), \
                patch("core.tts.effect.numpy_module", return_value=None):
            with self.assertLogs("core.tts.factory", level="WARNING") as logs:
                voice = build_tts(FakeSay())
        self.assertFalse(voice.neural.effect_on)
        self.assertIn("numpy недоступен", logs.output[0])


# --- проверка вручную (python -m core.tts.check --engine silero) ---------------------------------------------------------
class TestCheckTool(SileroCase):
    def run_check(self, *args, env_extra=""):
        lines = []
        tmp = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / "tts_samples"
        env = f"TTS_SILERO_PYTHON={self.python}\nTTS_SILERO_MODEL={self.model}\nTTS_THREADS=2\n{env_extra}"
        with patch("core.settings.ENV_FILE", env_file(env)), patch("core.tts.check.samples_dir", return_value=tmp), \
                patch("core.tts.check.AfplayPlayer", return_value=self.player):
            code = check.main(["--engine", "silero", *args], out=lines.append)
        return code, "\n".join(lines), tmp

    def test_plain_check_reports_versions_times_and_peak(self):
        code, text, tmp = self.run_check()
        self.assertEqual(code, 0)
        for part in ("Интерпретатор:", "Воркер:", "Модель:", "torch 2.15.0", "numpy 2.5.4", "scipy 1.18.1", "потоков torch 2",
                     "Загрузка модели в воркере 1.3 с", "Первая фраза: синтез", "RTF", "пик 0.5", "голоса: aidar, eugene"):
            self.assertIn(part, text)
        self.assertFalse(tmp.exists())
        self.assertIsNotNone(self.spawn.procs[0].returncode)                      # проверка не оставляет воркер
        self.assertEqual(self.player.played, [])

    def test_samples_are_ten_private_wavs_in_the_final_folder(self):
        code, text, tmp = self.run_check("--samples")
        self.assertEqual(code, 0)
        folder = tmp / "silero_eugene_final"
        files = sorted(p.name for p in folder.iterdir())
        self.assertEqual(files, sorted(f"{name}.wav" for name, _ in check.SAMPLE_PHRASES))
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        for name in files:
            self.assertEqual(stat.S_IMODE(os.stat(folder / name).st_mode), 0o600)
        self.assertEqual(self.player.played, [])
        self.assertIn("RTF по фразам: медиана", text)
        self.assertIn("наибольший пик", text)

    def test_samples_text_goes_through_prepare_for_speech_and_pronounce(self):
        self.run_check("--samples")
        sent = [r["text"] for r in self.spawn.procs[0].requests]
        self.assertIn("Открыто приложение Телеграм.", sent)
        self.assertIn("Запустил Сафари и закрыл Вижуал Студио Код.", sent)
        self.assertIn("Сейчас вторник, двадцать седьмого октября, 17:12.", sent)
        self.assertFalse(any("Safari" in s or "Telegram" in s for s in sent))

    def test_aidar_samples_use_their_own_folder(self):
        _, _, tmp = self.run_check("--samples", env_extra="TTS_VOICE=aidar\n")
        self.assertTrue((tmp / "silero_aidar_final").is_dir())

    def test_play_speaks_through_the_engine(self):
        code, text, _ = self.run_check("--play")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(self.player.played), 10)
        self.assertIn("воспроизведено предложений", text)

    def test_missing_model_is_reported(self):
        self.model.unlink()
        code, text, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Не установлено: модель Silero не найдена", text)
        self.assertEqual(self.spawn.calls, [])

    def test_failed_load_is_reported(self):
        self.spawn.script.append({"fatal": "ModuleNotFoundError"})
        code, text, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Не загружено", text)

    def test_engine_is_taken_from_env_when_it_is_silero(self):
        lines = []
        env = f"TTS_ENGINE=silero\nTTS_SILERO_PYTHON={self.python}\nTTS_SILERO_MODEL={self.model}\n"
        with patch("core.settings.ENV_FILE", env_file(env)), patch("core.tts.check.AfplayPlayer", return_value=self.player):
            self.assertEqual(check.main([], out=lines.append), 0)
        self.assertIn("torch 2.15.0", "\n".join(lines))


# --- очередь предложений принимает готовый файл -------------------------------------------------------------------------
class TestQueueWithFiles(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)
        self.wavs = TempWavs()
        self.addCleanup(self.wavs.cleanup)

    def test_a_path_is_played_as_is_and_removed_afterwards(self):
        made = []

        def synth(sentence):
            made.append(make_wav(self.folder))
            return made[-1]

        player = FakePlayer()
        result = SentenceQueue(synth, player, self.wavs).speak(["Раз.", "Два."])
        self.assertEqual((result.done, result.error, result.stopped), (2, False, False))
        self.assertEqual([p["path"] for p in player.played], made)
        self.assertEqual(os.listdir(self.folder), [])
        self.assertFalse(self.wavs._dir)                                       # своих файлов очередь не создавала

    def test_a_late_file_after_stop_is_removed_and_not_played(self):
        gate, started = threading.Event(), threading.Event()

        def synth(sentence):
            started.set()
            gate.wait(3)
            return make_wav(self.folder)

        player, results = FakePlayer(), []
        queue_ = SentenceQueue(synth, player, self.wavs)
        thread = threading.Thread(target=lambda: results.append(queue_.speak(["Раз."])))
        thread.start()
        self.assertTrue(started.wait(3))
        queue_.stop()
        gate.set()
        thread.join(3)
        self.assertTrue(results[0].stopped)
        self.assertEqual(player.played, [])
        self.assertTrue(wait_until(lambda: os.listdir(self.folder) == []))


if __name__ == "__main__":
    unittest.main()
