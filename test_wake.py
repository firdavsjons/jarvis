"""Этап 8.2: слово-активатор «Джарвис». Без сети, микрофона и модели: движки, часы и озвучка подменены."""
import inspect
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
import speech_recognition as sr

from core import installed_apps, wake
from core.ai import AIClient
from core.stt import check
from core.stt.base import Recognition, Segment
from core.stt.prompt import build_prompt
from core.stt.service import SpeechToText
from core.stt.truncated import TRUNCATED_REPLY
from core.wake import (DROPPED_LOG, LISTENING_REPLY, Decision, Phrase, WakeGate, build_gate, find_wake, is_cancel_phrase,
                       is_stop_phrase, judge, prompt_induced, wake_settings)
from fixtures_apps import patch_installed
from test_stt import FakeGoogle, StubWhisper, good, make_audio, ready_engine
from test_tools import ScriptedLLM
import main


def env_file(text):
    path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / ".env"
    path.write_text(text, encoding="utf-8")
    return path


class Hooks:
    """Связи шлюза с озвучкой и ассистентом: время задаётся в тесте."""

    def __init__(self):
        self.anchor, self.speaking, self.expects = None, False, False
        self.stops = self.closed = 0

    def stop(self):
        self.stops += 1

    def close(self):
        self.anchor = None
        self.closed += 1

    def gate(self, window=8.0, answer_timeout=20.0):
        return WakeGate(window, answer_timeout, anchor=lambda: self.anchor, speaking=lambda: self.speaking,
                        expects_answer=lambda: self.expects, stop_speech=self.stop, close_anchor=self.close)


# --- настройки ---------------------------------------------------------------------------------------------------------
class TestSettings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(wake_settings(env_file("")), (True, 8.0))

    def test_off_values(self):
        for raw in ("off", "OFF", "no", "false", "0", "none"):
            with self.subTest(raw=raw):
                self.assertFalse(wake_settings(env_file(f"WAKE_WORD={raw}\n"))[0])
        for raw in ("on", "yes", ""):
            self.assertTrue(wake_settings(env_file(f"WAKE_WORD={raw}\n"))[0])

    def test_window_is_clamped_to_3_30(self):
        self.assertEqual(wake_settings(env_file("WAKE_WINDOW=12\n"))[1], 12.0)
        self.assertEqual(wake_settings(env_file("WAKE_WINDOW=1\n"))[1], 3.0)
        self.assertEqual(wake_settings(env_file("WAKE_WINDOW=100\n"))[1], 30.0)
        self.assertEqual(wake_settings(env_file("WAKE_WINDOW=мусор\n"))[1], 8.0)

    def test_env_example(self):
        text = Path(__file__).with_name(".env.example").read_text(encoding="utf-8")
        self.assertIn("WAKE_WORD=on", text)
        self.assertIn("WAKE_WINDOW=8", text)

    def test_build_gate_follows_the_setting(self):
        engine, ai = MagicMock(), MagicMock()
        with patch("core.settings.ENV_FILE", env_file("")):
            self.assertIsInstance(build_gate(engine, ai), WakeGate)
        with patch("core.settings.ENV_FILE", env_file("WAKE_WORD=off\n")):
            self.assertIsNone(build_gate(engine, ai))
        with patch("core.settings.ENV_FILE", env_file("WAKE_WINDOW=15\n")):
            self.assertEqual(build_gate(engine, ai).window, 15.0)


# --- поиск слова -------------------------------------------------------------------------------------------------------
class TestFindWake(unittest.TestCase):
    def test_every_word_at_the_start(self):
        for word in wake.WAKE_WORDS:
            with self.subTest(word=word):
                result = find_wake(f"{word} открой калькулятор")
                self.assertEqual((result.found, result.position, result.rest), (True, "start", "открой калькулятор"))

    def test_case_yo_and_punctuation_are_ignored(self):
        for text in ("Джарвис, открой калькулятор", "ДЖАРВИС! открой калькулятор", "джарвис... открой калькулятор",
                     "Jarvis, открой калькулятор", "«Джарвис», открой калькулятор"):
            with self.subTest(text=text):
                self.assertEqual(find_wake(text).rest, "открой калькулятор")

    def test_one_mistake_is_allowed_at_the_start(self):
        for word in ("джарвиз", "жарвис", "джарвиси", "чарвес", "джарвист", "ярвис", "харвис"):
            with self.subTest(word=word):
                self.assertTrue(find_wake(f"{word} открой").found)
        for word in ("сервис", "сибирь", "спасибо", "жар", "дарья", "варвара"):
            with self.subTest(word=word):
                self.assertFalse(find_wake(f"{word} открой калькулятор").found)

    def test_known_false_positive_is_a_close_name(self):
        """Известное ограничение нечёткого поиска: «Дарвин» в начале фразы похож на «дарвис» (0,83)."""
        self.assertTrue(find_wake("Дарвин родился давно").found)
        self.assertFalse(find_wake("расскажи про Дарвина").found)             # не в начале — не считается

    def test_interjection_before_the_word(self):
        for interjection in wake.INTERJECTIONS:
            with self.subTest(interjection=interjection):
                result = find_wake(f"{interjection} джарвис открой калькулятор")
                self.assertEqual((result.found, result.rest), (True, "открой калькулятор"))
        self.assertEqual(find_wake("эй, Джарвис!").rest, "")

    def test_only_one_interjection_is_allowed(self):
        self.assertFalse(find_wake("ну эй джарвис открой калькулятор").found)

    def test_word_at_the_end(self):
        for text in ("открой калькулятор, Джарвис", "открой калькулятор Джарвис.", "Закрой сафари, джарвис!"):
            with self.subTest(text=text):
                result = find_wake(text)
                self.assertEqual((result.found, result.position), (True, "end"))
        self.assertEqual(find_wake("открой калькулятор, Джарвис").rest, "открой калькулятор")

    def test_the_end_requires_the_exact_word(self):
        self.assertFalse(find_wake("открой калькулятор джарвиз").found)

    def test_word_in_the_middle_does_not_count(self):
        for text in ("открой Джарвис калькулятор", "скажи что Джарвис умный человек", "а потом Джарвис сказал нет"):
            with self.subTest(text=text):
                result = find_wake(text)
                self.assertFalse(result.found)
                self.assertIn("в середине", result.reason)

    def test_word_at_both_ends_is_cut_on_both(self):
        self.assertEqual(find_wake("Джарвис открой калькулятор Джарвис").rest, "открой калькулятор")

    def test_just_the_word(self):
        for text in ("Джарвис", "джарвис.", "Эй, Джарвис", "Привет, Джарвис!"):
            with self.subTest(text=text):
                result = find_wake(text)
                self.assertEqual((result.found, result.rest), (True, ""))

    def test_no_word_and_empty(self):
        for text in ("Нет, но...", "Спасибо.", "Сибирь.", "открой калькулятор", "", "   ", "..."):
            with self.subTest(text=text):
                self.assertFalse(find_wake(text).found)

    def test_interjection_alone_is_not_a_wake(self):
        for text in ("привет", "ну", "слушай"):
            self.assertFalse(find_wake(text).found)


class TestStopPhrases(unittest.TestCase):
    def test_stop_and_cancel(self):
        for text in ("стоп", "Стоп!", "хватит", "стоп хватит", "Хватит."):
            self.assertTrue(is_stop_phrase(text), text)
        for text in ("стоп музыку", "не стоп", "", "отмена"):
            self.assertFalse(is_stop_phrase(text), text)
        self.assertTrue(is_cancel_phrase("Отмена."))
        self.assertFalse(is_cancel_phrase("стоп"))


# --- шлюз: решения -----------------------------------------------------------------------------------------------------
class TestGateDecisions(unittest.TestCase):
    def setUp(self):
        self.hooks = Hooks()
        self.gate = self.hooks.gate()

    def test_phrases_from_the_live_log_are_dropped(self):
        for text in ("Нет, но...", "Спасибо.", "Сибирь.", "нет но", "спасибо"):
            with self.subTest(text=text):
                with self.assertLogs("core.wake", level="INFO"):
                    self.assertEqual(self.gate.check(text).action, "drop")

    def test_drop_logs_a_fixed_line_without_text(self):
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.gate.check("Сибирь секретная")
        self.assertEqual(logs.output, [f"INFO:core.wake:{DROPPED_LOG}"])
        self.assertEqual(DROPPED_LOG, "Отброшено (нет слова «Джарвис»)")

    def test_counter_is_reported_every_ten_drops(self):
        with self.assertLogs("core.wake", level="INFO") as logs:
            for _ in range(25):
                self.gate.check("Сибирь")
        reports = [line for line in logs.output if "за запуск отброшено" in line]
        self.assertEqual(reports, ["INFO:core.wake:за запуск отброшено 10 фраз без обращения",
                                   "INFO:core.wake:за запуск отброшено 20 фраз без обращения"])
        self.assertEqual(self.gate.dropped, 25)

    def test_command_with_the_word_is_processed_without_it(self):
        for text, expected in (("Джарвис, открой калькулятор", "открой калькулятор"),
                               ("открой калькулятор, Джарвис", "открой калькулятор"),
                               ("эй Джарвис закрой сафари", "закрой сафари"),
                               ("жарвис поставь таймер на пять минут", "поставь таймер на пять минут")):
            with self.subTest(text=text):
                self.assertEqual(self.gate.check(text), Decision("process", expected))

    def test_only_the_word_means_listening(self):
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.assertEqual(self.gate.check("Джарвис").action, "listen")
        self.assertEqual(logs.output, ["INFO:core.wake:Обращение «Джарвис» без команды"])
        self.assertEqual(self.gate.check("Эй, Джарвис!").action, "listen")

    def test_word_in_the_middle_is_dropped(self):
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("открой Джарвис калькулятор").action, "drop")

    def test_stop_words_need_no_word_and_stop_speech(self):
        self.hooks.anchor = 100.0
        for text in ("стоп", "хватит", "Стоп!"):
            with self.subTest(text=text):
                with self.assertLogs("core.wake", level="INFO"):
                    self.assertEqual(self.gate.check(text), Decision("stop"))
        self.assertEqual(self.hooks.stops, 3)
        self.assertIsNone(self.hooks.anchor)                         # окно закрыто

    def test_stop_with_the_word_also_stops(self):
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис, стоп"), Decision("stop"))
            self.assertEqual(self.gate.check("хватит, Джарвис"), Decision("stop"))
        self.assertEqual(self.hooks.stops, 2)

    def test_cancel_needs_no_word_closes_the_window_and_goes_on(self):
        self.hooks.anchor = 100.0
        self.assertEqual(self.gate.check("отмена", Phrase(started_at=101.0)), Decision("process", "отмена"))
        self.assertEqual((self.hooks.stops, self.hooks.anchor), (1, None))
        self.hooks.anchor = 100.0
        self.assertEqual(self.gate.check("Джарвис, отмена"), Decision("process", "отмена"))
        self.assertEqual(self.hooks.stops, 2)

    def test_stop_does_not_turn_into_a_command_for_the_model(self):
        self.assertEqual(self.gate.check("стоп").action, "stop")


# --- окно диалога ------------------------------------------------------------------------------------------------------
class TestWindow(unittest.TestCase):
    def setUp(self):
        self.hooks = Hooks()
        self.gate = self.hooks.gate(window=8.0)

    def check(self, text, started):
        return self.gate.check(text, Phrase(started_at=started))

    def test_closed_until_the_first_answer(self):
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.check("громче", 5.0).action, "drop")

    def test_open_after_an_answer_for_the_window(self):
        self.hooks.anchor = 100.0
        for text in ("громче", "да", "нет", "который час", "открой калькулятор"):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, 103.0), Decision("process", text))
        self.assertEqual(self.check("громче", 108.0).action, "process")           # ровно 8 с — ещё открыто

    def test_closed_after_the_timeout(self):
        self.hooks.anchor = 100.0
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.check("громче", 108.5).action, "drop")
            self.assertEqual(self.check("громче", 150.0).action, "drop")

    def test_the_window_counts_from_the_start_of_the_phrase_not_from_recognition(self):
        self.hooks.anchor = 100.0
        self.assertEqual(self.check("громче", 107.0).action, "process")          # распознано позже, но начата в окне

    def test_window_follows_the_setting(self):
        gate = self.hooks.gate(window=15.0)
        self.hooks.anchor = 100.0
        self.assertEqual(gate.check("громче", Phrase(started_at=114.0)).action, "process")
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(gate.check("громче", Phrase(started_at=116.0)).action, "drop")

    def test_a_question_keeps_the_window_open_for_twenty_seconds(self):
        self.hooks.anchor, self.hooks.expects = 100.0, True                      # «Кому именно?»
        self.assertEqual(self.check("маме", 119.0), Decision("process", "маме"))
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.check("маме", 121.0).action, "drop")

    def test_without_a_question_the_long_wait_does_not_apply(self):
        self.hooks.anchor, self.hooks.expects = 100.0, False
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.check("маме", 119.0).action, "drop")

    def test_open_while_jarvis_is_speaking(self):
        self.hooks.speaking = True
        self.assertEqual(self.check("громче", 5.0).action, "process")

    def test_stop_closes_the_window(self):
        self.hooks.anchor = 100.0
        self.assertEqual(self.check("громче", 101.0).action, "process")
        with self.assertLogs("core.wake", level="INFO"):
            self.check("стоп", 102.0)
            self.assertEqual(self.check("громче", 103.0).action, "drop")

    def test_cancel_closes_the_window(self):
        self.hooks.anchor = 100.0
        self.check("отмена", 101.0)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.check("громче", 102.0).action, "drop")

    def test_a_new_answer_reopens_the_window(self):
        self.hooks.anchor = 100.0
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.check("громче", 120.0).action, "drop")
        self.hooks.anchor = 121.0                                                # ответ Jarvis
        self.assertEqual(self.check("громче", 123.0).action, "process")

    def test_the_word_inside_the_window_is_still_cut(self):
        self.hooks.anchor = 100.0
        self.assertEqual(self.check("Джарвис, громче", 101.0), Decision("process", "громче"))


# --- слово, пришедшее из подсказки ------------------------------------------------------------------------------------
class TestWordFromPrompt(unittest.TestCase):
    def setUp(self):
        self.hooks = Hooks()
        self.gate = self.hooks.gate()

    def test_thresholds_are_named_constants(self):
        self.assertEqual((wake.PROMPT_WAKE_MAX_RECORDING, wake.PROMPT_WAKE_MIN_CONFIDENCE), (1.2, 0.5))
        self.assertEqual(wake.PROMPT_WAKE_MIN_REST_WORDS, 2)
        source = Path(wake.__file__).read_text(encoding="utf-8")
        self.assertIn("Оценочные, подобрать по логу", source)

    def test_prompt_induced(self):
        self.assertTrue(prompt_induced("Джарвис.", 0.6, 0.4))
        self.assertTrue(prompt_induced("Джарвис, открой калькулятор", 1.1, 0.9))     # запись короче 1,2 с
        self.assertTrue(prompt_induced("Джарвис, открой калькулятор", 3.0, 0.4))     # Whisper не уверен
        self.assertFalse(prompt_induced("Джарвис, открой калькулятор", 3.0, 0.9))
        self.assertFalse(prompt_induced("Джарвис", 1.2, 0.5))                        # ровно на порогах — не из подсказки
        self.assertFalse(prompt_induced("открой калькулятор, Джарвис", 0.6, 0.4))    # слово в конце подсказка не диктует
        self.assertFalse(prompt_induced("Сибирь", 0.6, 0.4))
        self.assertFalse(prompt_induced("Джарвис", None, None))

    def test_bare_word_on_a_short_unsure_recording_does_not_open_anything(self):
        phrase = Phrase(audio_seconds=0.6, confidence=0.4, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.assertEqual(self.gate.check("Джарвис.", phrase).action, "drop")
        self.assertTrue(any("не засчитано" in line and "возможно из подсказки" in line for line in logs.output))

    def test_word_with_a_long_confident_command_passes(self):
        phrase = Phrase(audio_seconds=3.0, confidence=0.9, from_prompt=False)
        self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase), Decision("process", "открой калькулятор"))

    def test_prompt_word_needs_two_words_and_confidence(self):
        counted = judge("Джарвис, открой калькулятор", from_prompt=True, confidence=0.9)
        self.assertTrue(counted.counted)
        self.assertIn("возможно из подсказки", counted.reason)
        for text, confidence, why in (("Джарвис, громче", 0.9, "остаток короче 2 слов"),
                                      ("Джарвис, открой калькулятор", 0.4, "уверенность 0.40"),
                                      ("Джарвис", 0.9, "остаток короче 2 слов")):
            with self.subTest(text=text, confidence=confidence):
                result = judge(text, from_prompt=True, confidence=confidence)
                self.assertFalse(result.counted)
                self.assertIn(why, result.reason)

    def test_unsure_prompt_word_with_a_command_is_dropped_when_the_window_is_closed(self):
        phrase = Phrase(audio_seconds=3.0, confidence=0.4, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase).action, "drop")

    def test_unsure_prompt_word_inside_the_window_is_cut_and_the_rest_is_processed(self):
        self.hooks.anchor = 100.0
        phrase = Phrase(started_at=102.0, audio_seconds=3.0, confidence=0.4, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase), Decision("process",
                                                                                           "открой калькулятор"))

    def test_unsure_bare_prompt_word_inside_the_window_is_dropped_not_sent_to_the_model(self):
        self.hooks.anchor = 100.0
        phrase = Phrase(started_at=101.0, audio_seconds=0.6, confidence=0.4, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис.", phrase).action, "drop")

    def test_bare_word_on_a_good_recording_opens_the_dialog(self):
        phrase = Phrase(audio_seconds=1.8, confidence=0.9, from_prompt=False)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис.", phrase).action, "listen")


class TestServiceMarksTheWord(unittest.TestCase):
    PROMPT = "Закрой сафари. Поставь таймер на пять минут."     # без «Джарвис, открой …»: иначе это эхо подсказки

    def recognize(self, text, seconds, confidence, prompt=PROMPT):
        engine = ready_engine(good(text, avg_logprob=math.log(confidence)))
        provider = MagicMock()
        provider.get.return_value = prompt
        stt = SpeechToText(engine, None, provider if prompt else None, notify=lambda t: None)
        return stt.recognize(make_audio(seconds))

    def test_the_issue_examples(self):
        short = self.recognize("Джарвис.", 0.6, 0.4)
        self.assertIsNone(short) if short is None else self.assertTrue(short.wake_from_prompt)
        long = self.recognize("Джарвис, открой калькулятор", 3.0, 0.9)
        self.assertFalse(long.wake_from_prompt)

    def test_short_recording_is_marked(self):
        result = self.recognize("Джарвис, открой калькулятор", 1.0, 0.9)
        self.assertTrue(result.wake_from_prompt)

    def test_unsure_recognition_is_marked(self):
        self.assertTrue(self.recognize("Джарвис, открой калькулятор", 3.0, 0.4).wake_from_prompt)

    def test_without_a_prompt_nothing_is_marked(self):
        provider_less = SpeechToText(ready_engine(good("Джарвис, открой калькулятор", avg_logprob=math.log(0.4))), None,
                                     None, notify=lambda t: None)
        self.assertFalse(provider_less.recognize(make_audio(3.0)).wake_from_prompt)

    def test_text_without_the_word_is_not_marked(self):
        self.assertFalse(self.recognize("открой калькулятор", 3.0, 0.4).wake_from_prompt)

    def test_google_fallback_result_is_never_marked(self):
        engine = ready_engine()
        engine.transcribe_error = RuntimeError("сбой")
        stt = SpeechToText(engine, FakeGoogle("Джарвис"), MagicMock(get=MagicMock(return_value=self.PROMPT)),
                           notify=lambda t: None)
        with self.assertLogs("core.stt.service", level="WARNING"):
            result = stt.recognize(make_audio(0.6))
        self.assertFalse(result.wake_from_prompt)

    def test_the_prompt_always_starts_with_the_name(self):
        with patch_installed():
            for rotation in range(6):
                self.assertTrue(build_prompt(rotation).startswith("Джарвис"), rotation)
        with patch("core.installed_apps.list_installed_apps", return_value=[]):
            self.assertTrue(build_prompt().startswith("Джарвис"))


# --- process_command ----------------------------------------------------------------------------------------------------
class AppCase(unittest.TestCase):
    def setUp(self):
        self.hooks = Hooks()

    def app(self, gate="default", pending=False):
        app = MagicMock()
        app.wake = self.hooks.gate() if gate == "default" else gate
        app.has_pending.return_value = pending
        app.pending_is_private.return_value = False
        app.ai.message_dialog_open.return_value = False
        app.ai.bare_confirmation_reply.return_value = None
        app.ai.quick_confirm.return_value = None
        app.mac.execute.return_value = None
        return app

    def run_voice(self, app, text, phrase=None):
        main.VoiceApp.process_command(app, text, phrase=phrase)


class TestProcessCommand(AppCase):
    def test_dropped_phrase_touches_nothing(self):
        for text in ("Нет, но...", "Спасибо.", "Сибирь.", "открой калькулятор"):
            with self.subTest(text=text):
                app = self.app()
                with self.assertLogs("core.wake", level="INFO"):
                    self.run_voice(app, text)
                self.assertEqual(app.ai.mock_calls, [])                     # ни модели, ни истории, ни кэша, ни правил
                self.assertEqual(app.mac.mock_calls, [])
                self.assertEqual(app.engine.mock_calls, [])                 # молча: ни ответа, ни «Слушаю.»
                self.assertEqual(app.deliver.mock_calls, [])
                self.assertEqual(app.handle_confirmation.mock_calls, [])

    def test_command_with_the_word_goes_to_the_model_without_it(self):
        app = self.app()
        self.run_voice(app, "Джарвис, открой калькулятор")
        app.ai.respond.assert_called_once_with("открой калькулятор")

    def test_word_at_the_end(self):
        app = self.app()
        self.run_voice(app, "открой калькулятор, Джарвис")
        app.ai.respond.assert_called_once_with("открой калькулятор")

    def test_only_the_word_says_listening_without_the_model_and_opens_the_window(self):
        app = self.app()
        with self.assertLogs("core.wake", level="INFO"):
            self.run_voice(app, "Джарвис")
        app.engine.speak.assert_called_once_with("Слушаю.", opens_window=True)
        self.assertEqual(LISTENING_REPLY, "Слушаю.")
        app.ai.respond.assert_not_called()
        app.ai.quick_confirm.assert_not_called()
        app.mac.execute.assert_not_called()

    def test_stop_without_the_word_is_silent(self):
        app = self.app()
        self.hooks.anchor = 100.0
        with self.assertLogs("core.wake", level="INFO"):
            self.run_voice(app, "стоп")
        app.engine.speak.assert_not_called()
        app.ai.respond.assert_not_called()
        self.assertEqual(self.hooks.stops, 1)

    def test_cancel_without_the_word_goes_on_to_the_normal_path(self):
        app = self.app()
        app.ai.bare_confirmation_reply.return_value = "Сейчас нечего подтверждать."
        self.run_voice(app, "отмена")
        app.engine.speak.assert_called_once_with("Сейчас нечего подтверждать.")
        self.assertEqual(self.hooks.stops, 1)

    def test_pending_confirmation_needs_no_word(self):
        for text in ("да", "нет", "да, отправляй", "что-то непонятное"):
            with self.subTest(text=text):
                app = self.app(pending=True)
                self.run_voice(app, text)
                app.handle_confirmation.assert_called_once_with(text)
                app.ai.respond.assert_not_called()

    def test_stop_and_hold_during_pending_mean_cancel(self):
        for text in ("стоп", "хватит", "Стоп!"):
            with self.subTest(text=text):
                app = self.app(pending=True)
                self.run_voice(app, text)
                app.handle_confirmation.assert_called_once_with("отмена")

    def test_text_input_needs_no_word(self):
        app = self.app()
        main.VoiceApp.process_command(app, "открой калькулятор", source="text")
        app.ai.respond.assert_called_once_with("открой калькулятор")
        self.assertEqual(self.hooks.stops, 0)

    def test_stop_means_cancel_in_text_input_too_while_waiting_for_confirmation(self):
        app = self.app(pending=True)
        main.VoiceApp.process_command(app, "стоп", source="text")
        app.handle_confirmation.assert_called_once_with("отмена")

    def test_wake_off_means_everything_as_before(self):
        app = self.app(gate=None)
        self.run_voice(app, "Сибирь.")
        app.ai.respond.assert_called_once_with("Сибирь.")
        app = self.app(gate=None)
        self.run_voice(app, "Джарвис, открой калькулятор")
        app.ai.respond.assert_called_once_with("Джарвис, открой калькулятор")
        app = self.app(gate=None, pending=True)
        self.run_voice(app, "стоп")
        app.handle_confirmation.assert_called_once_with("стоп")                  # прежнее поведение

    def test_window_lets_a_command_without_the_word_through(self):
        app = self.app()
        self.hooks.anchor = 100.0
        self.run_voice(app, "громче", Phrase(started_at=103.0))
        app.ai.respond.assert_called_once_with("громче")

    def test_window_closed_by_the_timeout_drops_the_same_command(self):
        app = self.app()
        self.hooks.anchor = 100.0
        with self.assertLogs("core.wake", level="INFO"):
            self.run_voice(app, "громче", Phrase(started_at=109.0))
        app.ai.respond.assert_not_called()


class TestOrderOfChecks(AppCase):
    def test_confirmation_goes_before_the_word_and_before_truncation(self):
        app = self.app(pending=True)
        self.run_voice(app, "да отправляй на")
        app.handle_confirmation.assert_called_once_with("да отправляй на")
        app.engine.speak.assert_not_called()

    def test_stop_goes_before_the_word(self):
        app = self.app()
        with self.assertLogs("core.wake", level="INFO"):
            self.run_voice(app, "стоп")
        self.assertEqual(self.hooks.stops, 1)

    def test_word_goes_before_truncation_a_dropped_cut_phrase_costs_nothing(self):
        app = self.app()
        with self.assertLogs("core.wake", level="INFO"):
            self.run_voice(app, "позвонить ма")
        app.engine.speak.assert_not_called()                                    # не «Фраза обрезана»

    def test_truncation_is_checked_after_the_word_is_cut(self):
        app = self.app()
        self.run_voice(app, "Джарвис, позвонить ма")
        app.engine.speak.assert_called_once_with(TRUNCATED_REPLY)
        app.ai.respond.assert_not_called()

    def test_truncation_in_the_window(self):
        app = self.app()
        self.hooks.anchor = 100.0
        self.run_voice(app, "напиши себе в", Phrase(started_at=101.0))
        app.engine.speak.assert_called_once_with(TRUNCATED_REPLY)

    def test_calls_come_in_the_documented_order(self):
        order = []
        app = self.app(pending=True)
        app.handle_confirmation.side_effect = lambda t: order.append("подтверждение")
        self.run_voice(app, "стоп")
        app2 = self.app()
        gate = app2.wake
        original = gate.check
        gate.check = lambda *a, **k: (order.append("слово"), original(*a, **k))[1]
        app2.engine.speak.side_effect = lambda *a, **k: order.append("озвучка")
        self.run_voice(app2, "Джарвис, позвонить ма")
        self.assertEqual(order, ["подтверждение", "слово", "озвучка"])


class TestPrivacy(AppCase):
    SECRET = "секретный разговор про зарплату"

    def test_dropped_text_is_nowhere(self):
        app = self.app()
        with self.assertLogs(level="DEBUG") as logs:
            self.run_voice(app, self.SECRET)
        joined = "\n".join(logs.output)
        self.assertNotIn("зарплат", joined)
        self.assertNotIn("секретн", joined)
        self.assertEqual(app.ai.mock_calls, [])                                 # модель, история, кэш
        self.assertEqual(app.update_log.mock_calls, [])                         # и окно

    def test_dropped_text_does_not_reach_the_action_journal(self):
        app = self.app()
        with patch("core.journal.record") as record, self.assertLogs("core.wake", level="INFO"):
            self.run_voice(app, self.SECRET)
        record.assert_not_called()

    def test_dropped_text_does_not_reach_history_or_cache_of_a_real_client(self):
        from core.cache import CommandCache
        cache = CommandCache()
        client = AIClient(llm=ScriptedLLM(), cache=cache)
        app = self.app()
        app.ai = client
        with self.assertLogs("core.wake", level="INFO"):
            self.run_voice(app, self.SECRET)
        self.assertEqual(client.history.messages(), [])
        self.assertEqual(client.llm.requests, [])
        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.lookup(self.SECRET))

    def test_accepted_text_is_logged_only_after_the_check(self):
        app = self.app()
        with self.assertLogs("main", level="INFO") as logs:
            self.run_voice(app, "Джарвис, открой калькулятор")
        self.assertTrue(any("Обработка (voice): открой калькулятор" in line for line in logs.output))
        self.assertFalse(any("джарвис" in line.lower() for line in logs.output))


# --- озвучка и окно ----------------------------------------------------------------------------------------------------
@patch("core.engine._detect_voice", return_value="Milena")
@patch("core.engine.sr.Microphone")
@patch("core.engine.subprocess.Popen")
class TestEngineWindow(unittest.TestCase):
    def test_an_answer_opens_the_window(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        self.assertIsNone(engine.window_opened_at)
        engine.speak("Готово.")
        self.assertIsNotNone(engine.window_opened_at)
        self.assertAlmostEqual(engine.window_opened_at, engine._last_speech_end, places=6)

    def test_a_notice_does_not_open_it(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        engine.speak("Секунду.", opens_window=False)
        self.assertIsNone(engine.window_opened_at)
        self.assertGreater(engine._last_speech_end, 0)

    def test_a_notice_does_not_extend_an_open_window(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        engine.speak("Готово.")
        opened = engine.window_opened_at
        engine.speak("Таймер сработал.", opens_window=False)
        self.assertEqual(engine.window_opened_at, opened)

    def test_interrupted_speech_does_not_open_it(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        popen.return_value.wait.side_effect = lambda: engine.stop_speaking()
        engine.speak("Длинный ответ.")
        self.assertIsNone(engine.window_opened_at)

    def test_close_window(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        engine.speak("Готово.")
        engine.close_window()
        self.assertIsNone(engine.window_opened_at)

    def test_recognition_notices_are_quiet(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        engine.notices.append("Локальное распознавание не установлено, использую Google.")
        engine.flush_notices()
        self.assertIsNone(engine.window_opened_at)
        self.assertEqual(popen.call_count, 1)

    def test_listen_records_phrase_info_and_not_the_text(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        engine.recognizer = MagicMock()
        engine.recognizer.listen.return_value = sr.AudioData(b"\x01\x00" * 48000, 16000, 2)   # 3 с
        engine.stt = MagicMock()
        engine.stt.recognize.return_value = Recognition("Джарвис, открой", "stub", audio_seconds=3.0, confidence=0.9,
                                                        wake_from_prompt=True)
        with patch("core.engine.time.monotonic", return_value=500.0):
            self.assertEqual(engine.listen(), "джарвис, открой")
        phrase = engine.last_phrase
        self.assertEqual((phrase.audio_seconds, phrase.confidence, phrase.from_prompt), (3.0, 0.9, True))
        self.assertAlmostEqual(phrase.started_at, 497.0, places=3)             # конец записи минус её длина

    def test_real_gate_and_engine_together(self, popen, *_):
        from core.engine import SpeechEngine
        engine = SpeechEngine()
        ai = AIClient(llm=ScriptedLLM())
        with patch("core.settings.ENV_FILE", env_file("WAKE_WINDOW=8\n")):
            gate = build_gate(engine, ai)
        engine.speak("Готово.")
        opened = engine.window_opened_at
        self.assertEqual(gate.check("громче", Phrase(started_at=opened + 3)).action, "process")
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(gate.check("громче", Phrase(started_at=opened + 9)).action, "drop")
        ai._last_question = True                                               # Jarvis задал вопрос
        self.assertEqual(gate.check("маме", Phrase(started_at=opened + 19)).action, "process")
        engine.speak("Кому?")
        gate.check("стоп", Phrase())
        self.assertIsNone(engine.window_opened_at)


class TestWiring(unittest.TestCase):
    def test_quiet_speak(self):
        app = MagicMock()
        main.VoiceApp.quiet_speak(app, "Секунду.")
        app.engine.speak.assert_called_once_with("Секунду.", opens_window=False)

    def test_notifications_use_quiet_speak(self):
        source = inspect.getsource(main.VoiceApp.__init__)
        for expected in ("self.ai.on_slow_tool = self.quiet_speak", "make_fire_handler(self.quiet_speak)",
                         "make_missed_handler(self.quiet_speak)", "build_gate(self.engine, self.ai, CONFIRM_TIMEOUT)"):
            self.assertIn(expected, source)

    def test_loop_passes_the_phrase_info(self):
        self.assertIn("phrase=self.engine.last_phrase", inspect.getsource(main.VoiceApp.background_loop))

    def test_overlap_log_has_no_text(self):
        source = inspect.getsource(main.VoiceApp.background_loop)
        self.assertNotIn("loggable_phrase", source)

    def test_expects_answer(self):
        client = AIClient(llm=ScriptedLLM())
        self.assertFalse(client.expects_answer())
        client._last_question = True
        self.assertTrue(client.expects_answer())
        client._last_question = False
        client._dialog = {"phrases": ["напиши привет"]}
        self.assertTrue(client.expects_answer())

    def test_timer_and_notice_texts_do_not_open_the_window_in_the_scheduler(self):
        from core import scheduler
        spoken = []
        scheduler.make_missed_handler(lambda text: spoken.append(text))([{"kind": "timer", "label": "", "id": 1,
                                                                          "due": 0, "created": 0}])
        self.assertEqual(len(spoken), 1)


# --- core.stt.check --wake --------------------------------------------------------------------------------------------
class TestCheckWake(unittest.TestCase):
    def run_check(self, texts, seconds, confidences, *args):
        engine = StubWhisper([good(texts[0], avg_logprob=math.log(confidences[0]))])
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        queue = list(zip(texts, confidences))
        original = engine._transcribe

        def transcribe(model, pcm, prompt):
            if engine.calls:
                text, confidence = queue.pop(0)
                engine.segments = [good(text, avg_logprob=math.log(confidence))]
            return original(model, pcm, prompt)
        engine._transcribe = transcribe
        lines = []
        audios = iter([make_audio(s) for s in seconds])
        with patch("core.settings.ENV_FILE", env_file("")), patch_installed(), \
                patch("core.stt.check.local_engine", return_value=engine), \
                patch("core.stt.check.record_phrases", side_effect=lambda n: (next(audios) for _ in range(n))):
            code = check.main(["--mic", "--wake", "--repeat", str(len(texts)), *args], out=lines.append)
        return code, "\n".join(lines)

    def test_reports_whether_the_word_counted_and_why(self):
        code, text = self.run_check(["Джарвис.", "Джарвис, открой калькулятор", "открой калькулятор, Джарвис",
                                     "Сибирь.", "открой Джарвис калькулятор"],
                                    [0.6, 3.0, 2.5, 1.5, 2.5], [0.4, 0.9, 0.9, 0.9, 0.9])
        self.assertEqual(code, 0)
        self.assertIn("[1] слово «Джарвис»: не засчитано — слово возможно из подсказки Whisper (остаток короче 2 слов, "
                      "уверенность 0.40 < 0.50)", text)
        self.assertIn("[2] слово «Джарвис»: ЗАСЧИТАНО — слово в начале; команда: «открой калькулятор»", text)
        self.assertIn("[3] слово «Джарвис»: ЗАСЧИТАНО — слово в конце; команда: «открой калькулятор»", text)
        self.assertIn("[4] слово «Джарвис»: не засчитано — слова нет", text)
        self.assertIn("[5] слово «Джарвис»: не засчитано — слово в середине фразы", text)

    def test_bare_word_on_a_good_recording(self):
        _, text = self.run_check(["Джарвис."], [1.8], [0.9])
        self.assertIn("ЗАСЧИТАНО — слово в начале; команды нет, ответ «Слушаю.»", text)

    def test_without_the_flag_nothing_is_said_about_the_word(self):
        engine = StubWhisper([good("Джарвис, открой калькулятор")])
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        lines = []
        with patch("core.settings.ENV_FILE", env_file("")), patch_installed(), \
                patch("core.stt.check.local_engine", return_value=engine), \
                patch("core.stt.check.record_phrases", side_effect=lambda n: (make_audio(3.0) for _ in range(n))):
            check.main(["--mic", "--repeat", "1"], out=lines.append)
        self.assertNotIn("слово «Джарвис»", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
