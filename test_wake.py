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
from core.wake import (DROPPED_LOG, LISTENING_REPLY, NOT_WAKE_WORDS, WAKE_WORDS, Decision, Phrase, WakeGate, build_gate,
                       extra_wake_words, find_wake, is_cancel_phrase, is_stop_phrase, judge, match_word, prompt_induced,
                       wake_settings)
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
        for word in ("джарвиз", "джарвиси", "чарвес", "джарвист", "харвис", "шарвис", "марвис", "чаривис", "джервиз"):
            with self.subTest(word=word):
                self.assertTrue(find_wake(f"{word} открой").found)
        for word in ("сибирь", "спасибо", "дарья", "варвара", "верность", "джаз", "арбуз"):
            with self.subTest(word=word):
                self.assertFalse(find_wake(f"{word} открой калькулятор").found)

    def test_every_listed_word_is_counted_alone_with_interjection_and_at_the_end(self):
        self.assertEqual(len(WAKE_WORDS), 16)
        for word in ("червис", "чарвис", "джервис", "джервез", "жарвис", "жарвес", "джарвес", "джарвус", "джарис",
                     "джарус", "дарвис", "ярвис", "jarvis", "джарвис", "жарусь", "жарус"):
            with self.subTest(word=word):
                self.assertIn(word, WAKE_WORDS)
                start = find_wake(f"{word.capitalize()}, открой калькулятор")
                self.assertEqual((start.found, start.position, start.rest), (True, "start", "открой калькулятор"))
                after = find_wake(f"эй {word} открой калькулятор")
                self.assertEqual((after.found, after.rest), (True, "открой калькулятор"))
                end = find_wake(f"открой калькулятор, {word}.")
                self.assertEqual((end.found, end.position, end.rest), (True, "end", "открой калькулятор"))
                self.assertEqual(find_wake(word).rest, "")

    def test_live_distortions_with_punctuation(self):
        for text in ("Червис.", "Джервис.", "Жарусь?", "Джарис?"):
            with self.subTest(text=text):
                result = find_wake(text)
                self.assertEqual((result.found, result.rest), (True, ""))

    def test_real_words_are_not_counted(self):
        for text in ("Дарвин родился давно", "Жаров пришёл", "Что это?", "Жара сегодня", "Червь на крючке", "Сервис недоступен",
                     "Парус на горизонте", "что это", "Жаров", "Червь", "Сервис", "Парус"):
            with self.subTest(text=text):
                self.assertFalse(find_wake(text).found)

    def test_not_wake_words_only_block_the_fuzzy_match(self):
        self.assertEqual(NOT_WAKE_WORDS, ("дарвин", "сервис", "парус", "червь", "жаров", "жара"))
        for word in NOT_WAKE_WORDS:
            self.assertIsNone(match_word(word, True))
            self.assertNotIn(word, WAKE_WORDS)
        self.assertIsNotNone(match_word("чернис", True))                       # настоящее искажение (0,83), не в исключениях
        self.assertIsNotNone(match_word("чаривис", True))
        self.assertNotIn("чернис", NOT_WAKE_WORDS)
        self.assertNotIn("чаривис", NOT_WAKE_WORDS)

    def test_close_real_words_in_the_middle_or_end_are_not_counted(self):
        self.assertFalse(find_wake("расскажи про Дарвина").found)
        self.assertFalse(find_wake("открой Сервис").found)

    def test_match_details(self):
        listed = find_wake("Червис, открой калькулятор")
        self.assertEqual((listed.heard, listed.via, listed.distorted), ("червис", "list", True))
        self.assertRegex(listed.describe(), r"^червис ≈ джарвис, \d\.\d\d, из списка$")
        canonical = find_wake("Джарвис, открой")
        self.assertEqual((canonical.via, canonical.ratio, canonical.distorted), ("list", 1.0, False))
        self.assertEqual(canonical.describe(), "джарвис ≈ джарвис, 1.00, из списка")
        fuzzy = find_wake("шарвис открой калькулятор")
        self.assertEqual((fuzzy.via, fuzzy.nearest, fuzzy.distorted), ("fuzzy", "чарвис", True))
        self.assertEqual(fuzzy.describe(), "шарвис ≈ чарвис, 0.83, по искажению")
        self.assertEqual(find_wake("Нет, но").describe(), "")

    def test_custom_words_are_accepted_like_list_words(self):
        words = (*WAKE_WORDS, "ассистент")
        self.assertTrue(find_wake("Ассистент, открой калькулятор", words).found)
        self.assertTrue(find_wake("открой калькулятор, ассистент", words).found)
        self.assertFalse(find_wake("Ассистент, открой калькулятор").found)       # без своих слов — нет
        custom = find_wake("ассистент открой", words)
        self.assertEqual((custom.via, custom.ratio, custom.describe()), ("extra", 1.0, "ассистент ≈ ассистент, 1.00, из WAKE_WORDS"))

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
        self.assertEqual(wake.PROMPT_WAKE_MAX_RECORDING, 1.2)
        self.assertEqual(wake.WAKE_PROMPT_MIN_CONFIDENCE, 0.3)
        self.assertEqual(wake.WAKE_PROMPT_MIN_CONFIDENCE_ONE_WORD, 0.4)
        self.assertEqual(wake.WAKE_LISTEN_WINDOW_MIN_CONFIDENCE, 0.4)
        source = Path(wake.__file__).read_text(encoding="utf-8")
        self.assertIn("оценочный, по живым логам", source)
        self.assertIn("Оценочные, подобрать по логу", source)

    def test_prompt_induced(self):
        self.assertTrue(prompt_induced("Джарвис.", 0.6, 0.25))
        self.assertTrue(prompt_induced("Джарвис, открой калькулятор", 1.1, 0.9))     # запись короче 1,2 с
        self.assertTrue(prompt_induced("Джарвис, открой калькулятор", 3.0, 0.29))    # Whisper не уверен
        self.assertFalse(prompt_induced("Джарвис, открой калькулятор", 3.0, 0.9))
        for confidence in (0.41, 0.44, 0.46, 0.47):                                  # живой лог: больше не подозрительно
            self.assertFalse(prompt_induced("Джарвис, открой калькулятор", 2.0, confidence), confidence)
        self.assertFalse(prompt_induced("Джарвис", 1.2, 0.3))                        # ровно на порогах — не из подсказки
        self.assertFalse(prompt_induced("открой калькулятор, Джарвис", 0.6, 0.25))   # слово в конце подсказка не диктует
        self.assertFalse(prompt_induced("Сибирь", 0.6, 0.25))
        self.assertFalse(prompt_induced("Джарвис", None, None))

    def test_one_word_rest_with_confidence_041_is_counted(self):
        for audio in (0.9, 2.0):                                                     # и на короткой записи, и на нормальной
            with self.subTest(audio=audio):
                phrase = Phrase(audio_seconds=audio, confidence=0.41, from_prompt=audio < 1.2)
                self.assertEqual(self.gate.check("Джарвис, громче", phrase), Decision("process", "громче"))

    def test_bare_word_on_a_short_recording_with_confidence_025_is_not_counted(self):
        phrase = Phrase(audio_seconds=0.6, confidence=0.25, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.assertEqual(self.gate.check("Джарвис.", phrase).action, "drop")
        self.assertTrue(any("не засчитано" in line and "остаток пуст" in line and "уверенность 0.25 < порога 0.40" not in line
                            for line in logs.output))

    def test_bare_word_marked_from_prompt_is_never_counted(self):
        for confidence in (0.9, 0.5, 0.25):
            phrase = Phrase(audio_seconds=0.6, confidence=confidence, from_prompt=True)
            with self.subTest(confidence=confidence), self.assertLogs("core.wake", level="INFO"):
                self.assertEqual(self.gate.check("Джарвис.", phrase).action, "drop")

    def test_command_with_044_confidence_is_counted(self):
        phrase = Phrase(audio_seconds=3.0, confidence=0.44, from_prompt=False)
        self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase), Decision("process", "открой калькулятор"))
        phrase = Phrase(audio_seconds=1.0, confidence=0.44, from_prompt=True)         # даже на короткой записи
        self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase), Decision("process", "открой калькулятор"))

    def test_word_with_a_long_confident_command_passes(self):
        phrase = Phrase(audio_seconds=3.0, confidence=0.9, from_prompt=False)
        self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase), Decision("process", "открой калькулятор"))

    def test_confidence_boundaries(self):
        two = "Джарвис, открой калькулятор"
        one = "Джарвис, громче"
        self.assertTrue(judge(two, True, 0.30).counted)
        self.assertFalse(judge(two, True, 0.29).counted)
        self.assertTrue(judge(one, True, 0.40).counted)
        self.assertFalse(judge(one, True, 0.39).counted)
        self.assertTrue(judge(one, True, None).counted)                              # уверенности нет — не отказываем
        self.assertFalse(judge("Джарвис", True, 0.99).counted)

    def test_refusal_reasons_carry_the_numbers(self):
        two = judge("Джарвис, открой калькулятор", True, 0.25)
        self.assertIn("уверенность 0.25 < порога 0.30", two.reason)
        one = judge("Джарвис, громче", True, 0.35)
        self.assertIn("уверенность 0.35 < порога 0.40 для остатка из 1 слова", one.reason)
        self.assertIn("остаток пуст", judge("Джарвис.", True, 0.25).reason)

    def test_refusal_is_logged_with_numbers_without_text(self):
        phrase = Phrase(audio_seconds=0.9, confidence=0.25, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.assertEqual(self.gate.check("Джарвис, секретный разговор", phrase).action, "drop")
        joined = "\n".join(logs.output)
        self.assertIn("уверенность 0.25 < порога 0.30", joined)
        self.assertNotIn("секретн", joined)
        phrase = Phrase(audio_seconds=0.9, confidence=0.35, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.assertEqual(self.gate.check("Джарвис, секретно", phrase).action, "drop")
        joined = "\n".join(logs.output)
        self.assertIn("уверенность 0.35 < порога 0.40 для остатка из 1 слова", joined)
        self.assertNotIn("секретн", joined)

    def test_unsure_prompt_word_with_a_command_is_dropped_when_the_window_is_closed(self):
        phrase = Phrase(audio_seconds=3.0, confidence=0.25, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase).action, "drop")

    def test_unsure_prompt_word_inside_the_window_is_cut_and_the_rest_is_processed(self):
        self.hooks.anchor = 100.0
        phrase = Phrase(started_at=102.0, audio_seconds=3.0, confidence=0.25, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис, открой калькулятор", phrase), Decision("process",
                                                                                           "открой калькулятор"))

    def test_unsure_bare_prompt_word_inside_the_window_is_dropped_not_sent_to_the_model(self):
        self.hooks.anchor = 100.0
        phrase = Phrase(started_at=101.0, audio_seconds=0.6, confidence=0.25, from_prompt=True)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(self.gate.check("Джарвис.", phrase).action, "drop")


class TestListeningWindow(unittest.TestCase):
    """«Джарвис» одним словом на нормальной записи: «Слушаю.»; окно открывается только при уверенности ≥ 0,4."""

    def setUp(self):
        self.hooks = Hooks()
        self.gate = self.hooks.gate()

    def listen(self, confidence, audio=1.8):
        phrase = Phrase(audio_seconds=audio, confidence=confidence, from_prompt=False)
        with self.assertLogs("core.wake", level="INFO") as logs:
            return self.gate.check("Джарвис.", phrase), logs.output

    def test_confident_bare_word_opens_the_window(self):
        for confidence in (0.4, 0.5, 0.9, None):
            with self.subTest(confidence=confidence):
                decision, _ = self.listen(confidence)
                self.assertEqual((decision.action, decision.opens_window), ("listen", True))

    def test_uncertain_bare_word_answers_but_keeps_the_window_closed(self):
        for confidence in (0.3, 0.35, 0.39):
            with self.subTest(confidence=confidence):
                decision, logs = self.listen(confidence)
                self.assertEqual((decision.action, decision.opens_window), ("listen", False))
                self.assertTrue(any("окно диалога не открыто" in line and f"{confidence:.2f} < 0.40" in line for line in logs))

    def test_main_passes_the_flag_to_speak(self):
        app = MagicMock()
        app.wake = self.hooks.gate()
        app.has_pending.return_value = False
        for confidence, expected in ((0.9, True), (0.35, False)):
            app.engine.speak.reset_mock()
            with self.assertLogs("core.wake", level="INFO"):
                main.VoiceApp.process_command(app, "Джарвис", phrase=Phrase(audio_seconds=1.8, confidence=confidence))
            app.engine.speak.assert_called_once_with("Слушаю.", opens_window=expected)
            app.ai.respond.assert_not_called()

    def test_distorted_bare_word_behaves_the_same(self):
        phrase = Phrase(audio_seconds=1.8, confidence=0.35, from_prompt=False)
        with self.assertLogs("core.wake", level="INFO"):
            decision = self.gate.check("Червис.", phrase)
        self.assertEqual((decision.action, decision.opens_window), ("listen", False))


class TestLogOfDistortion(unittest.TestCase):
    def setUp(self):
        self.gate = Hooks().gate()

    def test_distorted_word_is_logged_with_the_distance_without_text(self):
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.gate.check("Шарвис, секретная команда", Phrase(audio_seconds=3.0, confidence=0.9))
        self.assertIn("INFO:core.wake:слово «Джарвис» засчитано по искажению (расстояние 0.83)", logs.output)
        self.assertNotIn("секретн", "\n".join(logs.output))
        self.assertNotIn("шарвис", "\n".join(logs.output).lower())

    def test_listed_distortion_is_logged_too(self):
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.gate.check("Червис, открой калькулятор", Phrase(audio_seconds=3.0, confidence=0.9))
        self.assertTrue(any("засчитано по искажению (расстояние 0.6" in line for line in logs.output))

    def test_the_name_itself_is_not_logged_as_a_distortion(self):
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.gate.check("Джарвис, открой калькулятор", Phrase(audio_seconds=3.0, confidence=0.9))
            self.gate.check("Jarvis, открой калькулятор", Phrase(audio_seconds=3.0, confidence=0.9))
            self.gate.check("Сибирь")                                                  # чтобы список логов не был пустым
        self.assertFalse(any("по искажению" in line for line in logs.output))

    def test_custom_word_is_logged_as_a_variant(self):
        gate = Hooks().gate()
        gate.words = (*WAKE_WORDS, "ассистент")
        with self.assertLogs("core.wake", level="INFO") as logs:
            self.assertEqual(gate.check("Ассистент, открой калькулятор").text, "открой калькулятор")
        self.assertIn("INFO:core.wake:слово «Джарвис» засчитано по искажению (расстояние 1.00)", logs.output)


class TestExtraWords(unittest.TestCase):
    def words(self, value):
        return extra_wake_words(env_file(f"WAKE_WORDS={value}\n"))

    def test_empty_and_missing(self):
        self.assertEqual(extra_wake_words(env_file("")), ())
        self.assertEqual(self.words(""), ())

    def test_valid_words_are_added_and_normalized(self):
        self.assertEqual(self.words("Ассистент, КОМПЬЮТЕР , ёлка"), ("ассистент", "компьютер", "елка"))
        self.assertEqual(self.words("джарвис, ассистент"), ("ассистент",))          # встроенное слово не дублируется
        self.assertEqual(self.words("assistant, computer"), ("assistant", "computer"))
        self.assertEqual(self.words("ассистент, ассистент"), ("ассистент",))

    def test_short_words_are_skipped(self):
        with self.assertLogs("core.wake", level="WARNING") as logs:
            self.assertEqual(self.words("бот, мак, домовой"), ("домовой",))
        self.assertTrue(any("«бот» пропущено (короче 4 символов)" in line for line in logs.output))
        self.assertEqual(self.words("абвг"), ("абвг",))                              # ровно 4 символа допустимо

    def test_mixed_alphabets_and_non_letters_are_skipped(self):
        with self.assertLogs("core.wake", level="WARNING") as logs:
            self.assertEqual(self.words("джарvis, помощник, hello-world, робот2"), ("помощник",))
        self.assertEqual(sum("пропущено" in line for line in logs.output), 3)

    def test_at_most_ten_words(self):
        value = ", ".join(f"слово{chr(1072 + i)}" for i in range(0)) or ", ".join(
            w for w in ("альфа", "бета", "гамма", "дельта", "омега", "сигма", "тета", "кеппа", "лямбда", "сайгон",
                        "одиннадцатое", "двенадцатое"))
        with self.assertLogs("core.wake", level="WARNING") as logs:
            words = self.words(value)
        self.assertEqual(len(words), 10)
        self.assertEqual(sum("не больше 10 слов" in line for line in logs.output), 2)

    def test_gate_uses_the_words_from_env_at_start(self):
        engine, ai = MagicMock(), MagicMock()
        with patch("core.settings.ENV_FILE", env_file("WAKE_WORDS=ассистент, компьютер\n")):
            gate = build_gate(engine, ai)
        self.assertEqual(gate.words, (*WAKE_WORDS, "ассистент", "компьютер"))
        self.assertEqual(gate.check("Компьютер, открой калькулятор").text, "открой калькулятор")
        self.assertEqual(gate.check("открой калькулятор, ассистент").text, "открой калькулятор")

    def test_without_the_setting_only_the_builtin_list(self):
        engine = MagicMock()
        engine.speaking.is_set.return_value = False
        engine.window_opened_at = None
        with patch("core.settings.ENV_FILE", env_file("")):
            gate = build_gate(engine, MagicMock())
        self.assertEqual(gate.words, WAKE_WORDS)
        with self.assertLogs("core.wake", level="INFO"):
            self.assertEqual(gate.check("Ассистент, открой калькулятор").action, "drop")

    def test_env_example_has_the_setting(self):
        self.assertIn("WAKE_WORDS=", Path(__file__).with_name(".env.example").read_text(encoding="utf-8"))


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

    def test_unsure_recognition_is_marked_at_the_gate_threshold(self):
        # Через движок уверенность ниже e^-1 ≈ 0,37 не доходит: её отсекает фильтр шума (avg_logprob < -1,0)
        self.assertFalse(self.recognize("Джарвис, открой калькулятор", 3.0, 0.44).wake_from_prompt)   # живой лог
        self.assertFalse(self.recognize("Джарвис, открой калькулятор", 3.0, 0.38).wake_from_prompt)
        self.assertTrue(prompt_induced("Джарвис, открой калькулятор", 3.0, 0.29))

    def test_confidence_below_the_noise_filter_never_reaches_the_gate(self):
        self.assertIsNone(self.recognize("Джарвис, открой калькулятор", 3.0, 0.25))

    def test_without_a_prompt_nothing_is_marked(self):
        provider_less = SpeechToText(ready_engine(good("Джарвис, открой калькулятор", avg_logprob=math.log(0.5))), None,
                                     None, notify=lambda t: None)
        self.assertFalse(provider_less.recognize(make_audio(0.9)).wake_from_prompt)      # короткая запись, но подсказки нет

    def test_text_without_the_word_is_not_marked(self):
        self.assertFalse(self.recognize("открой калькулятор", 1.0, 0.5).wake_from_prompt)

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
    def run_check(self, texts, seconds, confidences, *args, env=""):
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
        with patch("core.settings.ENV_FILE", env_file(env)), patch_installed(), \
                patch("core.stt.check.local_engine", return_value=engine), \
                patch("core.stt.check.record_phrases", side_effect=lambda n: (next(audios) for _ in range(n))):
            code = check.main(["--mic", "--wake", "--repeat", str(len(texts)), *args], out=lines.append)
        return code, "\n".join(lines)

    def test_reports_whether_the_word_counted_why_and_which_word(self):
        code, text = self.run_check(["Джарвис.", "Джарвис, открой калькулятор", "открой калькулятор, Червис",
                                     "Сибирь.", "открой Джарвис калькулятор", "Шарвис, громче"],
                                    [0.6, 3.0, 2.5, 1.5, 2.5, 3.0], [0.4, 0.9, 0.9, 0.9, 0.9, 0.9])
        self.assertEqual(code, 0)
        self.assertIn("[1] слово «Джарвис»: не засчитано — слово возможно из подсказки Whisper (остаток пуст); "
                      "джарвис ≈ джарвис, 1.00, из списка", text)
        self.assertIn("[2] слово «Джарвис»: ЗАСЧИТАНО — слово в начале; джарвис ≈ джарвис, 1.00, из списка; "
                      "команда: «открой калькулятор»", text)
        self.assertRegex(text, r"\[3\] слово «Джарвис»: ЗАСЧИТАНО — слово в конце; червис ≈ джарвис, 0\.\d\d, из списка; "
                               r"команда: «открой калькулятор»")
        self.assertIn("[4] слово «Джарвис»: не засчитано — слова нет", text)
        self.assertIn("[5] слово «Джарвис»: не засчитано — слово в середине фразы", text)
        self.assertIn("[6] слово «Джарвис»: ЗАСЧИТАНО — слово в начале; шарвис ≈ чарвис, 0.83, по искажению; "
                      "команда: «громче»", text)

    def test_bare_word_on_a_good_recording(self):
        _, text = self.run_check(["Джарвис."], [1.8], [0.9])
        self.assertIn("ЗАСЧИТАНО — слово в начале; джарвис ≈ джарвис, 1.00, из списка; команды нет, ответ «Слушаю.»", text)

    def test_bare_word_with_weak_confidence_says_that_the_window_will_not_open(self):
        _, text = self.run_check(["Джарвис."], [1.8], [0.38], "--no-prompt")
        self.assertIn("команды нет, ответ «Слушаю.», окно не откроется (уверенность 0.38 < 0.40)", text)

    def test_summary_at_the_end(self):
        _, text = self.run_check(["Джарвис, открой калькулятор", "Сибирь.", "Червис.", "Спасибо."], [3.0, 1.5, 2.0, 1.5],
                                 [0.9, 0.5, 0.7, 0.4], "--no-prompt")
        last = text.splitlines()[-1]
        self.assertEqual(last, "Сводка --wake: записей 4; слово засчитано 2; не засчитано 2; медианная уверенность 0.60")

    def test_summary_median_for_an_odd_count(self):
        _, text = self.run_check(["Джарвис, да", "Сибирь.", "Спасибо."], [3.0, 1.5, 1.5], [0.8, 0.5, 0.4], "--no-prompt")
        self.assertEqual(text.splitlines()[-1],
                         "Сводка --wake: записей 3; слово засчитано 1; не засчитано 2; медианная уверенность 0.50")

    def test_summary_counts_filtered_recordings_separately(self):
        _, text = self.run_check(["Джарвис, открой", "Продолжение следует..."], [3.0, 3.0], [0.9, 0.9], "--no-prompt")
        last = text.splitlines()[-1]
        self.assertIn("записей 2; слово засчитано 1; не засчитано 0; отброшено фильтром шума 1", last)

    def test_no_summary_without_the_flag(self):
        engine = StubWhisper([good("Джарвис, открой калькулятор")])
        engine.model_path = lambda: Path("/нет/такого/ggml-x.bin")
        lines = []
        with patch("core.settings.ENV_FILE", env_file("")), patch_installed(), \
                patch("core.stt.check.local_engine", return_value=engine), \
                patch("core.stt.check.record_phrases", side_effect=lambda n: (make_audio(3.0) for _ in range(n))):
            check.main(["--mic", "--repeat", "1"], out=lines.append)
        text = "\n".join(lines)
        self.assertNotIn("слово «Джарвис»", text)
        self.assertNotIn("Сводка", text)

    def test_custom_words_from_env_are_used_in_the_diagnostics(self):
        _, text = self.run_check(["Ассистент, открой калькулятор"], [3.0], [0.9], "--no-prompt", env="WAKE_WORDS=ассистент\n")
        self.assertIn("ЗАСЧИТАНО — слово в начале; ассистент ≈ ассистент, 1.00, из WAKE_WORDS", text)
        _, text = self.run_check(["Ассистент, открой калькулятор"], [3.0], [0.9], "--no-prompt")
        self.assertIn("не засчитано — слова нет", text)


if __name__ == "__main__":
    unittest.main()
