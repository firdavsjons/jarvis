"""Этап 8.1: локальное распознавание речи (core/stt). Без модели, сети и микрофона: движки подменены фальшивками."""
import logging
import math
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков, загрузки моделей и записи в домашние папки
import speech_recognition as sr

from core import contacts, installed_apps
from core.engine import SpeechEngine, speech_phrase_limit
from core.stt import check, factory, prompt as prompt_module, service, whisper_local
from core.stt import faster as faster_module
from core.stt import whisper_cpp as cpp_module
from core.stt.base import MIN_SECONDS, Audio, Engine, Loading, Recognition, Segment, SttError, Unavailable
from core.stt.blacklist import BLACKLIST, is_blacklisted
from core.stt.google import GoogleEngine, google_language
from core.stt import prompt_echo
from core.stt.prompt import (PROMPT_TOKEN_LIMIT, PromptProvider, accusative, build_prompt, contact_names, dative,
                             estimate_tokens, prepositional, russian_app_names, weather_sentence)
from core.stt.repeats import collapse_repeats
from core.stt.service import (FAILED_FALLBACK, FAILED_OFF, FAILED_REPLY, LOADING_REPLY, NOT_INSTALLED_FALLBACK,
                              NOT_INSTALLED_OFF, SpeechToText)
from core.stt.truncated import TRUNCATED_REPLY, is_truncated
from core.stt.whisper_local import LocalWhisper, filter_segments
from fixtures_apps import patch_installed
import main

SECRET_PHRASE = "секретная фраза пользователя"


def env_file(text):
    path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def make_audio(seconds=2.0):
    return Audio(sr.AudioData(b"\x01\x00" * int(16000 * seconds), 16000, 2))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class StubWhisper(LocalWhisper):
    """Локальный движок без модели: куски речи задаются в тесте."""
    name = "stub"
    DEFAULT_MODEL = "stub-model"

    def __init__(self, segments=(), load_error=None, **kwargs):
        kwargs.setdefault("unload_minutes", 0)
        super().__init__(**kwargs)
        self.segments = list(segments)
        self.load_error = load_error
        self.transcribe_error = None
        self.gate = None                 # Event: загрузка ждёт, пока тест её не отпустит
        self.loads = 0
        self.freed = []
        self.calls = []                  # (длина PCM в байтах, подсказка)

    def _load_model(self):
        self.loads += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.load_error is not None:
            raise self.load_error
        return object()

    def _transcribe(self, model, pcm, prompt):
        self.calls.append((len(pcm), prompt))
        if self.transcribe_error is not None:
            raise self.transcribe_error
        return list(self.segments)

    def _free_model(self, model):
        self.freed.append(model)

    def wait_until(self, state, timeout=3.0):
        deadline = time.monotonic() + timeout
        while self.state != state and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.state == state


class FakeGoogle(Engine):
    name = "google"
    local = False

    def __init__(self, text="привет из google", error=None):
        self.text, self.error, self.calls = text, error, []

    def recognize(self, audio, prompt=""):
        self.calls.append(prompt)
        if self.error:
            raise self.error
        return Recognition(text=self.text, engine=self.name, seconds=0.3, audio_seconds=audio.seconds)


def good(text="открой сафари", **kwargs):
    return Segment(text=text, **kwargs)


def ready_engine(*segments, **kwargs):
    engine = StubWhisper(segments or [good()], **kwargs)
    engine.ensure_loaded()
    return engine


# --- настройки ---------------------------------------------------------------------------------------------------
class TestSettings(unittest.TestCase):
    def settings(self, text="", cores=8):
        with patch("core.settings.ENV_FILE", env_file(text)), patch("core.stt.factory.os.cpu_count", return_value=cores):
            return factory.stt_settings()

    def test_defaults(self):
        s = self.settings()
        self.assertEqual((s.engine, s.fallback, s.model, s.language, s.threads, s.unload_minutes),
                         ("whisper_cpp", True, "", "ru", 4, 10))

    def test_engine_values_and_aliases(self):
        for raw, expected in (("whisper_cpp", "whisper_cpp"), ("whisper", "whisper_cpp"), ("Whisper-CPP", "whisper_cpp"),
                              ("faster_whisper", "faster_whisper"), ("faster-whisper", "faster_whisper"),
                              ("google", "google"), ("GOOGLE", "google")):
            with self.subTest(raw=raw):
                self.assertEqual(self.settings(f"STT_ENGINE={raw}\n").engine, expected)

    def test_unknown_engine_is_local_not_google(self):
        with self.assertLogs("core.stt.factory", level="WARNING") as logs:
            self.assertEqual(self.settings("STT_ENGINE=мусор\n").engine, "whisper_cpp")
        self.assertIn("STT_ENGINE=мусор не поддерживается", logs.output[0])

    def test_fallback_off_values(self):
        for raw in ("off", "OFF", "no", "false", "0", "none"):
            with self.subTest(raw=raw):
                self.assertFalse(self.settings(f"STT_FALLBACK={raw}\n").fallback)
        for raw in ("google", "on", ""):
            with self.subTest(raw=raw):
                self.assertTrue(self.settings(f"STT_FALLBACK={raw}\n").fallback)

    def test_threads_never_exceed_half_of_cores(self):
        self.assertEqual(self.settings("STT_THREADS=8\n", cores=8).threads, 4)
        self.assertEqual(self.settings("STT_THREADS=2\n", cores=8).threads, 2)
        self.assertEqual(self.settings("", cores=4).threads, 2)        # по умолчанию 4, но ядер всего 4
        self.assertEqual(self.settings("", cores=1).threads, 1)
        self.assertEqual(self.settings("STT_THREADS=мусор\n", cores=8).threads, 4)
        self.assertEqual(self.settings("STT_THREADS=0\n", cores=8).threads, 1)

    def test_model_language_unload(self):
        s = self.settings("STT_MODEL=small-q5_1\nSTT_LANGUAGE=en\nSTT_UNLOAD_MINUTES=3\n")
        self.assertEqual((s.model, s.language, s.unload_minutes), ("small-q5_1", "en", 3))
        self.assertEqual(self.settings("STT_UNLOAD_MINUTES=0\n").unload_minutes, 0)

    def test_env_example_lists_all_settings(self):
        text = Path(__file__).with_name(".env.example").read_text(encoding="utf-8")
        for name in ("STT_ENGINE=whisper_cpp", "STT_FALLBACK=google", "STT_MODEL=", "STT_LANGUAGE=ru", "STT_THREADS=4",
                     "STT_UNLOAD_MINUTES=10", "SPEECH_PHRASE_LIMIT=20", "SPEECH_PAUSE=1.2"):
            self.assertIn(name, text)


class TestBuild(unittest.TestCase):
    def build(self, text=""):
        with patch("core.settings.ENV_FILE", env_file(text)):
            return factory.build_stt(MagicMock(), notify=lambda text: None)

    def test_default_is_local_with_google_fallback_and_prompt(self):
        stt = self.build()
        self.assertEqual(stt.primary.name, "whisper_cpp")
        self.assertEqual(stt.primary.model_name, "large-v3-turbo-q5_0")
        self.assertEqual(stt.fallback.name, "google")
        self.assertIsInstance(stt.prompt_provider, PromptProvider)

    def test_faster_whisper_default_model_is_small(self):
        stt = self.build("STT_ENGINE=faster_whisper\n")
        self.assertEqual((stt.primary.name, stt.primary.model_name), ("faster_whisper", "small"))

    def test_fallback_off(self):
        self.assertIsNone(self.build("STT_FALLBACK=off\n").fallback)

    def test_google_engine_has_no_fallback_and_no_prompt(self):
        stt = self.build("STT_ENGINE=google\n")
        self.assertIsInstance(stt.primary, GoogleEngine)
        self.assertIsNone(stt.fallback)
        self.assertIsNone(stt.prompt_provider)

    def test_build_loads_nothing(self):
        stt = self.build()
        self.assertEqual(stt.primary.state, "idle")          # настоящий _import_backend упал бы (tests_base)

    def test_google_never_loads_whisper(self):
        with patch.object(LocalWhisper, "start_loading") as start, patch.object(LocalWhisper, "ensure_loaded") as ensure, \
                patch("core.stt.whisper_cpp._import_backend") as cpp, patch("core.stt.faster._import_backend") as fw:
            stt = self.build("STT_ENGINE=google\n")
            stt.start()
        for spy in (start, ensure, cpp, fw):
            spy.assert_not_called()

    def test_start_loads_local_model_in_background(self):
        with patch.object(LocalWhisper, "start_loading") as start:
            self.build().start()
        start.assert_called_once_with()

    def test_describe_says_where_audio_goes(self):
        self.assertIn("звук остаётся на Mac", self.build("STT_FALLBACK=off\n").describe())
        self.assertIn("откат выключен", self.build("STT_FALLBACK=off\n").describe())
        self.assertIn("уйдёт в Google при сбое", self.build().describe())
        self.assertIn("звук уходит в Google", self.build("STT_ENGINE=google\n").describe())

    def test_phrase_limit_setting(self):
        with patch("core.settings.ENV_FILE", env_file("")):
            self.assertEqual(speech_phrase_limit(), 20.0)
        with patch("core.settings.ENV_FILE", env_file("SPEECH_PHRASE_LIMIT=35\n")):
            self.assertEqual(speech_phrase_limit(), 35.0)
        with patch("core.settings.ENV_FILE", env_file("SPEECH_PHRASE_LIMIT=1000\n")):
            self.assertEqual(speech_phrase_limit(), 60.0)


# --- Google ------------------------------------------------------------------------------------------------------
class TestGoogle(unittest.TestCase):
    def test_language_codes(self):
        self.assertEqual(google_language("ru"), "ru-RU")
        self.assertEqual(google_language("ru-RU"), "ru-RU")
        self.assertEqual(google_language("en"), "en-US")
        self.assertEqual(google_language("de"), "de-DE")

    def test_recognize_passes_audio_and_language_only(self):
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "  Открой Сафари  "
        audio = make_audio()
        result = GoogleEngine(recognizer, "ru").recognize(audio, prompt="Джарвис. Контакты: мама.")
        recognizer.recognize_google.assert_called_once_with(audio.source, language="ru-RU")
        self.assertEqual(result.text, "Открой Сафари")
        self.assertEqual(result.engine, "google")

    def test_unknown_value_is_empty_text_not_error(self):
        recognizer = MagicMock()
        recognizer.recognize_google.side_effect = sr.UnknownValueError()
        self.assertEqual(GoogleEngine(recognizer).recognize(make_audio()).text, "")

    def test_request_error_is_technical(self):
        recognizer = MagicMock()
        recognizer.recognize_google.side_effect = sr.RequestError("нет сети")
        with self.assertRaises(SttError):
            GoogleEngine(recognizer).recognize(make_audio())


# --- фильтр шума и галлюцинаций ----------------------------------------------------------------------------------
class TestHallucinationGuard(unittest.TestCase):
    def recognize(self, *segments):
        return ready_engine(*segments).recognize(make_audio())

    def test_normal_speech_passes(self):
        result = self.recognize(good("открой сафари", no_speech_prob=0.05, avg_logprob=-0.3))
        self.assertEqual(result.text, "открой сафари")
        self.assertIsNone(result.discarded)
        self.assertAlmostEqual(result.confidence, math.exp(-0.3))

    def test_no_speech_prob_above_threshold_dropped(self):
        result = self.recognize(good("тихо", no_speech_prob=0.61, avg_logprob=-0.2))
        self.assertEqual((result.text, result.discarded), ("", "noise"))

    def test_no_speech_prob_at_threshold_kept(self):
        self.assertEqual(self.recognize(good("граница", no_speech_prob=0.6, avg_logprob=-0.2)).text, "граница")

    def test_low_logprob_dropped(self):
        result = self.recognize(good("бормотание", no_speech_prob=0.1, avg_logprob=-1.01))
        self.assertEqual((result.text, result.discarded), ("", "noise"))

    def test_logprob_at_threshold_kept(self):
        self.assertEqual(self.recognize(good("граница", avg_logprob=-1.0)).text, "граница")

    def test_every_blacklisted_phrase_dropped(self):
        for entry in BLACKLIST:
            with self.subTest(entry=entry):
                result = self.recognize(good(entry.capitalize() + "."))
                self.assertEqual((result.text, result.discarded), ("", "noise"))

    def test_blacklist_matches_with_author_case_and_punctuation(self):
        for text in ("Продолжение следует...", "СУБТИТРЫ СДЕЛАЛ DimaTorzok", "Субтитры создавал DimaTorzok",
                     "Спасибо за просмотр!", "Подписывайтесь на канал, ставьте лайки", "Редактор субтитров А.Семкин Корректор А.Егорова"):
            with self.subTest(text=text):
                self.assertTrue(is_blacklisted(text))

    def test_blacklist_is_not_a_substring_search(self):
        for text in ("скажи спасибо за просмотр", "включи продолжение следует", "спасибо", "открой сафари", ""):
            with self.subTest(text=text):
                self.assertFalse(is_blacklisted(text))

    def test_blacklist_has_the_agreed_phrases(self):
        for entry in ("продолжение следует", "субтитры сделал", "субтитры создавал", "спасибо за просмотр",
                      "подписывайтесь на канал", "редактор субтитров"):
            self.assertIn(entry, BLACKLIST)

    def test_only_bad_segment_is_dropped(self):
        result = self.recognize(good("открой сафари", avg_logprob=-0.2), good("Субтитры сделал DimaTorzok"),
                                good("бормотание", avg_logprob=-2.0), good("и закрой почту", avg_logprob=-0.4))
        self.assertEqual(result.text, "открой сафари и закрой почту")
        self.assertIsNone(result.discarded)

    def test_blacklisted_phrase_split_into_segments(self):
        kept, reason = filter_segments([good("спасибо"), good("за просмотр")])
        self.assertEqual((kept, reason), ([], "noise"))

    def test_empty_result_is_not_noise(self):
        result = self.recognize()
        engine = ready_engine(good("  "))
        result_blank = engine.recognize(make_audio())
        self.assertEqual((result_blank.text, result_blank.discarded), ("", None))

    def test_segments_without_scores_are_checked_by_text_only(self):
        self.assertEqual(self.recognize(good("открой почту", no_speech_prob=None, avg_logprob=None)).text, "открой почту")


# --- локальный движок: загрузка, прогрев, выгрузка ---------------------------------------------------------------
class TestLoading(unittest.TestCase):
    def test_background_load_does_not_block(self):
        engine = StubWhisper([good()])
        engine.gate = threading.Event()
        started = time.monotonic()
        engine.start_loading()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(engine.state, "loading")
        with self.assertRaises(Loading):
            engine.ensure_loaded()
        with self.assertRaises(Loading):
            engine.recognize(make_audio())
        engine.gate.set()
        self.assertTrue(engine.wait_until("ready"))
        self.assertEqual(engine.recognize(make_audio()).text, "открой сафари")

    def test_start_loading_twice_loads_once(self):
        engine = StubWhisper([good()])
        engine.start_loading()
        engine.start_loading()
        self.assertTrue(engine.wait_until("ready"))
        self.assertEqual(engine.loads, 1)

    def test_warm_up_on_short_quiet_fragment_without_prompt(self):
        engine = ready_engine()
        self.assertEqual(len(engine.calls), 1)
        length, prompt = engine.calls[0]
        self.assertEqual(length, 2 * 8000)                    # 0,5 с тишины 16 кГц / 16 бит
        self.assertEqual(prompt, "")

    def test_load_time_is_logged(self):
        with self.assertLogs("core.stt.whisper_local", level="INFO") as logs:
            ready_engine()
        self.assertTrue(any("загружена за" in line for line in logs.output))
        self.assertTrue(any("Прогрев" in line for line in logs.output))

    def test_model_error_makes_engine_unavailable(self):
        engine = StubWhisper(load_error=RuntimeError("битый файл"))
        with self.assertLogs("core.stt.whisper_local", level="WARNING"):
            with self.assertRaises(Unavailable) as ctx:
                engine.ensure_loaded()
        self.assertEqual(ctx.exception.kind, "failed")
        self.assertEqual(engine.state, "unavailable")
        with self.assertRaises(Unavailable):
            engine.ensure_loaded()
        self.assertEqual(engine.loads, 1)                      # повторно не грузим

    def test_missing_package_is_not_installed(self):
        engine = StubWhisper(load_error=Unavailable("пакет не установлен"))
        with self.assertLogs("core.stt.whisper_local", level="WARNING"):
            with self.assertRaises(Unavailable) as ctx:
                engine.ensure_loaded()
        self.assertEqual(ctx.exception.kind, "not_installed")

    def test_warm_up_failure_is_a_load_failure(self):
        engine = StubWhisper()
        engine.transcribe_error = RuntimeError("Metal")
        with self.assertLogs("core.stt.whisper_local", level="WARNING"):
            with self.assertRaises(Unavailable):
                engine.ensure_loaded()

    def test_background_load_error_is_logged_and_state_unavailable(self):
        engine = StubWhisper(load_error=RuntimeError("битый файл"))
        with self.assertLogs("core.stt.whisper_local", level="WARNING"):
            engine.start_loading()
            self.assertTrue(engine.wait_until("unavailable"))

    def test_transcribe_error_is_technical_and_engine_stays_ready(self):
        engine = ready_engine()
        engine.transcribe_error = RuntimeError("сбой")
        with self.assertRaises(SttError):
            engine.recognize(make_audio())
        self.assertEqual(engine.state, "ready")

    def test_prompt_reaches_local_engine(self):
        engine = ready_engine()
        engine.recognize(make_audio(), "Джарвис. Контакты: мама.")
        self.assertEqual(engine.calls[-1][1], "Джарвис. Контакты: мама.")


class TestUnload(unittest.TestCase):
    def setUp(self):
        patcher = patch("core.stt.whisper_local.threading.Timer")
        self.timer = patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.engine = StubWhisper([good()], unload_minutes=10, clock=self.clock)
        self.engine.ensure_loaded()

    def test_unloaded_after_idle_minutes(self):
        self.clock.now += 599
        self.assertFalse(self.engine.unload_if_idle())
        self.assertEqual(self.engine.state, "ready")
        self.clock.now += 2
        with self.assertLogs("core.stt.whisper_local", level="INFO") as logs:
            self.assertTrue(self.engine.unload_if_idle())
        self.assertEqual(self.engine.state, "idle")
        self.assertEqual(len(self.engine.freed), 1)
        self.assertTrue(any("выгружена" in line for line in logs.output))

    def test_use_postpones_unload(self):
        self.clock.now += 500
        self.engine.recognize(make_audio())
        self.clock.now += 500
        self.assertFalse(self.engine.unload_if_idle())
        self.assertEqual(self.engine.state, "ready")

    def test_next_phrase_reloads_synchronously(self):
        self.clock.now += 700
        self.engine.unload_if_idle()
        self.assertEqual(self.engine.recognize(make_audio()).text, "открой сафари")
        self.assertEqual(self.engine.loads, 2)
        self.assertEqual(self.engine.state, "ready")

    def test_not_unloaded_during_recognition(self):
        self.clock.now += 700
        with self.engine._run_lock:
            self.assertFalse(self.engine.unload_if_idle())
        self.assertEqual(self.engine.state, "ready")

    def test_zero_minutes_never_unloads_and_sets_no_timer(self):
        timer_calls = self.timer.call_count
        engine = StubWhisper([good()], unload_minutes=0, clock=self.clock)
        engine.ensure_loaded()
        self.assertEqual(self.timer.call_count, timer_calls)
        self.clock.now += 100000
        self.assertFalse(engine.unload_if_idle())

    def test_load_schedules_a_timer_for_the_idle_period(self):
        self.timer.assert_called_with(600.0, self.engine.unload_if_idle)


# --- адаптеры движков (пакеты подменены) -------------------------------------------------------------------------
class FakeArray:
    def __init__(self, data):
        self.data = data

    def astype(self, kind):
        return self

    def __truediv__(self, other):
        return self


FAKE_NUMPY = types.SimpleNamespace(int16="int16", float32="float32", frombuffer=lambda pcm, dtype: FakeArray(pcm))


class FakeCppModel:
    instances = []
    result = []

    def __init__(self, path, **kwargs):
        self.path, self.kwargs, self.calls = path, kwargs, []
        FakeCppModel.instances.append(self)

    def transcribe(self, samples, **options):
        self.calls.append(options)
        return list(FakeCppModel.result)


def raw_segment(text, probability=None):
    return types.SimpleNamespace(text=text, probability=probability)


class TestWhisperCppAdapter(unittest.TestCase):
    def setUp(self):
        FakeCppModel.instances, FakeCppModel.result = [], []
        self.models = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT))
        for target, value in (("_import_backend", types.SimpleNamespace(Model=FakeCppModel)), ("_numpy", FAKE_NUMPY)):
            patcher = patch(f"core.stt.whisper_cpp.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def engine(self, model="", **kwargs):
        return cpp_module.WhisperCppEngine(model=model, models_dir=self.models, unload_minutes=0, **kwargs)

    def test_default_model_file_name(self):
        self.assertEqual(self.engine().model_path(), self.models / "ggml-large-v3-turbo-q5_0.bin")
        self.assertEqual(self.engine("small-q5_1").model_path(), self.models / "ggml-small-q5_1.bin")

    def test_missing_model_file_is_not_installed_and_never_downloaded(self):
        engine = self.engine()
        with self.assertLogs("core.stt.whisper_local", level="WARNING"):
            with self.assertRaises(Unavailable) as ctx:
                engine.ensure_loaded()
        self.assertEqual(ctx.exception.kind, "not_installed")
        self.assertIn("ggml-large-v3-turbo-q5_0.bin", ctx.exception.reason)
        self.assertEqual(FakeCppModel.instances, [])           # без файла модель вообще не создаётся: pywhispercpp не скачивает

    def test_model_is_always_loaded_from_an_existing_file_path(self):
        (self.models / "ggml-small-q5_1.bin").write_bytes(b"x")
        engine = self.engine("small-q5_1", threads=3)
        engine.ensure_loaded()
        model = FakeCppModel.instances[0]
        self.assertEqual(model.path, str(self.models / "ggml-small-q5_1.bin"))
        self.assertEqual(model.kwargs["n_threads"], 3)
        self.assertIs(model.kwargs["print_progress"], False)

    def test_transcribe_options_and_scores(self):
        (self.models / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x")
        FakeCppModel.result = [raw_segment(" открой сафари ", 0.9), raw_segment("шум", float("nan")), raw_segment("ноль", 0.0)]
        engine = self.engine(threads=2)
        engine.ensure_loaded()
        segments = engine._transcribe(engine._model, b"\x00\x00" * 10, "Джарвис.")
        options = FakeCppModel.instances[0].calls[-1]
        self.assertEqual(options["language"], "ru")
        self.assertEqual(options["initial_prompt"], "Джарвис.")
        self.assertIs(options["extract_probability"], True)
        self.assertEqual(options["n_threads"], 2)
        self.assertEqual(options["no_speech_thold"], 0.6)
        self.assertEqual(options["logprob_thold"], -1.0)
        self.assertAlmostEqual(segments[0].avg_logprob, math.log(0.9))
        self.assertIsNone(segments[0].no_speech_prob)          # no_speech_prob pywhispercpp не отдаёт
        self.assertIsNone(segments[1].avg_logprob)
        self.assertIsNone(segments[2].avg_logprob)

    def test_empty_prompt_is_not_passed(self):
        (self.models / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x")
        engine = self.engine()
        engine.ensure_loaded()
        engine._transcribe(engine._model, b"\x00\x00", "")
        self.assertNotIn("initial_prompt", FakeCppModel.instances[0].calls[-1])

    def test_low_probability_goes_through_the_guard(self):
        (self.models / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x")
        FakeCppModel.result = [raw_segment("бормотание", 0.2)]       # ln(0,2) = -1,6 < -1,0
        result = self.engine().recognize(make_audio())
        self.assertEqual((result.text, result.discarded), ("", "noise"))

    def test_missing_package_message(self):
        with patch("core.stt.whisper_cpp.importlib.import_module", side_effect=ImportError("нет")):
            with self.assertRaises(Unavailable) as ctx:
                tests_base.REAL_IMPORT_BACKEND["core.stt.whisper_cpp"]()
        self.assertIn("pywhispercpp", ctx.exception.reason)
        self.assertEqual(ctx.exception.kind, "not_installed")


class FakeFasterModel:
    instances = []
    segments = []
    error = None

    def __init__(self, name, **kwargs):
        if FakeFasterModel.error:
            raise FakeFasterModel.error
        self.name, self.kwargs, self.calls = name, kwargs, []
        FakeFasterModel.instances.append(self)

    def transcribe(self, samples, **options):
        self.calls.append(options)
        return iter(FakeFasterModel.segments), object()


class TestFasterWhisperAdapter(unittest.TestCase):
    def setUp(self):
        FakeFasterModel.instances, FakeFasterModel.segments, FakeFasterModel.error = [], [], None
        for target, value in (("_import_backend", types.SimpleNamespace(WhisperModel=FakeFasterModel)),
                              ("_numpy", FAKE_NUMPY)):
            patcher = patch(f"core.stt.faster.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.models = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT))

    def engine(self, **kwargs):
        return faster_module.FasterWhisperEngine(models_dir=self.models, unload_minutes=0, **kwargs)

    def test_loads_only_local_files_on_cpu_int8(self):
        engine = self.engine(threads=3)
        engine.ensure_loaded()
        model = FakeFasterModel.instances[0]
        self.assertEqual(model.name, "small")
        self.assertIs(model.kwargs["local_files_only"], True)         # скачивать нельзя
        self.assertEqual((model.kwargs["device"], model.kwargs["compute_type"]), ("cpu", "int8"))
        self.assertEqual(model.kwargs["cpu_threads"], 3)
        self.assertEqual(model.kwargs["download_root"], str(self.models / "faster-whisper"))

    def test_model_folder_is_used_when_present(self):
        folder = self.models / "faster-whisper" / "small"
        folder.mkdir(parents=True)
        engine = self.engine()
        engine.ensure_loaded()
        self.assertEqual(FakeFasterModel.instances[0].name, str(folder))

    def test_missing_model_is_not_installed(self):
        FakeFasterModel.error = FileNotFoundError("нет в кэше")
        with self.assertLogs("core.stt.whisper_local", level="WARNING"):
            with self.assertRaises(Unavailable) as ctx:
                self.engine().ensure_loaded()
        self.assertEqual(ctx.exception.kind, "not_installed")

    def test_scores_and_prompt(self):
        FakeFasterModel.segments = [types.SimpleNamespace(text=" открой сафари", no_speech_prob=0.7, avg_logprob=-0.2),
                                    types.SimpleNamespace(text=" и почту", no_speech_prob=0.1, avg_logprob=-0.3)]
        engine = self.engine()
        result = engine.recognize(make_audio(), "Джарвис.")
        options = FakeFasterModel.instances[0].calls[-1]
        self.assertEqual(options["initial_prompt"], "Джарвис.")
        self.assertEqual(options["language"], "ru")
        self.assertEqual(result.text, "и почту")                       # первый кусок: no_speech_prob 0,7 > 0,6

    def test_missing_package_message(self):
        with patch("core.stt.faster.importlib.import_module", side_effect=ImportError("нет")):
            with self.assertRaises(Unavailable) as ctx:
                tests_base.REAL_IMPORT_BACKEND["core.stt.faster"]()
        self.assertIn("faster-whisper", ctx.exception.reason)


# --- SpeechToText: откат, оповещения, журнал ---------------------------------------------------------------------
class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.clock = Clock()

    def stt(self, primary, fallback=None, provider=None):
        return SpeechToText(primary, fallback, provider, notify=self.said.append, clock=self.clock)


class TestFallback(ServiceCase):
    def test_local_result_is_used_without_google(self):
        google = FakeGoogle()
        stt = self.stt(ready_engine(good("открой сафари")), google)
        self.assertEqual(stt.recognize(make_audio()).text, "открой сафари")
        self.assertEqual(google.calls, [])
        self.assertEqual(self.said, [])

    def test_technical_error_falls_back_to_google_once(self):
        engine = ready_engine()
        engine.transcribe_error = RuntimeError("сбой")
        google = FakeGoogle("привет из google")
        stt = self.stt(engine, google)
        with self.assertLogs("core.stt.service", level="WARNING"):
            result = stt.recognize(make_audio())
        self.assertEqual((result.text, result.engine), ("привет из google", "google"))
        self.assertEqual(len(google.calls), 1)
        self.assertEqual(self.said, [])                         # по одной фразе молча

    def test_noise_does_not_go_to_google(self):
        google = FakeGoogle()
        stt = self.stt(ready_engine(good("Продолжение следует...")), google)
        with self.assertLogs("core.stt.service", level="INFO") as logs:
            self.assertIsNone(stt.recognize(make_audio()))
        self.assertEqual(google.calls, [])
        self.assertTrue(any("Отброшено (шум/галлюцинация)" in line for line in logs.output))

    def test_low_confidence_does_not_go_to_google(self):
        google = FakeGoogle()
        stt = self.stt(ready_engine(good("бормотание", avg_logprob=-2.0)), google)
        self.assertIsNone(stt.recognize(make_audio()))
        self.assertEqual(google.calls, [])

    def test_empty_result_does_not_go_to_google(self):
        google = FakeGoogle()
        stt = self.stt(ready_engine(), google)
        stt.primary.segments = []
        self.assertIsNone(stt.recognize(make_audio()))
        self.assertEqual(google.calls, [])

    def test_fallback_off_says_failed_and_sends_nothing(self):
        engine = ready_engine()
        engine.transcribe_error = RuntimeError("сбой")
        stt = self.stt(engine, None)
        with self.assertLogs("core.stt.service", level="WARNING"):
            self.assertIsNone(stt.recognize(make_audio()))
        self.assertEqual(self.said, [FAILED_REPLY])

    def test_fallback_error_says_failed(self):
        engine = ready_engine()
        engine.transcribe_error = RuntimeError("сбой")
        google = FakeGoogle(error=SttError("нет сети"))
        with self.assertLogs("core.stt.service", level="WARNING"):
            self.assertIsNone(self.stt(engine, google).recognize(make_audio()))
        self.assertEqual(len(google.calls), 1)                  # одна попытка на фразу
        self.assertEqual(self.said, [FAILED_REPLY])

    def test_failed_notice_is_rate_limited(self):
        engine = ready_engine()
        engine.transcribe_error = RuntimeError("сбой")
        stt = self.stt(engine, None)
        with self.assertLogs("core.stt.service", level="WARNING"):
            stt.recognize(make_audio())
            stt.recognize(make_audio())
            self.clock.now += 21
            stt.recognize(make_audio())
        self.assertEqual(self.said, [FAILED_REPLY, FAILED_REPLY])

    def test_google_primary_error_says_failed(self):
        google = FakeGoogle(error=SttError("нет сети"))
        with self.assertLogs("core.stt.service", level="WARNING"):
            self.assertIsNone(self.stt(google).recognize(make_audio()))
        self.assertEqual(self.said, [FAILED_REPLY])


class TestNotInstalled(ServiceCase):
    def engine(self, kind="not_installed"):
        return StubWhisper(load_error=Unavailable("нет пакета", kind=kind))

    def test_with_fallback_announces_once_and_works_on_google(self):
        google = FakeGoogle("привет из google")
        stt = self.stt(self.engine(), google)
        with self.assertLogs(level="WARNING"):
            first = stt.recognize(make_audio())
        second = stt.recognize(make_audio())
        self.assertEqual((first.text, second.text), ("привет из google", "привет из google"))
        self.assertEqual(self.said, [NOT_INSTALLED_FALLBACK])
        self.assertEqual(NOT_INSTALLED_FALLBACK, "Локальное распознавание не установлено, использую Google.")

    def test_fallback_off_announces_once_then_only_failed(self):
        stt = self.stt(self.engine(), None)
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(stt.recognize(make_audio()))
        self.assertIsNone(stt.recognize(make_audio()))
        self.clock.now += 100
        self.assertIsNone(stt.recognize(make_audio()))
        self.assertEqual(self.said, [NOT_INSTALLED_OFF, FAILED_REPLY, FAILED_REPLY])
        self.assertEqual(NOT_INSTALLED_OFF, "Локальное распознавание не установлено, откат на Google выключен.")
        self.assertNotIn("не установлено", FAILED_REPLY)         # причина больше не повторяется

    def test_off_sends_no_audio_to_google(self):
        google = FakeGoogle()
        stt = self.stt(self.engine(), None)
        with self.assertLogs(level="WARNING"):
            stt.recognize(make_audio())
        self.assertEqual(google.calls, [])

    def test_failed_load_has_its_own_wording(self):
        stt = self.stt(self.engine("failed"), FakeGoogle())
        with self.assertLogs(level="WARNING"):
            stt.recognize(make_audio())
        self.assertEqual(self.said, [FAILED_FALLBACK])
        stt = self.stt(self.engine("failed"), None)
        with self.assertLogs(level="WARNING"):
            stt.recognize(make_audio())
        self.assertEqual(self.said[-1], FAILED_OFF)


class TestLoadingState(ServiceCase):
    def loading_engine(self):
        engine = StubWhisper([good("локально")])
        engine.gate = threading.Event()
        self.addCleanup(engine.gate.set)
        engine.start_loading()
        return engine

    def test_with_fallback_google_works_until_model_is_ready(self):
        engine = self.loading_engine()
        google = FakeGoogle("пока из google")
        stt = self.stt(engine, google)
        self.assertEqual(stt.recognize(make_audio()).text, "пока из google")
        self.assertEqual(self.said, [])
        engine.gate.set()
        self.assertTrue(engine.wait_until("ready"))
        self.assertEqual(stt.recognize(make_audio()).text, "локально")
        self.assertEqual(len(google.calls), 1)

    def test_without_fallback_asks_to_wait(self):
        stt = self.stt(self.loading_engine(), None)
        self.assertIsNone(stt.recognize(make_audio()))
        self.assertIsNone(stt.recognize(make_audio()))
        self.assertEqual(self.said, [LOADING_REPLY])             # не чаще раза в 20 с
        self.assertEqual(LOADING_REPLY, "Распознавание загружается, секунду.")


class TestShortAndTiming(ServiceCase):
    def test_short_recording_dropped_for_every_engine(self):
        for engine in (ready_engine(), FakeGoogle()):
            with self.subTest(engine=engine.name):
                with self.assertLogs("core.stt.service", level="INFO") as logs:
                    result = self.stt(engine, FakeGoogle()).recognize(make_audio(0.3))
                self.assertIsNone(result)
                self.assertTrue(any("Отброшено (шум/галлюцинация)" in line for line in logs.output))
        self.assertEqual(len(ready_engine().calls), 1)           # только прогрев: до модели короткая запись не дошла

    def test_short_recording_never_reaches_google(self):
        google = FakeGoogle()
        engine = ready_engine()
        before = len(engine.calls)
        self.stt(engine, google).recognize(make_audio(0.39))
        self.assertEqual((len(engine.calls), google.calls), (before, []))

    def test_recording_at_the_limit_is_processed(self):
        self.assertEqual(self.stt(ready_engine(), None).recognize(make_audio(MIN_SECONDS)).text, "открой сафари")

    def test_timing_logged_without_text(self):
        clock = Clock()
        engine = ready_engine(good(SECRET_PHRASE), clock=clock)
        with self.assertLogs("core.stt", level="DEBUG") as logs:
            result = self.stt(engine, None).recognize(make_audio(3.0))
        self.assertEqual(result.text, SECRET_PHRASE)
        joined = "\n".join(logs.output)
        self.assertIn("Распознавание (stub): запись 3.0 с, обработка", joined)
        self.assertNotIn(SECRET_PHRASE, joined)

    def test_slow_recognition_warns(self):
        class Slow(StubWhisper):
            def _transcribe(self, model, pcm, prompt):
                if self.calls:
                    self.clock_ref.now += 9
                return super()._transcribe(model, pcm, prompt)

        clock = Clock()
        engine = Slow([good()], clock=clock)
        engine.clock_ref = clock
        engine.ensure_loaded()
        with self.assertLogs("core.stt.service", level="INFO") as logs:
            self.stt(engine, None).recognize(make_audio())
        self.assertTrue(any(line.startswith("WARNING") and "больше 8 с" in line for line in logs.output))

    def test_fast_recognition_does_not_warn(self):
        with self.assertLogs("core.stt.service", level="INFO") as logs:
            self.stt(ready_engine(), None).recognize(make_audio())
        self.assertFalse(any(line.startswith("WARNING") for line in logs.output))


# --- подсказка ---------------------------------------------------------------------------------------------------
class PromptCase(unittest.TestCase):
    def setUp(self):
        patcher = patch_installed()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(installed_apps.set_user_aliases, {})
        self.addCleanup(lambda: contacts.CONTACTS_FILE.unlink(missing_ok=True))

    def write_contacts(self, data):
        import json
        contacts.CONTACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        contacts.CONTACTS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestPrompt(PromptCase):
    """Подсказка — обычные командные предложения без меток и списков (Этап 8.1.2)."""

    def test_is_natural_sentences_in_the_expected_style(self):
        self.write_contacts({"мама": {"username": "mama_user"}})
        text = build_prompt()
        self.assertTrue(text.startswith("Джарвис, открой "))
        for part in ("Закрой ", "Напиши маме в телеграм привет.", "Поставь таймер на пять минут.", "Какая погода в Душанбе?"):
            self.assertIn(part, text)

    def test_no_colons_and_no_enumerations(self):
        self.write_contacts({"мама": {"username": "mama_user"}, "босс": {"username": "boss_user"}})
        for rotation in range(6):
            text = build_prompt(rotation)
            self.assertNotIn(":", text)
            self.assertLessEqual(text.count(","), 1)                    # только после «Джарвис»
            if "," in text:
                self.assertTrue(text.startswith("Джарвис,"))
            for label in ("Контакты", "Приложения", "Команды", "Программы", "Как я называю"):
                self.assertNotIn(label, text)

    def test_at_most_80_tokens_even_with_many_contacts(self):
        self.write_contacts({f"контакт номер {n} длинное имя": {"username": f"user_{n:05d}"} for n in range(80)})
        for rotation in range(5):
            text = build_prompt(rotation)
            self.assertLessEqual(estimate_tokens(text), PROMPT_TOKEN_LIMIT)
        self.assertEqual(PROMPT_TOKEN_LIMIT, 80)
        self.assertLessEqual(len(build_prompt()), 160)

    def test_contains_name_and_a_russian_app_name(self):
        text = build_prompt()
        self.assertIn("Джарвис", text)
        self.assertTrue(any(name in text for name in ("калькулятор", "сафари", "телеграм", "заметки")))

    def test_first_prompt_names_calculator_and_safari(self):
        text = build_prompt(0)
        self.assertIn("калькулятор", text)
        self.assertIn("сафари", text)

    def test_no_english_names_and_no_memory_aliases(self):
        installed_apps.set_user_aliases({"мой браузер": "Safari", "рабочий чат": "Telegram"})
        for rotation in range(5):
            text = build_prompt(rotation)
            for english in ("Safari", "Telegram", "Calculator", "Google Chrome", "YouTube", "Notes", "Mail"):
                self.assertNotIn(english, text)
            self.assertNotIn("мой браузер", text)
            self.assertNotIn("рабочий чат", text)

    def test_at_most_three_apps_and_two_contacts(self):
        self.write_contacts({k: {"username": f"user_{i:05d}"} for i, k in enumerate(("мама", "папа", "брат", "босс"))})
        apps = russian_app_names(installed_apps.list_installed_apps())
        self.assertGreater(len(apps), 3)
        for rotation in range(4):
            text = build_prompt(rotation)
            commands = [s for s in text.split(". ") if s.lower().startswith(("джарвис, открой", "закрой", "открой"))]
            self.assertLessEqual(len(commands), 3)
            self.assertLessEqual(text.count("Напиши "), 2)

    def test_not_installed_app_has_no_russian_name(self):
        with patch("core.installed_apps.list_installed_apps", return_value=["Safari", "Calculator"]):
            text = build_prompt()
        self.assertIn("сафари", text)
        self.assertNotIn("телеграм", text)
        self.assertNotIn("календарь", text)

    def test_without_apps_or_contacts_still_has_the_name(self):
        with patch("core.installed_apps.list_installed_apps", return_value=[]):
            text = build_prompt()
        self.assertTrue(text.startswith("Джарвис."))
        self.assertNotIn("Напиши", text)
        self.assertLessEqual(estimate_tokens(text), PROMPT_TOKEN_LIMIT)

    def test_one_app_fills_all_app_slots(self):
        with patch("core.installed_apps.list_installed_apps", return_value=["Safari"]):
            self.assertIn("Закрой сафари.", build_prompt())

    def test_no_username_phone_or_title(self):
        self.write_contacts({"мама": {"username": "mama_user", "phone": "+992900000001", "title": "Мама Иванова"},
                             "себе": {"username": "my_own_name", "title": "Избранное"}})
        for rotation in range(3):
            text = build_prompt(rotation)
            for private in ("mama_user", "992900000001", "my_own_name", "Иванова", "Избранное", "@"):
                self.assertNotIn(private, text)
        self.assertIn("маме", build_prompt(0))

    def test_contact_keys_that_look_like_username_or_phone_are_skipped(self):
        self.write_contacts({"@durov_x": {"username": "durov_x"}, "89001234567": {"phone": "89001234567"},
                             "босс": {"username": "boss_user"}})
        self.assertEqual(contact_names(), ["босс"])
        text = build_prompt()
        self.assertIn("боссу", text)
        self.assertNotIn("durov", text)
        self.assertNotIn("89001234567", text)

    def test_missing_contacts_file_is_not_created(self):
        contacts.CONTACTS_FILE.unlink(missing_ok=True)
        build_prompt()
        self.assertFalse(contacts.CONTACTS_FILE.exists())

    def test_rotation_changes_apps_and_contacts(self):
        self.write_contacts({k: {"username": f"user_{i:05d}"} for i, k in enumerate(("мама", "папа", "брат", "босс"))})
        prompts_ = {build_prompt(r) for r in range(4)}
        self.assertGreater(len(prompts_), 1)
        self.assertEqual(build_prompt(2), build_prompt(2))           # одна и та же ротация — тот же текст

    def test_provider_rotates_on_every_refresh_and_caches_in_between(self):
        clock, calls = Clock(), []
        provider = PromptProvider(build=lambda rotation: calls.append(rotation) or f"промпт {rotation}", clock=clock)
        self.assertEqual(provider.get(), "промпт 0")
        clock.now += 599
        self.assertEqual(provider.get(), "промпт 0")
        clock.now += 2
        self.assertEqual(provider.get(), "промпт 1")
        clock.now += 601
        self.assertEqual(provider.get(), "промпт 2")
        self.assertEqual(calls, [0, 1, 2])

    def test_real_provider_changes_the_prompt_after_ten_minutes(self):
        clock = Clock()
        provider = PromptProvider(clock=clock)
        first = provider.get()
        clock.now += 601
        self.assertNotEqual(provider.get(), first)

    def test_spoken_extras_only_if_installed(self):
        installed = ["WhatsApp", "YouTube", "Google Chrome", "Viber", "zoom.us", "Slack", "Spotify"]
        names = russian_app_names(installed)
        for spoken in ("ватсап", "ютуб", "хром", "вайбер", "зум", "слак"):
            self.assertIn(spoken, names)
        self.assertEqual(russian_app_names(["Safari"]), ["сафари"])
        self.assertEqual(russian_app_names([]), [])

    def test_one_name_per_app(self):
        names = russian_app_names(["Finder", "WhatsApp"])
        self.assertEqual(sum(1 for n in names if n in ("файндер", "финдер")), 1)
        self.assertEqual(sum(1 for n in names if n in ("ватсап", "вотсап")), 1)

    def test_extras_do_not_touch_app_aliases(self):
        from core.apps import APP_ALIASES
        before = dict(APP_ALIASES)
        build_prompt()
        self.assertEqual(APP_ALIASES, before)
        for spoken in ("ватсап", "ютуб", "вайбер", "зум", "слак"):
            self.assertNotIn(spoken, APP_ALIASES)


class TestPromptCity(PromptCase):
    def city_sentence(self, env):
        with patch("core.settings.ENV_FILE", env_file(env)):
            return weather_sentence()

    def test_default_city_is_dushanbe(self):
        self.assertEqual(self.city_sentence(""), "Какая погода в Душанбе?")
        self.assertIn("Какая погода в Душанбе?", build_prompt())

    def test_city_comes_from_default_city_setting(self):
        self.assertEqual(self.city_sentence("DEFAULT_CITY=Москва\n"), "Какая погода в Москве?")
        self.assertEqual(self.city_sentence("DEFAULT_CITY=Худжанд\n"), "Какая погода в Худжанде?")
        self.assertEqual(self.city_sentence("DEFAULT_CITY=Сочи\n"), "Какая погода в Сочи?")
        with patch("core.settings.ENV_FILE", env_file("DEFAULT_CITY=Москва\n")):
            text = build_prompt()
        self.assertIn("Какая погода в Москве?", text)
        self.assertNotIn("Душанбе", text)

    def test_city_that_cannot_be_declined_is_left_out(self):
        for city in ("Нижний Новгород", "London", "Санкт-Петербург"):
            with self.subTest(city=city):
                self.assertEqual(self.city_sentence(f"DEFAULT_CITY={city}\n"), "Какая сегодня погода?")


class TestCases(unittest.TestCase):
    def test_dative(self):
        for key, expected in (("мама", "маме"), ("Аня", "Ане"), ("Иван", "Ивану"), ("босс", "боссу"), ("брат", "брату"),
                              ("Мария", "Марии"), ("Игорь", "Игорю"), ("Андрей", "Андрею"), ("себе", "себе"),
                              ("Таню", "Таню"), ("my boss", "my boss"), ("жена мужа", "жена мужа"), ("ma", "ma")):
            with self.subTest(key=key):
                self.assertEqual(dative(key), expected)

    def test_accusative(self):
        for name, expected in (("почта", "почту"), ("музыка", "музыку"), ("калькулятор", "калькулятор"),
                               ("сафари", "сафари"), ("заметки", "заметки"), ("фото", "фото")):
            with self.subTest(name=name):
                self.assertEqual(accusative(name), expected)

    def test_prepositional(self):
        for city, expected in (("Душанбе", "Душанбе"), ("Москва", "Москве"), ("Лондон", "Лондоне"), ("Казань", "Казани"),
                               ("Рязань", "Рязани"), ("Сочи", "Сочи"), ("Нижний Новгород", None), ("Paris", None)):
            with self.subTest(city=city):
                self.assertEqual(prepositional(city), expected)


class TestPromptSetting(unittest.TestCase):
    def build(self, text):
        with patch("core.settings.ENV_FILE", env_file(text)):
            return factory.build_stt(MagicMock(), notify=lambda text: None)

    def test_on_by_default(self):
        self.assertTrue(factory.SttSettings.__dataclass_fields__["prompt"])
        with patch("core.settings.ENV_FILE", env_file("")):
            self.assertTrue(factory.stt_settings().prompt)
        self.assertIsInstance(self.build("").prompt_provider, PromptProvider)
        self.assertIsInstance(self.build("STT_PROMPT=on\n").prompt_provider, PromptProvider)

    def test_off_builds_no_provider(self):
        for raw in ("off", "OFF", "no", "false", "0", "none"):
            with self.subTest(raw=raw):
                self.assertIsNone(self.build(f"STT_PROMPT={raw}\n").prompt_provider)

    def test_off_means_the_engine_gets_an_empty_prompt(self):
        engine = ready_engine()
        stt = SpeechToText(engine, None, self.build("STT_PROMPT=off\n").prompt_provider, notify=lambda text: None)
        stt.recognize(make_audio())
        self.assertEqual(engine.calls[-1][1], "")

    def test_on_means_the_engine_gets_the_prompt(self):
        engine = ready_engine()
        with patch_installed():
            stt = SpeechToText(engine, None, self.build("").prompt_provider, notify=lambda text: None)
            stt.recognize(make_audio())
        self.assertTrue(engine.calls[-1][1].startswith("Джарвис"))

    def test_off_with_google_fallback_nothing_changes(self):
        stt = self.build("STT_PROMPT=off\nSTT_FALLBACK=google\n")
        self.assertEqual(stt.fallback.name, "google")
        self.assertIsNone(stt.prompt_provider)

    def test_env_example_has_the_setting(self):
        self.assertIn("STT_PROMPT=on", Path(__file__).with_name(".env.example").read_text(encoding="utf-8"))


class TestPromptNeverGoesToGoogle(ServiceCase, PromptCase):
    def setUp(self):
        ServiceCase.setUp(self)
        PromptCase.setUp(self)

    def test_prompt_is_passed_to_local_engine_only(self):
        provider = MagicMock()
        provider.get.return_value = "Джарвис. Контакты: мама."
        engine = ready_engine()
        google = FakeGoogle()
        self.stt(engine, google, provider).recognize(make_audio())
        self.assertEqual(engine.calls[-1][1], "Джарвис. Контакты: мама.")
        self.assertEqual(google.calls, [])

    def test_fallback_to_google_sends_no_prompt(self):
        self.write_contacts({"мама": {"username": "mama_user"}})
        engine = ready_engine()
        engine.transcribe_error = RuntimeError("сбой")
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "привет"
        stt = self.stt(engine, GoogleEngine(recognizer), PromptProvider())
        with self.assertLogs("core.stt.service", level="WARNING"):
            stt.recognize(make_audio())
        args, kwargs = recognizer.recognize_google.call_args
        self.assertEqual(set(kwargs), {"language"})
        self.assertEqual(len(args), 1)
        self.assertNotIn("Джарвис", repr(args) + repr(kwargs))
        self.assertNotIn("мама", repr(args) + repr(kwargs))

    def test_google_primary_does_not_even_build_the_prompt(self):
        provider = MagicMock()
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "привет"
        self.stt(GoogleEngine(recognizer), None, provider).recognize(make_audio())
        provider.get.assert_not_called()

    def test_google_engine_ignores_a_prompt_if_given(self):
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "привет"
        GoogleEngine(recognizer).recognize(make_audio(), prompt="Джарвис. Контакты: мама.")
        self.assertNotIn("Джарвис", repr(recognizer.recognize_google.call_args))


class TestPromptEcho(PromptCase):
    """Этап 8.1.2: метки и эхо подсказки в распознанном тексте (строки из живого лога)."""
    PROMPT = "Джарвис, открой калькулятор. Закрой сафари. Поставь таймер на пять минут. Какая погода в Душанбе?"

    def recognize(self, text, prompt=PROMPT, seconds=6.0, avg_logprob=-0.2, engine=None):
        engine = engine or ready_engine(good(text, avg_logprob=avg_logprob))
        return engine, engine.recognize(make_audio(seconds), prompt)

    def test_labels_from_the_live_log_are_stripped(self):
        for raw, expected in (("Контакты: Да.", "Да."), ("Контакты: закрой, как вы видите?", "закрой, как вы видите?"),
                              ("Контакты: Светилик.", "Светилик.")):
            with self.subTest(raw=raw):
                with self.assertLogs("core.stt.whisper_local", level="INFO"):
                    _, result = self.recognize(raw)
                self.assertEqual((result.text, result.discarded), (expected, None))

    def test_every_label_in_any_case_and_place(self):
        for raw, expected in (("контакты: закрой", "закрой"), ("КОНТАКТЫ:закрой", "закрой"), ("Приложения: открой",
                              "открой"), ("Команды: поставь таймер", "поставь таймер"), ("Программы: открой сафари",
                              "открой сафари"), ("Как я называю приложения: открой почту", "открой почту"),
                              ("закрой Контакты: сафари", "закрой сафари"), ("открой Приложения: почту", "открой почту"),
                              ("Контакты: закрой Команды: сафари", "закрой сафари")):
            with self.subTest(raw=raw):
                with self.assertLogs("core.stt.whisper_local", level="INFO"):
                    _, result = self.recognize(raw)
                self.assertEqual(result.text, expected)

    def test_the_word_without_a_colon_is_not_a_label(self):
        for raw in ("закрой контакты", "открой приложения", "покажи команды", "открой программы"):
            with self.subTest(raw=raw):
                _, result = self.recognize(raw)
                self.assertEqual(result.text, raw)

    def test_only_a_label_is_dropped_as_echo(self):
        with self.assertLogs("core.stt.whisper_local", level="INFO") as logs:
            _, result = self.recognize("Контакты:")
        self.assertEqual((result.text, result.discarded), ("", "prompt_echo"))
        self.assertIn("INFO:core.stt.whisper_local:Отброшено (эхо подсказки), всего за запуск: 1", logs.output)

    def test_nothing_recognized_is_not_an_echo(self):
        engine = ready_engine()
        engine.segments = []
        result = engine.recognize(make_audio(), self.PROMPT)
        self.assertEqual((result.text, result.discarded), ("", None))
        self.assertEqual(engine.echo_total, 0)

    def test_whole_prompt_sentence_on_a_short_recording_is_echo(self):
        # 14–28 символов текста на 0,5 с записи: сказать столько невозможно
        for text in ("Закрой сафари.", "Какая погода в Душанбе?", "Поставь таймер на пять минут", "открой калькулятор",
                     "Джарвис, открой калькулятор."):
            with self.subTest(text=text):
                with self.assertLogs("core.stt.whisper_local", level="INFO"):
                    _, result = self.recognize(text, seconds=0.5)
                self.assertEqual((result.text, result.discarded), ("", "prompt_echo"))

    def test_whole_prompt_sentence_with_low_confidence_is_echo(self):
        with self.assertLogs("core.stt.whisper_local", level="INFO"):
            _, result = self.recognize("Закрой сафари.", seconds=5.0, avg_logprob=math.log(0.4))
        self.assertEqual((result.text, result.discarded), ("", "prompt_echo"))

    def test_real_command_equal_to_a_prompt_sentence_is_kept(self):
        for text in ("Закрой сафари.", "поставь таймер на пять минут", "Какая погода в Душанбе?"):
            with self.subTest(text=text):
                engine, result = self.recognize(text, seconds=3.5, avg_logprob=math.log(0.9))
                self.assertEqual((result.text, result.discarded), (text, None))
                self.assertEqual(engine.echo_total, 0)

    def test_text_that_only_resembles_a_prompt_sentence_is_kept(self):
        for text in ("закрой телеграм", "поставь таймер на десять минут", "какая погода в Москве"):
            with self.subTest(text=text):
                _, result = self.recognize(text, seconds=0.9, avg_logprob=math.log(0.4))
                self.assertEqual(result.text, text)

    def test_thresholds_are_named_constants(self):
        self.assertEqual(prompt_echo.ECHO_MAX_CHARS_PER_SECOND, 20)
        self.assertEqual(prompt_echo.ECHO_MAX_CONFIDENCE, 0.5)
        source = Path(prompt_echo.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("Оценочные, подобрать по логу"), 2)

    def test_no_prompt_means_no_echo_check_but_labels_still_stripped(self):
        engine = ready_engine(good("Закрой сафари.", avg_logprob=math.log(0.4)))
        self.assertEqual(engine.recognize(make_audio(0.9), "").text, "Закрой сафари.")
        engine = ready_engine(good("Контакты: Да."))
        with self.assertLogs("core.stt.whisper_local", level="INFO"):
            self.assertEqual(engine.recognize(make_audio(), "").text, "Да.")

    def test_counters_and_log_lines_without_text(self):
        engine = ready_engine(good("Контакты: Светилик."))
        with self.assertLogs("core.stt.whisper_local", level="INFO") as logs:
            engine.recognize(make_audio(), self.PROMPT)
            engine.recognize(make_audio(), self.PROMPT)
            engine.segments = [good("Закрой сафари.")]
            engine.recognize(make_audio(0.5), self.PROMPT)
            engine.recognize(make_audio(0.5), self.PROMPT)
        self.assertEqual((engine.labels_total, engine.echo_total), (2, 2))
        self.assertIn("INFO:core.stt.whisper_local:Убраны метки подсказки, всего: 1", logs.output)
        self.assertIn("INFO:core.stt.whisper_local:Убраны метки подсказки, всего: 2", logs.output)
        self.assertIn("INFO:core.stt.whisper_local:Отброшено (эхо подсказки), всего за запуск: 1", logs.output)
        self.assertIn("INFO:core.stt.whisper_local:Отброшено (эхо подсказки), всего за запуск: 2", logs.output)
        joined = "\n".join(logs.output)
        for secret in ("Светилик", "Закрой сафари"):
            self.assertNotIn(secret, joined)

    def test_service_does_not_double_log_the_echo(self):
        engine = ready_engine(good("Закрой сафари."))
        stt = SpeechToText(engine, FakeGoogle(), type("P", (), {"get": lambda self: self_prompt})(), notify=lambda t: None)
        self_prompt = self.PROMPT
        with self.assertLogs("core.stt", level="INFO") as logs:
            self.assertIsNone(stt.recognize(make_audio(0.5)))
        joined = "\n".join(logs.output)
        self.assertEqual(joined.count("Отброшено (эхо подсказки)"), 1)
        self.assertNotIn("шум/галлюцинация", joined)

    def test_echo_does_not_go_to_google(self):
        engine = ready_engine(good("Закрой сафари."))
        google = FakeGoogle()
        provider = type("P", (), {"get": lambda self: TestPromptEcho.PROMPT})()
        with self.assertLogs("core.stt", level="INFO"):
            SpeechToText(engine, google, provider, notify=lambda t: None).recognize(make_audio(0.5))
        self.assertEqual(google.calls, [])


class TestEchoStreakWarning(unittest.TestCase):
    PROMPT = TestPromptEcho.PROMPT
    WARNING = ("Подсказка искажает распознавание; сравните: python -m core.stt.check --mic --no-prompt, "
               "или STT_PROMPT=off")

    def run_phrases(self, texts, prompt=PROMPT):
        engine = ready_engine()
        warnings = []
        for text in texts:
            engine.segments = [good(text)]
            with self.assertLogs("core.stt.whisper_local", level="DEBUG") as logs:
                logging.getLogger("core.stt.whisper_local").debug("отметка")
                engine.recognize(make_audio(6.0), prompt)
            warnings += [line for line in logs.output if line.startswith("WARNING")]
        return warnings

    def test_three_phrases_in_a_row_warn_once(self):
        warnings = self.run_phrases(["Контакты: Да.", "Контакты: закрой", "Контакты: Светилик."])
        self.assertEqual(warnings, [f"WARNING:core.stt.whisper_local:{self.WARNING}"])

    def test_mixed_labels_and_echo_count_together(self):
        warnings = self.run_phrases(["Контакты: Да.", "Контакты:", "Контакты: закрой"])
        self.assertEqual(len(warnings), 1)

    def test_a_clean_phrase_resets_the_streak(self):
        warnings = self.run_phrases(["Контакты: Да.", "Контакты: закрой", "открой сафари", "Контакты: Светилик.",
                                     "Контакты: нет"])
        self.assertEqual(warnings, [])

    def test_four_in_a_row_warns_once_then_starts_over(self):
        warnings = self.run_phrases(["Контакты: а"] * 4)
        self.assertEqual(len(warnings), 1)
        warnings = self.run_phrases(["Контакты: а"] * 6)
        self.assertEqual(len(warnings), 2)

    def test_two_phrases_are_not_enough(self):
        self.assertEqual(self.run_phrases(["Контакты: Да.", "Контакты: закрой"]), [])

    def test_no_warning_when_the_prompt_is_off(self):
        self.assertEqual(self.run_phrases(["Контакты: Да."] * 5, prompt=""), [])

    def test_noise_discarded_phrases_do_not_break_the_streak(self):
        engine = ready_engine()
        with self.assertLogs("core.stt.whisper_local", level="INFO") as logs:
            for text in ("Контакты: Да.", "Контакты: закрой"):
                engine.segments = [good(text)]
                engine.recognize(make_audio(6.0), self.PROMPT)
            engine.segments = [good("Продолжение следует...")]       # шум: отброшен фильтром, серия не прервана
            engine.recognize(make_audio(6.0), self.PROMPT)
            engine.segments = [good("Контакты: Светилик.")]
            engine.recognize(make_audio(6.0), self.PROMPT)
        self.assertEqual(sum(1 for line in logs.output if line.startswith("WARNING")), 1)

    def test_warning_text_names_the_remedies(self):
        self.assertIn("--no-prompt", self.WARNING)
        self.assertIn("STT_PROMPT=off", self.WARNING)


class TestCollapseRepeats(unittest.TestCase):
    def test_same_sentence_repeated_keeps_one(self):
        self.assertEqual(collapse_repeats("Открой калькулятор. Открой калькулятор."), "Открой калькулятор.")
        self.assertEqual(collapse_repeats("открой калькулятор. Открой Калькулятор! открой калькулятор"), "открой калькулятор.")
        self.assertEqual(collapse_repeats("Включи свет в комнате. Включи свет в комнате. Включи свет в комнате."),
                         "Включи свет в комнате.")

    def test_phrase_repeated_three_times_keeps_one(self):
        self.assertEqual(collapse_repeats("включи свет включи свет включи свет"), "включи свет")
        self.assertEqual(collapse_repeats("включи свет, включи свет, включи свет."), "включи свет.")
        self.assertEqual(collapse_repeats("скажи включи свет включи свет включи свет сейчас"), "скажи включи свет сейчас")
        self.assertEqual(collapse_repeats("Открой почту. Открой почту. Открой почту."), "Открой почту.")
        self.assertEqual(collapse_repeats("a б в a б в a б в a б в"), "a б в")

    def test_comparison_ignores_case_yo_and_punctuation(self):
        self.assertEqual(collapse_repeats("Всё сделано. всё, сделано!"), "Всё сделано.")

    def test_single_word_repeats_are_never_touched(self):
        for text in ("громче громче громче", "Громче, громче, громче!", "стоп стоп", "Стоп. Стоп.", "тише тише тише тише"):
            with self.subTest(text=text):
                self.assertEqual(collapse_repeats(text), text)

    def test_ordinary_text_is_unchanged(self):
        for text in ("открой калькулятор", "Открой калькулятор. Закрой сафари.", "раз два раз два", "включи свет включи свет",
                     "поставь таймер на пять минут", "да да нет нет да да", "что-то", "", "   ", "Открой калькулятор. Открой сафари."):
            with self.subTest(text=text):
                self.assertEqual(collapse_repeats(text), text)

    def test_two_copies_of_a_phrase_without_sentence_ends_stay(self):
        self.assertEqual(collapse_repeats("позвони маме позвони маме"), "позвони маме позвони маме")

    def test_applied_to_local_whisper_result_and_logged_without_text(self):
        engine = ready_engine(good("Открой калькулятор. Открой калькулятор."))
        with self.assertLogs("core.stt.whisper_local", level="INFO") as logs:
            result = engine.recognize(make_audio())
        self.assertEqual(result.text, "Открой калькулятор.")
        self.assertNotIn("калькулятор", "\n".join(logs.output))

    def test_repeat_across_segments(self):
        engine = ready_engine(good("Открой калькулятор."), good("Открой калькулятор."))
        self.assertEqual(engine.recognize(make_audio()).text, "Открой калькулятор.")

    def test_repeated_single_word_survives_the_engine(self):
        self.assertEqual(ready_engine(good("громче громче громче")).recognize(make_audio()).text, "громче громче громче")

    def test_google_text_is_not_collapsed(self):
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "Открой калькулятор. Открой калькулятор."
        self.assertEqual(GoogleEngine(recognizer).recognize(make_audio()).text, "Открой калькулятор. Открой калькулятор.")


# --- обрезанные фразы --------------------------------------------------------------------------------------------
class TestTruncated(unittest.TestCase):
    def test_cut_phrases(self):
        for text in ("позвонить ма", "напиши себе в", "открой чат с", "Открой чат с.", "включи свет в", "скрыть бо",
                     "расскажи о", "открой и", "поставь будильник на", "напиши маме и"):
            with self.subTest(text=text):
                self.assertTrue(is_truncated(text))

    def test_normal_phrases(self):
        for text in ("кто я", "включи тв", "поставь на 5", "открой ватсап", "открой сафари", "громче", "пауза",
                     "открой вк", "что играет", "погода в душанбе", "включи музыку", "позвонить маме",
                     "а ты кто", "кто мы", "открой ютуб", "напиши себе привет"):
            with self.subTest(text=text):
                self.assertFalse(is_truncated(text))

    def test_one_word_is_never_truncated(self):
        for text in ("в", "ма", "и", "", "   "):
            self.assertFalse(is_truncated(text))

    def test_exceptions_never_cut(self):
        for word in ("я", "ты", "мы", "вы", "он", "да", "нет", "ок", "ну", "тв", "вк", "стоп", "хватит", "отмена",
                     "подтверждаю"):
            with self.subTest(word=word):
                self.assertFalse(is_truncated(f"слушай {word}"))

    def test_numbers_never_cut(self):
        for text in ("поставь на 5", "громкость 50", "таймер на 10", "поставь 7", "кто 1"):
            self.assertFalse(is_truncated(text))

    def test_confirmation_and_control_phrases(self):
        for text in ("да", "нет", "стоп", "хватит", "отмена", "подтверждаю", "ну да", "нет нет", "да да", "стоп хватит",
                     "не надо", "нет отмена"):
            with self.subTest(text=text):
                self.assertFalse(is_truncated(text))

    def test_reply_text(self):
        self.assertEqual(TRUNCATED_REPLY, "Фраза обрезана. Повторите, пожалуйста.")


class AppCase(unittest.TestCase):
    def app(self, pending=False):
        app = MagicMock()
        app.has_pending.return_value = pending
        app.ai.bare_confirmation_reply.return_value = None
        app.ai.quick_confirm.return_value = None
        app.mac.execute.return_value = None
        return app


class TestTruncatedInMain(AppCase):
    def test_cut_voice_phrase_asks_to_repeat_and_skips_everything(self):
        for text in ("позвонить ма", "напиши себе в", "открой чат с"):
            with self.subTest(text=text):
                app = self.app()
                main.VoiceApp.process_command(app, text)
                app.engine.speak.assert_called_once_with(TRUNCATED_REPLY)
                app.ai.respond.assert_not_called()
                app.ai.quick_confirm.assert_not_called()
                app.mac.execute.assert_not_called()

    def test_normal_phrase_goes_on(self):
        app = self.app()
        app.mac.execute.return_value = "Открыто."
        main.VoiceApp.process_command(app, "открой ватсап")
        app.engine.speak.assert_called_once_with("Открыто.")

    def test_typed_text_is_not_checked(self):
        app = self.app()
        app.mac.execute.return_value = "ok"
        main.VoiceApp.process_command(app, "позвонить ма", source="text")
        app.engine.speak.assert_called_once_with("ok")

    def test_answer_to_pending_confirmation_is_not_checked(self):
        app = self.app(pending=True)
        main.VoiceApp.process_command(app, "да отправляй на")
        app.handle_confirmation.assert_called_once_with("да отправляй на")
        app.engine.speak.assert_not_called()

    def test_control_words_are_not_cut(self):
        for text in ("да", "нет", "стоп"):
            app = self.app()
            main.VoiceApp.process_command(app, text)
            for call in app.engine.speak.call_args_list:
                self.assertNotEqual(call.args[0], TRUNCATED_REPLY)

    def test_cut_phrase_logged_without_text(self):
        app = self.app()
        with self.assertLogs("main", level="INFO") as logs:
            main.VoiceApp.process_command(app, "позвонить ма")
        self.assertTrue(any("Отброшено (обрезанная фраза)" in line for line in logs.output))


# --- SpeechEngine.listen -----------------------------------------------------------------------------------------
@patch("core.engine._detect_voice", return_value="Milena")
@patch("core.engine.sr.Microphone")
class TestListen(unittest.TestCase):
    def engine(self, text="Открой Сафари"):
        engine = SpeechEngine()
        self.events = []
        engine.recognizer = MagicMock()
        engine.recognizer.listen.return_value = sr.AudioData(b"\x01\x00" * 32000, 16000, 2)
        engine.microphone.__exit__.side_effect = lambda *a: self.events.append("mic_closed") and False
        engine.stt = MagicMock()
        engine.stt.recognize.side_effect = lambda audio: (self.events.append("recognize"),
                                                           Recognition(text, "stub") if text else None)[1]
        return engine

    def test_text_is_lowercased_and_comes_from_stt(self, *_):
        self.assertEqual(self.engine().listen(), "открой сафари")

    def test_nothing_recognized(self, *_):
        self.assertIsNone(self.engine(text="").listen())

    def test_phrase_limit_defaults_to_twenty_seconds(self, *_):
        engine = self.engine()
        engine.listen()
        engine.recognizer.listen.assert_called_once_with(engine.microphone.__enter__.return_value, timeout=5,
                                                         phrase_time_limit=20.0)

    def test_phrase_limit_from_env_and_pause_unchanged(self, *_):
        with patch("core.settings.ENV_FILE", env_file("SPEECH_PHRASE_LIMIT=12\nSPEECH_PAUSE=1.5\n")):
            engine = SpeechEngine()
        self.assertEqual(engine.phrase_limit, 12.0)
        self.assertEqual(engine.recognizer.pause_threshold, 1.5)

    def test_microphone_is_released_before_recognition(self, *_):
        self.engine().listen()
        self.assertEqual(self.events, ["mic_closed", "recognize"])

    def test_wait_timeout_is_silence(self, *_):
        engine = self.engine()
        engine.recognizer.listen.side_effect = sr.WaitTimeoutError()
        self.assertIsNone(engine.listen())
        engine.stt.recognize.assert_not_called()

    def test_recognition_crash_does_not_kill_the_loop(self, *_):
        engine = self.engine()
        engine.stt.recognize.side_effect = RuntimeError("сбой")
        with self.assertLogs("core.engine", level="ERROR"):
            self.assertIsNone(engine.listen())

    @patch("core.engine.subprocess.Popen")
    def test_notices_are_spoken_after_listen_not_inside_it(self, popen, *_):
        engine = SpeechEngine()
        engine.recognizer = MagicMock()
        engine.recognizer.listen.return_value = sr.AudioData(b"\x01\x00" * 32000, 16000, 2)
        engine.stt.primary = StubWhisper(load_error=Unavailable("нет пакета"))
        engine.stt.fallback = FakeGoogle("открой сафари")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(engine.listen(), "открой сафари")
        popen.assert_not_called()                        # внутри listen() ничего не звучит: фраза не станет «эхом во время записи»
        self.assertEqual(engine.notices, [NOT_INSTALLED_FALLBACK])
        engine.flush_notices()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(engine.notices, [])
        engine.flush_notices()
        self.assertEqual(popen.call_count, 1)

    def test_engine_builds_stt_but_loads_nothing(self, *_):
        engine = SpeechEngine()
        self.assertEqual(engine.stt.primary.state, "idle")

    def test_start_stt_starts_background_loading(self, *_):
        engine = SpeechEngine()
        with patch.object(engine.stt, "start") as start:
            engine.start_stt()
        start.assert_called_once_with()

    def test_recognized_text_is_not_logged_by_listen(self, *_):
        """Текст пишет в лог только main, после проверки слова-активатора (отброшенное не пишется нигде)."""
        engine = self.engine("напиши маме в телеграм привет")
        with self.assertNoLogs("core.engine", level="INFO"):
            self.assertEqual(engine.listen(), "напиши маме в телеграм привет")


# --- check.py ----------------------------------------------------------------------------------------------------
class TestCheckCli(unittest.TestCase):
    def run_check(self, env, *args, engine=None, installed=True):
        lines = []
        patches = [patch("core.settings.ENV_FILE", env_file(env)),
                   patch("core.stt.check.importlib.util.find_spec", return_value=object() if installed else None),
                   patch_installed()]
        if engine is not None:
            patches.append(patch("core.stt.check.local_engine", return_value=engine))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return check.main(list(args), out=lines.append), "\n".join(lines)

    def test_google_has_nothing_to_check(self):
        code, text = self.run_check("STT_ENGINE=google\n")
        self.assertEqual(code, 0)
        self.assertIn("Выбран Google", text)

    def test_loads_and_reports_without_microphone(self):
        engine = StubWhisper([good()])
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        code, text = self.run_check("", engine=engine)
        self.assertEqual(code, 0)
        self.assertIn("Модель загружена и прогрета", text)
        self.assertIn("откат на Google: да", text)

    def test_missing_model_reports_and_fails(self):
        engine = StubWhisper(load_error=Unavailable("модель не найдена: /x"))
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        with self.assertLogs("core.stt.whisper_local", level="WARNING"):
            code, text = self.run_check("", engine=engine, installed=False)
        self.assertEqual(code, 1)
        self.assertIn("НЕ установлен", text)
        self.assertIn("ФАЙЛА НЕТ", text)
        self.assertIn("модель не найдена", text)

    def test_wav_is_recognized_and_text_printed(self):
        engine = StubWhisper([good("открой сафари", avg_logprob=-0.2)])
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        with patch("core.stt.check.read_wav", return_value=make_audio(2.0)):
            code, text = self.run_check("", "--wav", "файл.wav", engine=engine)
        self.assertEqual(code, 0)
        self.assertIn("[1] текст: открой сафари", text)
        self.assertIn("[1] запись 2.0 с", text)

    def test_check_never_falls_back_to_google(self):
        engine = StubWhisper([good()])
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        with patch("core.stt.check.read_wav", return_value=make_audio(2.0)), \
                patch("core.stt.google.GoogleEngine.recognize") as google:
            self.run_check("", "--wav", "файл.wav", engine=engine)
        google.assert_not_called()

    def mic_engine(self, texts):
        engine = StubWhisper([good(texts[0])])
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        queue = list(texts)
        original = engine._transcribe

        def transcribe(model, pcm, prompt):
            if engine.calls:
                engine.segments = [good(queue.pop(0))]
            return original(model, pcm, prompt)
        engine._transcribe = transcribe
        return engine

    def test_mic_records_three_phrases_by_default(self):
        engine = self.mic_engine(["открой сафари", "закрой почту", "который час"])
        recorded = []

        def record(count):
            recorded.append(count)
            for _ in range(count):
                yield make_audio(2.0)
        with patch("core.stt.check.record_phrases", side_effect=record), patch_installed():
            code, text = self.run_check("", "--mic", engine=engine)
        self.assertEqual((code, recorded), (0, [3]))
        for line in ("[1] текст: открой сафари", "[2] текст: закрой почту", "[3] текст: который час"):
            self.assertIn(line, text)
        self.assertIn("Подсказка Whisper (", text)
        self.assertIn("Джарвис, открой ", text)

    def test_repeat_sets_the_number_of_phrases(self):
        engine = self.mic_engine(["раз", "два"])
        with patch("core.stt.check.record_phrases", side_effect=lambda n: (make_audio() for _ in range(n))) as record, \
                patch_installed():
            self.run_check("", "--mic", "--repeat", "2", engine=engine)
        record.assert_called_once_with(2)

    def test_no_prompt_passes_an_empty_prompt(self):
        engine = self.mic_engine(["открой сафари", "закрой почту", "который час"])
        with patch("core.stt.check.record_phrases", side_effect=lambda n: (make_audio() for _ in range(n))), patch_installed():
            code, text = self.run_check("", "--mic", "--no-prompt", engine=engine)
        self.assertIn("выключена (--no-prompt)", text)
        self.assertNotIn("Джарвис, открой", text)
        self.assertEqual([prompt for _, prompt in engine.calls[1:]], ["", "", ""])      # [0] — прогрев

    def test_with_prompt_the_engine_receives_it(self):
        engine = self.mic_engine(["открой сафари", "закрой почту", "который час"])
        with patch("core.stt.check.record_phrases", side_effect=lambda n: (make_audio() for _ in range(n))), patch_installed():
            self.run_check("", "--mic", engine=engine)
        for _, prompt in engine.calls[1:]:
            self.assertTrue(prompt.startswith("Джарвис, открой "))

    def test_stt_prompt_off_in_env_disables_the_prompt(self):
        engine = self.mic_engine(["открой сафари", "закрой почту", "который час"])
        with patch("core.stt.check.record_phrases", side_effect=lambda n: (make_audio() for _ in range(n))):
            code, text = self.run_check("STT_PROMPT=off\n", "--mic", engine=engine)
        self.assertIn("выключена (STT_PROMPT=off в .env)", text)
        self.assertEqual([prompt for _, prompt in engine.calls[1:]], ["", "", ""])

    def test_echo_is_reported_as_discarded(self):
        engine = self.mic_engine(["Контакты:"])
        with patch("core.stt.check.record_phrases", side_effect=lambda n: (make_audio() for _ in range(1))), patch_installed(), \
                self.assertLogs("core.stt.whisper_local", level="INFO"):
            code, text = self.run_check("", "--mic", "--repeat", "1", engine=engine)
        self.assertIn("[1] отброшено: эхо подсказки", text)

    def test_wav_repeat(self):
        engine = self.mic_engine(["раз", "два", "три"])
        with patch("core.stt.check.read_wav", return_value=make_audio(2.0)), patch_installed():
            code, text = self.run_check("", "--wav", "файл.wav", "--repeat", "3", engine=engine)
        self.assertEqual(text.count("] запись 2.0 с"), 3)

    def test_without_flags_nothing_is_recorded(self):
        engine = self.mic_engine(["раз"])
        with patch("core.stt.check.record_phrases") as record, patch_installed():
            code, _ = self.run_check("", engine=engine)
        record.assert_not_called()
        self.assertEqual(code, 0)

    def test_read_wav_reads_a_real_file(self):
        path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / "тест.wav"
        with wave.open(str(path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(b"\x00\x00" * 8000)
        self.assertAlmostEqual(check.read_wav(str(path)).seconds, 0.5, places=2)

    def test_local_engine_uses_settings(self):
        with patch("core.settings.ENV_FILE", env_file("STT_ENGINE=faster_whisper\nSTT_MODEL=base\n")):
            engine = check.local_engine(factory.stt_settings())
        self.assertEqual((engine.name, engine.model_name), ("faster_whisper", "base"))


if __name__ == "__main__":
    unittest.main()
