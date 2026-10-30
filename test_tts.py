"""Этап 9.2: озвучка core/tts. Всё на подменах: sherpa_onnx, afplay и say — фальшивки, звук не играет, модели не грузятся."""
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков, загрузки моделей и записи в домашние папки
import numpy as np

from core.engine import SpeechEngine
from core.tts import check, effect
from core.tts import sherpa as sherpa_module
from core.tts.base import TTSEngine, TTSSettings, TTSUnavailable, tts_settings
from core.tts.factory import FAILED_NOTICE, NOT_INSTALLED_NOTICE, SpeechVoice, build_tts, models_dir
from core.tts.player import PlayResult, SentenceQueue, TempWavs, split_sentences
from core.tts.say import SayEngine, pick_voice, say_command
from core.tts.sherpa import NeuralResult, SherpaEngine

SECRET = "секретная фраза пользователя"


def env_file(text):
    path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    return predicate()


# --- настройки ---------------------------------------------------------------------------------------------------------
class TestSettings(unittest.TestCase):
    def settings(self, text="", cores=8):
        with patch("core.tts.base.os.cpu_count", return_value=cores):
            return tts_settings(env_file(text))

    def test_defaults(self):
        self.assertEqual(self.settings(), TTSSettings(engine="say", voice="", speed=1.0, threads=2, effect=False, pitch=-2.0,
                                                       reverb=0.12))

    def test_engine_values(self):
        self.assertEqual(self.settings("TTS_ENGINE=sherpa\n").engine, "sherpa")
        self.assertEqual(self.settings("TTS_ENGINE=SAY\n").engine, "say")
        with self.assertLogs("core.tts.base", level="WARNING") as logs:
            self.assertEqual(self.settings("TTS_ENGINE=мусор\n").engine, "say")
        self.assertIn("TTS_ENGINE=мусор не поддерживается", logs.output[0])

    def test_garbage_numbers_fall_back_to_defaults_with_a_warning(self):
        for name, default in (("TTS_SPEED", "speed"), ("TTS_PITCH", "pitch"), ("TTS_REVERB", "reverb"), ("TTS_THREADS", "threads")):
            with self.subTest(name=name), self.assertLogs("core.tts.base", level="WARNING") as logs:
                value = getattr(self.settings(f"{name}=мусор\n"), default)
            self.assertEqual(value, getattr(TTSSettings(), default))
            self.assertIn(f"{name}=мусор не число", logs.output[0])

    def test_bounds(self):
        cases = (("TTS_SPEED", "0.1", "speed", 0.8), ("TTS_SPEED", "9", "speed", 1.5), ("TTS_PITCH", "-20", "pitch", -6.0),
                 ("TTS_PITCH", "3", "pitch", 0.0), ("TTS_REVERB", "-1", "reverb", 0.0), ("TTS_REVERB", "0.9", "reverb", 0.3))
        for name, raw, field, expected in cases:
            with self.subTest(name=name, raw=raw), self.assertLogs("core.tts.base", level="WARNING"):
                self.assertEqual(getattr(self.settings(f"{name}={raw}\n"), field), expected)

    def test_valid_values_are_kept_without_warnings(self):
        s = self.settings("TTS_SPEED=1.2\nTTS_PITCH=-4\nTTS_REVERB=0.2\nTTS_THREADS=3\nTTS_EFFECT=jarvis\nTTS_VOICE= vits-x \n")
        self.assertEqual((s.speed, s.pitch, s.reverb, s.threads, s.effect, s.voice), (1.2, -4.0, 0.2, 3, True, "vits-x"))

    def test_unicode_minus_and_comma(self):
        s = self.settings("TTS_PITCH=−3\nTTS_SPEED=1,25\n")
        self.assertEqual((s.pitch, s.speed), (-3.0, 1.25))

    def test_effect_values(self):
        self.assertFalse(self.settings("TTS_EFFECT=off\n").effect)
        self.assertTrue(self.settings("TTS_EFFECT=Jarvis\n").effect)
        with self.assertLogs("core.tts.base", level="WARNING"):
            self.assertFalse(self.settings("TTS_EFFECT=on\n").effect)

    def test_threads_never_exceed_half_of_the_cores(self):
        self.assertEqual(self.settings("TTS_THREADS=8\n", cores=8).threads, 4)
        self.assertEqual(self.settings("", cores=2).threads, 1)
        self.assertEqual(self.settings("TTS_THREADS=2\n", cores=8).threads, 2)

    def test_env_example_lists_every_setting(self):
        text = Path(__file__).with_name(".env.example").read_text(encoding="utf-8")
        for line in ("TTS_ENGINE=say", "TTS_VOICE=", "TTS_SPEED=1.0", "TTS_THREADS=2", "TTS_EFFECT=off", "TTS_PITCH=-2",
                     "TTS_REVERB=0.12"):
            self.assertIn(line, text)


# --- нарезка на предложения ---------------------------------------------------------------------------------------------
class TestSplitSentences(unittest.TestCase):
    def test_by_dots_questions_and_exclamations(self):
        self.assertEqual(split_sentences("Открыто приложение Telegram. Закрыто приложение Safari."),
                         ["Открыто приложение Telegram.", "Закрыто приложение Safari."])
        self.assertEqual(split_sentences("Правда? Да! Конечно."), ["Правда?", "Да!", "Конечно."])
        self.assertEqual(split_sentences("Что?! Не может быть…"), ["Что?!", "Не может быть…"])

    def test_numbers_and_dates_are_not_cut(self):
        for text in ("Температура 3.5 градуса.", "Сегодня 27.10.2026, вторник.", "Версия 1.2.3 установлена.",
                     "Сейчас вторник, 27 октября, 17:12.", "Курс 94.5 рубля за доллар.", "Зайдите на example.com, пожалуйста."):
            with self.subTest(text=text):
                self.assertEqual(split_sentences(text), [text])

    def test_abbreviations_are_not_cut(self):
        for text in ("Это бывает у кошек, собак и т. д. Дальше.", "Нужны яблоки, груши и т.п. Это всё.", "То есть т. е. так."):
            with self.subTest(text=text):
                self.assertEqual(split_sentences(text), [text])                      # после сокращения не режем даже перед заглавной
        self.assertEqual(len(split_sentences("Смотрите рис. 2 на стр. 5. Конец.")), 2)       # «5.» — не сокращение, дальше новое предложение
        self.assertEqual(split_sentences("Он родился в 1799 г. в Москве."), ["Он родился в 1799 г. в Москве."])
        self.assertEqual(split_sentences("А. С. Пушкин жил давно."), ["А. С. Пушкин жил давно."])
        self.assertEqual(split_sentences("Цена 5 руб. за штуку."), ["Цена 5 руб. за штуку."])

    def test_a_dot_before_a_lowercase_word_is_not_an_end(self):
        self.assertEqual(split_sentences("Смотри см. выше и дальше."), ["Смотри см. выше и дальше."])

    def test_lines_and_empty_pieces(self):
        self.assertEqual(split_sentences("Первая строка\n\nВторая строка.\n  \nТретья"), ["Первая строка", "Вторая строка.", "Третья"])
        for text in ("", "   ", "\n\n", None):
            self.assertEqual(split_sentences(text), [])

    def test_lonely_marks_are_glued_to_the_neighbour(self):
        self.assertEqual(split_sentences("Привет! ? Дальше"), ["Привет! ?", "Дальше"])

    def test_no_text_is_lost(self):
        text = "Сегодня вторник. Погода: 3.5 градуса, т. е. тепло! Идёт дождь? Возьмите зонт…"
        self.assertEqual(" ".join(split_sentences(text)).split(), text.split())


class TestLongSentences(unittest.TestCase):
    def long(self):
        return ("слово " * 30 + "раз, ") + ("слово " * 30 + "два; ") + ("слово " * 20 + "три, 3,5 процента и т. д.")

    def test_long_sentence_is_cut_by_commas_and_semicolons_to_at_most_200(self):
        text = self.long()
        self.assertGreater(len(text), 200)
        parts = split_sentences(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 200 for p in parts), [len(p) for p in parts])
        self.assertEqual(" ".join(parts).split(), text.split())            # ничего не потеряно и не склеено

    def test_the_cut_does_not_break_numbers_or_abbreviations(self):
        text = "слово " * 40 + "стоимость 3,5 рубля, " + "слово " * 40 + "и т. д., " + "слово " * 30 + "конец."
        parts = split_sentences(text)
        joined = " ".join(parts)
        self.assertIn("3,5", joined)
        self.assertIn("т. д.", joined)
        for part in parts:
            self.assertFalse(part.startswith((",", ";", "5 ")))             # «3,5» не разорвано
            self.assertFalse(part.endswith(" 3"))
        self.assertTrue(all(len(p) <= 200 for p in parts))

    def test_a_short_sentence_is_not_cut_at_commas(self):
        text = "Сейчас вторник, 27 октября, 17:12."
        self.assertEqual(split_sentences(text), [text])

    def test_a_long_piece_without_commas_is_cut_at_spaces(self):
        text = "слово " * 60
        parts = split_sentences(text.strip())
        self.assertTrue(all(len(p) <= 200 for p in parts))
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_first_piece_is_short_enough_to_start_quickly(self):
        self.assertLessEqual(len(split_sentences(self.long())[0]), 200)

    def test_the_limit_argument(self):
        parts = split_sentences("раз два три, четыре пять шесть, семь восемь девять", limit=20)
        self.assertTrue(all(len(p) <= 20 for p in parts), parts)


# --- временные WAV и очередь -------------------------------------------------------------------------------------------
class FakePlayer:
    """Вместо afplay: запоминает, что «играл», и умеет ждать, пока тест его отпустит."""

    def __init__(self):
        self.played, self.gates, self.stopped, self.fail_at = [], {}, threading.Event(), None
        self.started = threading.Event()

    def play(self, path):
        index = len(self.played)
        info = {"path": path, "exists": os.path.exists(path), "mode": stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else None,
                "size": os.path.getsize(path) if os.path.exists(path) else 0}
        self.played.append(info)
        self.started.set()
        gate = self.gates.get(index)
        if gate:
            gate.wait(3)
        if self.stopped.is_set():
            return True
        return self.fail_at != index

    def stop(self):
        self.stopped.set()
        for gate in self.gates.values():
            gate.set()


class Rig:
    """Очередь на фальшивом синтезе и плеере."""

    def __init__(self, synth_gates=None, fail_synth_at=None):
        self.wavs = TempWavs()
        self.synthesized = []
        self.synth_gates = synth_gates or {}
        self.fail_synth_at = fail_synth_at
        self.player = FakePlayer()
        self.queue = SentenceQueue(self.synth, self.player, self.wavs)

    def synth(self, sentence):
        index = len(self.synthesized)
        self.synthesized.append(sentence)
        gate = self.synth_gates.get(index)
        if gate:
            gate.wait(3)
        if self.fail_synth_at == index:
            raise RuntimeError("синтез упал")
        return [0.1] * 160, 16000

    def files(self):
        return os.listdir(self.wavs.directory) if self.wavs._dir else []


class RigCase(unittest.TestCase):
    def rig(self, **kwargs):
        rig = Rig(**kwargs)
        self.addCleanup(rig.wavs.cleanup)
        return rig


class TestTempWavs(RigCase):
    def test_names_are_random_and_carry_no_text_or_time(self):
        rig = self.rig()
        names = set()
        with patch("time.time", return_value=1_700_000_000.0), patch("time.monotonic", return_value=42.0):
            for _ in range(40):
                path = rig.wavs.write([0.1] * 16, 16000)
                names.add(os.path.basename(path))
        self.assertEqual(len(names), 40)                                              # все разные при одном и том же времени
        for name in names:
            self.assertRegex(name, r"^[0-9a-f]{16}\.wav$")
            self.assertNotIn("1700000000", name)

    def test_names_do_not_depend_on_the_text_being_spoken(self):
        rig = self.rig()
        rig.queue.speak([SECRET, "Открыто приложение Telegram."])
        for info in rig.player.played:
            name = os.path.basename(info["path"])
            self.assertRegex(name, r"^[0-9a-f]{16}\.wav$")
            self.assertNotIn("секрет", name)
            self.assertNotIn("telegram", name.lower())

    def test_permissions(self):
        rig = self.rig()
        rig.queue.speak(["Первое.", "Второе."])
        self.assertEqual(stat.S_IMODE(os.stat(rig.wavs.directory).st_mode), 0o700)
        self.assertEqual([i["mode"] for i in rig.player.played], [0o600, 0o600])

    def test_wav_is_a_valid_16_bit_mono_file(self):
        import wave
        rig = self.rig()
        path = rig.wavs.write([0.0, 0.5, -0.5, 1.5, -1.5], 22050)
        with wave.open(path, "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()), (1, 2, 22050, 5))
            data = np.frombuffer(wav.readframes(5), dtype="<i2")
        self.assertEqual(int(data[3]), 32767)                                         # выше 1,0 обрезается без переполнения
        self.assertEqual(int(data[4]), -32767)

    def test_files_are_removed_right_after_playback(self):
        rig = self.rig()
        rig.queue.speak(["Раз.", "Два.", "Три."])
        self.assertTrue(all(i["exists"] for i in rig.player.played))
        self.assertEqual(rig.files(), [])
        self.assertFalse(any(os.path.exists(i["path"]) for i in rig.player.played))

    def test_cleanup_removes_the_directory(self):
        wavs = TempWavs()
        wavs.write([0.1] * 8, 8000)
        directory = wavs.directory
        self.assertTrue(os.path.isdir(directory))
        wavs.cleanup()
        self.assertFalse(os.path.exists(directory))
        wavs.cleanup()                                                                # повторный вызов безопасен

    def test_cleanup_is_registered_at_exit(self):
        with patch("core.tts.player.atexit.register") as register:
            wavs = TempWavs()
            _ = wavs.directory
            _ = wavs.directory
        register.assert_called_once_with(wavs.cleanup)
        wavs.cleanup()


class TestSentenceQueue(RigCase):
    def test_sentences_play_in_order(self):
        rig = self.rig()
        result = rig.queue.speak(["Первое.", "Второе.", "Третье."])
        self.assertEqual((result.done, result.error, result.stopped), (3, False, False))
        self.assertEqual(rig.synthesized, ["Первое.", "Второе.", "Третье."])
        self.assertEqual(len(rig.player.played), 3)

    def test_the_first_sentence_sounds_before_the_last_is_synthesized(self):
        gate = threading.Event()
        rig = self.rig(synth_gates={2: gate})                                          # третье предложение синтезируется долго
        thread = threading.Thread(target=rig.queue.speak, args=(["Раз.", "Два.", "Три."],))
        thread.start()
        self.assertTrue(wait_until(lambda: len(rig.player.played) >= 1))              # первое уже звучит
        self.assertEqual(len(rig.synthesized), 3)
        gate.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_at_most_two_sentences_ahead(self):
        rig = self.rig()
        release = threading.Event()
        rig.player.gates[0] = release                                                  # первое предложение играет долго
        thread = threading.Thread(target=rig.queue.speak, args=([f"Предложение {i}." for i in range(8)],))
        thread.start()
        self.assertTrue(wait_until(lambda: len(rig.player.played) >= 1))
        time.sleep(0.3)
        # играет №1, в очереди №2 и №3, ещё одно готово и ждёт места: дальше синтез не уходит
        self.assertLessEqual(len(rig.synthesized), 4)
        self.assertGreaterEqual(len(rig.synthesized), 3)
        release.set()
        thread.join(3)
        self.assertEqual(len(rig.player.played), 8)

    def test_stop_in_the_middle_of_speech(self):
        rig = self.rig()
        rig.player.gates[0] = threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(rig.queue.speak([f"Предложение {i}." for i in range(6)])))
        thread.start()
        self.assertTrue(wait_until(lambda: len(rig.player.played) >= 1))
        rig.queue.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0].stopped)
        self.assertFalse(results[0].error)
        self.assertEqual(len(rig.player.played), 1)                                    # остальные не играли
        self.assertTrue(rig.player.stopped.is_set())
        self.assertTrue(wait_until(lambda: rig.files() == []))                         # очередь очищена, файлов нет

    def test_generation_grows_on_stop(self):
        rig = self.rig()
        before = rig.queue.generation
        rig.queue.stop()
        self.assertEqual(rig.queue.generation, before + 1)

    def test_late_synthesis_after_stop_is_discarded(self):
        gate = threading.Event()
        rig = self.rig(synth_gates={1: gate})                                          # второе предложение «задумалось»
        results = []
        thread = threading.Thread(target=lambda: results.append(rig.queue.speak(["Раз.", "Два.", "Три."])))
        thread.start()
        self.assertTrue(wait_until(lambda: len(rig.synthesized) >= 2))
        rig.queue.stop()
        gate.set()                                                                      # синтез закончился уже после остановки
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0].stopped)
        self.assertLessEqual(len(rig.player.played), 1)
        self.assertNotIn("Три.", rig.synthesized)
        self.assertTrue(wait_until(lambda: rig.files() == []))                         # запоздалый WAV не остался

    def test_a_new_phrase_after_a_stop_plays_normally(self):
        rig = self.rig()
        rig.queue.stop()
        rig.player.stopped.clear()
        result = rig.queue.speak(["Снова."])
        self.assertEqual((result.done, result.stopped, result.error), (1, False, False))

    def test_synthesis_error_stops_with_an_error_and_keeps_what_was_played(self):
        rig = self.rig(fail_synth_at=1)
        with self.assertLogs("core.tts.player", level="WARNING") as logs:
            result = rig.queue.speak([SECRET, "Второе.", "Третье."])
        self.assertEqual((result.done, result.error), (1, True))
        self.assertNotIn("секрет", "\n".join(logs.output))                             # в логе нет текста озвучки
        self.assertIn("синтез предложения 2 не удался", logs.output[0])
        self.assertEqual(rig.files(), [])

    def test_playback_error_stops_with_an_error(self):
        rig = self.rig()
        rig.player.fail_at = 1
        with self.assertLogs("core.tts.player", level="WARNING") as logs:
            result = rig.queue.speak([SECRET, "Второе.", "Третье."])
        self.assertEqual((result.done, result.error), (1, True))
        self.assertNotIn("секрет", "\n".join(logs.output))
        self.assertEqual(rig.files(), [])

    def test_empty_input(self):
        rig = self.rig()
        self.assertEqual(rig.queue.speak([]), PlayResult())


class TestAfplayPlayer(unittest.TestCase):
    def test_arguments_are_a_list_without_shell(self):
        from core.tts.player import AfplayPlayer
        with patch("core.tts.player.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.assertTrue(AfplayPlayer().play("/tmp/x.wav"))
        popen.assert_called_once_with(["afplay", "/tmp/x.wav"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_nonzero_exit_is_an_error_but_not_after_stop(self):
        from core.tts.player import AfplayPlayer
        with patch("core.tts.player.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 1
            self.assertFalse(AfplayPlayer().play("/tmp/x.wav"))
            player = AfplayPlayer()
            popen.return_value.wait.side_effect = lambda: (player.stop(), -15)[1]
            popen.return_value.poll.return_value = None
            self.assertTrue(player.play("/tmp/x.wav"))

    def test_missing_afplay_is_an_error(self):
        from core.tts.player import AfplayPlayer
        with patch("core.tts.player.subprocess.Popen", side_effect=OSError("нет")), self.assertLogs("core.tts.player", level="ERROR"):
            self.assertFalse(AfplayPlayer().play("/tmp/x.wav"))

    def test_stop_terminates_the_process(self):
        from core.tts.player import AfplayPlayer
        player = AfplayPlayer()
        player._proc = MagicMock()
        player._proc.poll.return_value = None
        player.stop()
        player._proc.terminate.assert_called_once()


# --- эффект ------------------------------------------------------------------------------------------------------------
def sine(freq, seconds=1.0, rate=22050, amp=0.5):
    t = np.arange(int(rate * seconds)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def dominant(signal, rate):
    spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
    return float(np.argmax(spectrum)) * rate / len(signal)


class TestEffect(unittest.TestCase):
    RATE = 22050

    def test_pitch_ratio_and_bounds(self):
        self.assertAlmostEqual(effect.pitch_ratio(-2), 2 ** (-2 / 12), places=6)
        self.assertEqual(effect.pitch_ratio(0), 1.0)
        self.assertAlmostEqual(effect.pitch_ratio(-6), 0.7071, places=3)
        self.assertEqual(effect.pitch_ratio(-24), 0.7)                                   # граница 0,7
        self.assertEqual(effect.pitch_ratio(5), 1.0)                                     # граница 1,0

    def test_model_speed_is_compensated_for_the_stretch(self):
        self.assertAlmostEqual(effect.compensated_speed(1.0, -2), 1 / effect.pitch_ratio(-2), places=6)
        self.assertEqual(effect.compensated_speed(1.0, 0), 1.0)
        self.assertAlmostEqual(effect.compensated_speed(1.5, -6), 1.5 / effect.pitch_ratio(-6), places=6)
        self.assertEqual(effect.compensated_speed(1.5, -6), 1.5 / effect.pitch_ratio(-6))        # 2,12 — ещё в пределах 2,5
        self.assertEqual(effect.compensated_speed(5.0, -6), effect.MODEL_SPEED_RANGE[1])           # сверху ограничено
        self.assertEqual(effect.compensated_speed(0.8, -6), 0.8 / effect.pitch_ratio(-6))

    def test_lower_pitch_lowers_the_frequency(self):
        x = sine(440)
        for semitones in (-2, -4, -6):
            y = effect.lower_pitch(x, semitones)
            expected = 440 * effect.pitch_ratio(semitones)
            self.assertAlmostEqual(dominant(y, self.RATE), expected, delta=8, msg=semitones)

    def test_duration_is_kept_with_the_compensated_synthesis(self):
        """Модель синтезировала быстрее в 1/r раз (короче в r раз); после растяжения длина как была."""
        for semitones in (-2, -6):
            ratio = effect.pitch_ratio(semitones)
            original = 22050
            short = sine(300, seconds=original * ratio / 22050)                           # то, что вернула модель
            stretched = effect.lower_pitch(short, semitones)
            self.assertAlmostEqual(len(stretched), original, delta=3)

    def test_no_change_at_zero_semitones(self):
        x = sine(300)
        np.testing.assert_array_equal(effect.lower_pitch(x, 0), x)

    def test_reverb_adds_the_tail_and_keeps_the_start(self):
        x = sine(300, 0.5)
        y, tail = effect.add_reverb(x, self.RATE, 0.12)
        expected_tail = int(self.RATE * effect.TAIL_SECONDS) - 1
        self.assertEqual(len(y), len(x) + expected_tail)
        self.assertAlmostEqual(tail, expected_tail / self.RATE, places=6)
        self.assertTrue(0.08 <= tail <= 0.15)                                            # хвост 80–150 мс
        self.assertGreater(float(np.sum(y[len(x):] ** 2)), 0.0)                          # в хвосте есть звук
        self.assertGreater(float(np.corrcoef(x[:2000], y[:2000])[0, 1]), 0.9)            # начало похоже на исходное

    def test_reverb_tail_energy_grows_with_the_mix(self):
        x = sine(300, 0.5)
        energies = [float(np.sum(effect.add_reverb(x, self.RATE, mix)[0][len(x):] ** 2)) for mix in (0.05, 0.12, 0.3)]
        self.assertLess(energies[0], energies[1])
        self.assertLess(energies[1], energies[2])

    def test_zero_mix_is_a_noop_without_tail(self):
        x = sine(300, 0.3)
        y, tail = effect.add_reverb(x, self.RATE, 0.0)
        self.assertEqual(tail, 0.0)
        np.testing.assert_allclose(y, x)

    def test_reverb_is_deterministic(self):
        x = sine(300, 0.3)
        np.testing.assert_array_equal(effect.add_reverb(x, self.RATE, 0.12)[0], effect.add_reverb(x, self.RATE, 0.12)[0])

    def test_loud_signal_does_not_clip(self):
        loud = sine(300, 0.5, amp=1.0)
        out, _ = effect.apply_effect(loud, self.RATE, -4, 0.3)
        self.assertLessEqual(float(np.max(np.abs(out))), 1.0)
        self.assertLessEqual(float(np.max(np.abs(out))), effect.PEAK_LIMIT + 1e-9)

    def test_quiet_signal_keeps_its_loudness(self):
        quiet = sine(300, 0.5, amp=0.05)
        out, _ = effect.apply_effect(quiet, self.RATE, -2, 0.12)
        rms_in, rms_out = float(np.sqrt(np.mean(quiet ** 2))), float(np.sqrt(np.mean(out ** 2)))
        self.assertAlmostEqual(rms_out / rms_in, 1.0, delta=0.05)

    def test_apply_effect_returns_tail_and_longer_signal(self):
        x = sine(300, 0.5)
        out, tail = effect.apply_effect(x, self.RATE, -2, 0.12)
        self.assertGreater(tail, 0.0)
        self.assertGreater(len(out), len(effect.lower_pitch(x, -2)))
        out, tail = effect.apply_effect(x, self.RATE, -2, 0.0)
        self.assertEqual(tail, 0.0)

    def test_empty_and_tiny_signals(self):
        out, tail = effect.apply_effect([], self.RATE, -2, 0.12)
        self.assertEqual((len(out), tail), (0, 0.0))
        out, _ = effect.apply_effect([0.1], self.RATE, -2, 0.12)
        self.assertTrue(np.all(np.isfinite(out)))
        silent, _ = effect.apply_effect(np.zeros(1000), self.RATE, -2, 0.12)
        self.assertTrue(np.all(np.isfinite(silent)))

    def test_numpy_missing_means_unavailable(self):
        with patch("core.tts.effect.numpy_module", return_value=None):
            self.assertFalse(effect.available())
        self.assertTrue(effect.available())


# --- sherpa: фальшивый пакет -------------------------------------------------------------------------------------------
class FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.valid = True

    def validate(self):
        return self.valid


class FakeGenerationConfig:
    def __init__(self):
        self.sid, self.speed, self.silence_scale = None, None, None


class FakeBackend:
    """sherpa_onnx из того, что нужно движку: конфиги и OfflineTts.generate."""
    instances = []
    fail_on_create = None
    config_valid = True
    empty = False
    rate = 22050

    class OfflineTtsVitsModelConfig(FakeConfig):
        pass

    class OfflineTtsModelConfig(FakeConfig):
        pass

    class OfflineTtsConfig(FakeConfig):
        def validate(self):
            return FakeBackend.config_valid

    GenerationConfig = FakeGenerationConfig

    class OfflineTts:
        def __init__(self, config):
            if FakeBackend.fail_on_create:
                raise FakeBackend.fail_on_create
            self.config, self.calls = config, []
            FakeBackend.instances.append(self)

        def generate(self, text, config):
            self.calls.append((text, config.sid, config.speed, config.silence_scale))
            samples = [] if FakeBackend.empty else [0.1] * int(FakeBackend.rate * 0.1)
            return types.SimpleNamespace(samples=samples, sample_rate=FakeBackend.rate)


def make_voice_folder(root, name="vits-piper-ru_RU-denis-medium-fp16", onnx=True, tokens=True, data=True):
    folder = Path(root) / name
    folder.mkdir(parents=True)
    if onnx:
        (folder / "ru_RU-denis-medium.onnx").write_bytes(b"onnx")
    if tokens:
        (folder / "tokens.txt").write_text("a 1\n", encoding="utf-8")
    if data:
        (folder / "espeak-ng-data").mkdir()
    return folder


class SherpaCase(unittest.TestCase):
    VOICE = "vits-piper-ru_RU-denis-medium-fp16"

    def setUp(self):
        FakeBackend.instances, FakeBackend.fail_on_create, FakeBackend.config_valid, FakeBackend.empty = [], None, True, False
        self.models = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT))
        patcher = patch("core.tts.sherpa._import_backend", return_value=FakeBackend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, voice=VOICE, player=None, **kwargs):
        engine = SherpaEngine(voice=voice, models_dir=self.models, player=player or FakePlayer(), **kwargs)
        self.addCleanup(engine._queue.wavs.cleanup)
        return engine


class TestSherpaLoading(SherpaCase):
    def test_config_comes_from_the_voice_folder_and_settings(self):
        folder = make_voice_folder(self.models, self.VOICE)
        engine = self.engine(threads=3)
        engine.load()
        self.assertEqual(engine.state, "ready")
        config = FakeBackend.instances[0].config
        model = config.kwargs["model"]
        self.assertEqual(model.kwargs["num_threads"], 3)
        self.assertEqual(model.kwargs["provider"], "cpu")
        vits = model.kwargs["vits"].kwargs
        self.assertEqual(vits["model"], str(folder / "ru_RU-denis-medium.onnx"))
        self.assertEqual(vits["tokens"], str(folder / "tokens.txt"))
        self.assertEqual(vits["data_dir"], str(folder / "espeak-ng-data"))
        self.assertEqual(config.kwargs["max_num_sentences"], 1)

    def test_default_threads_is_two(self):
        make_voice_folder(self.models, self.VOICE)
        engine = self.engine()
        engine.load()
        self.assertEqual(FakeBackend.instances[0].config.kwargs["model"].kwargs["num_threads"], 2)

    def test_warm_up_phrase_is_generated_and_not_played(self):
        make_voice_folder(self.models, self.VOICE)
        player = FakePlayer()
        engine = self.engine(player=player)
        engine.load()
        self.assertEqual([c[0] for c in FakeBackend.instances[0].calls], ["Привет."])
        self.assertEqual(player.played, [])
        self.assertIsNotNone(engine.warmup_rtf)
        self.assertGreater(engine.load_seconds, 0)

    def test_load_time_is_logged_without_text(self):
        make_voice_folder(self.models, self.VOICE)
        with self.assertLogs("core.tts.sherpa", level="INFO") as logs:
            self.engine().load()
        joined = "\n".join(logs.output)
        self.assertIn("загружен за", joined)
        self.assertIn("RTF", joined)
        self.assertNotIn("Привет", joined)

    def test_nothing_installed_means_not_installed(self):
        cases = (("", dict(), "голос не выбран"),
                 (self.VOICE, dict(), "папка голоса не найдена"),
                 (self.VOICE, dict(onnx=False), "нет файла модели"),
                 (self.VOICE, dict(tokens=False), "нет tokens.txt"),
                 (self.VOICE, dict(data=False), "нет espeak-ng-data"))
        for voice, folder_args, reason in cases:
            with self.subTest(reason=reason):
                models = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT))
                if folder_args or reason != "папка голоса не найдена" and voice:
                    make_voice_folder(models, self.VOICE, **folder_args)
                engine = SherpaEngine(voice=voice, models_dir=models, player=FakePlayer())
                with self.assertLogs("core.tts.sherpa", level="WARNING"):
                    engine.load()
                self.assertEqual(engine.state, "unavailable")
                self.assertEqual(engine.unavailable.kind, "not_installed")
                self.assertIn(reason, engine.unavailable.reason)

    def test_missing_package_is_not_installed(self):
        make_voice_folder(self.models, self.VOICE)
        with patch("core.tts.sherpa.importlib.import_module", side_effect=ImportError("нет")):
            with self.assertRaises(TTSUnavailable) as ctx:
                tests_base.REAL_IMPORT_BACKEND["core.tts.sherpa"]()
        self.assertIn("sherpa-onnx", ctx.exception.reason)
        self.assertEqual(ctx.exception.kind, "not_installed")

    def test_package_error_in_load_makes_the_engine_unavailable(self):
        make_voice_folder(self.models, self.VOICE)
        with patch("core.tts.sherpa._import_backend", side_effect=TTSUnavailable("пакет sherpa-onnx не установлен")):
            engine = self.engine()
            with self.assertLogs("core.tts.sherpa", level="WARNING"):
                engine.load()
        self.assertEqual((engine.state, engine.unavailable.kind), ("unavailable", "not_installed"))

    def test_broken_model_is_a_failure(self):
        make_voice_folder(self.models, self.VOICE)
        FakeBackend.fail_on_create = RuntimeError("битая модель")
        engine = self.engine()
        with self.assertLogs("core.tts.sherpa", level="WARNING"):
            engine.load()
        self.assertEqual((engine.state, engine.unavailable.kind), ("unavailable", "failed"))

    def test_config_that_fails_validation_is_a_failure(self):
        make_voice_folder(self.models, self.VOICE)
        FakeBackend.config_valid = False
        engine = self.engine()
        with self.assertLogs("core.tts.sherpa", level="WARNING"):
            engine.load()
        self.assertEqual((engine.state, engine.unavailable.kind), ("unavailable", "failed"))

    def test_never_downloads_or_writes_into_the_model_folder(self):
        folder = make_voice_folder(self.models, self.VOICE)
        before = sorted(p.name for p in folder.rglob("*"))
        engine = self.engine()
        engine.load()
        engine.speak("Тест.")
        self.assertEqual(sorted(p.name for p in folder.rglob("*")), before)
        empty = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT))
        broken = SherpaEngine(voice=self.VOICE, models_dir=empty, player=FakePlayer())
        with self.assertLogs("core.tts.sherpa", level="WARNING"):
            broken.load()
        self.assertEqual(list(empty.iterdir()), [])                                       # папку модели не создаём

    def test_background_loading_does_not_block(self):
        make_voice_folder(self.models, self.VOICE)
        engine = self.engine()
        gate = threading.Event()
        original = FakeBackend.OfflineTts.__init__

        def slow(self_inner, config):
            gate.wait(3)
            original(self_inner, config)
        with patch.object(FakeBackend.OfflineTts, "__init__", slow):
            engine.start_loading()
            self.assertEqual(engine.state, "loading")
            engine.start_loading()                                                         # второй вызов ничего не делает
            gate.set()
            self.assertTrue(wait_until(lambda: engine.state == "ready"))
        self.assertEqual(len(FakeBackend.instances), 1)


class TestSherpaSpeaking(SherpaCase):
    def ready(self, **kwargs):
        make_voice_folder(self.models, self.VOICE)
        engine = self.engine(**kwargs)
        engine.load()
        return engine

    def test_speaks_by_sentences_with_speed_and_sid(self):
        engine = self.ready(speed=1.2)
        result = engine.speak("Открыто приложение Telegram. Закрыто приложение Safari.")
        self.assertIsInstance(result, NeuralResult)
        self.assertEqual((result.done, len(result.sentences), result.error, result.stopped), (2, 2, False, False))
        calls = FakeBackend.instances[0].calls[1:]                                         # первый вызов — прогрев
        self.assertEqual([c[0] for c in calls], ["Открыто приложение Telegram.", "Закрыто приложение Safari."])
        self.assertTrue(all(c[1] == 0 and c[2] == 1.2 and c[3] == 0.2 for c in calls))
        self.assertEqual(result.tail, 0.0)

    def test_effect_speeds_the_model_up_and_adds_the_tail(self):
        engine = self.ready(speed=1.0, effect_on=True, pitch=-2.0, reverb=0.12)
        result = engine.speak("Один.")
        self.assertAlmostEqual(FakeBackend.instances[0].calls[-1][2], 1 / effect.pitch_ratio(-2), places=5)
        self.assertGreater(result.tail, 0.0)
        self.assertAlmostEqual(result.tail, engine.tail_seconds, places=9)
        self.assertLessEqual(result.tail, 0.15)

    def test_effect_off_keeps_the_user_speed_and_no_tail(self):
        engine = self.ready(speed=1.0, effect_on=False, pitch=-6.0)
        result = engine.speak("Один.")
        self.assertEqual(FakeBackend.instances[0].calls[-1][2], 1.0)
        self.assertEqual(result.tail, 0.0)

    def test_played_wavs_hold_the_effect_processed_audio(self):
        player = FakePlayer()
        engine = self.ready(player=player, effect_on=True, pitch=-4.0, reverb=0.2)
        engine.speak("Один.")
        self.assertEqual(len(player.played), 1)
        self.assertGreater(player.played[0]["size"], 44 + 2 * int(22050 * 0.1))           # длиннее исходного: растяжение и хвост

    def test_empty_text(self):
        engine = self.ready()
        self.assertEqual(engine.speak("   ").sentences, [])
        self.assertEqual(len(FakeBackend.instances[0].calls), 1)                             # только прогрев

    def test_empty_synthesis_is_an_error(self):
        engine = self.ready()
        FakeBackend.empty = True
        with self.assertLogs("core.tts.player", level="WARNING"):
            result = engine.speak("Один.")
        self.assertTrue(result.error)
        self.assertEqual(result.done, 0)

    def test_stop_interrupts_the_phrase(self):
        player = FakePlayer()
        player.gates[0] = threading.Event()
        engine = self.ready(player=player)
        results = []
        thread = threading.Thread(target=lambda: results.append(engine.speak("Раз. Два. Три.")))
        thread.start()
        self.assertTrue(wait_until(lambda: len(player.played) >= 1))
        engine.stop()
        thread.join(3)
        self.assertTrue(results[0].stopped)
        self.assertEqual(results[0].tail, 0.0 if results[0].done == 0 else results[0].tail)

    def test_model_speed_helper(self):
        self.assertEqual(SherpaEngine(speed=1.3).model_speed(), 1.3)
        self.assertAlmostEqual(SherpaEngine(speed=1.0, effect_on=True, pitch=-2.0).model_speed(), 1.1225, places=3)


# --- роутер: нейронный голос и откат на say -----------------------------------------------------------------------------
class FakeSay:
    def __init__(self):
        self.spoken, self.stopped = [], 0

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stopped += 1


class FakeNeural:
    name = "sherpa"

    def __init__(self, state="ready", result=None, error=None, unavailable=None):
        self.state, self.result, self.error, self.unavailable = state, result, error, unavailable
        self.spoken, self.stopped, self.started = [], 0, 0

    def start_loading(self):
        self.started += 1

    def speak(self, text):
        self.spoken.append(text)
        if self.error:
            raise self.error
        return self.result or NeuralResult(sentences=[text], done=1, tail=0.15)

    def stop(self):
        self.stopped += 1


class TestSpeechVoice(unittest.TestCase):
    def test_without_neural_everything_goes_to_say(self):
        say = FakeSay()
        voice = SpeechVoice(say)
        self.assertEqual(voice.speak("Привет."), 0.0)
        self.assertEqual(say.spoken, ["Привет."])
        self.assertIsNone(voice.take_notice())
        voice.start()                                                                      # ничего не грузит

    def test_ready_neural_voice_speaks_and_returns_its_tail(self):
        say, neural = FakeSay(), FakeNeural()
        voice = SpeechVoice(say, neural)
        self.assertEqual(voice.speak("Привет."), 0.15)
        self.assertEqual((neural.spoken, say.spoken), (["Привет."], []))

    def test_not_ready_means_say(self):
        for state in ("idle", "loading"):
            with self.subTest(state=state):
                say, neural = FakeSay(), FakeNeural(state=state)
                voice = SpeechVoice(say, neural)
                self.assertEqual(voice.speak("Привет."), 0.0)
                self.assertEqual((say.spoken, neural.spoken), (["Привет."], []))
                self.assertIsNone(voice.take_notice())                                     # ещё грузится — оповещения нет

    def test_unavailable_means_say(self):
        say, neural = FakeSay(), FakeNeural(state="unavailable", unavailable=TTSUnavailable("нет"))
        voice = SpeechVoice(say, neural)
        voice.speak("Привет.")
        self.assertEqual((say.spoken, neural.spoken), (["Привет."], []))

    def test_error_in_the_middle_finishes_with_say_without_repeats(self):
        say = FakeSay()
        result = NeuralResult(sentences=["Раз.", "Два.", "Три."], done=1, error=True)
        voice = SpeechVoice(say, FakeNeural(result=result))
        with self.assertLogs("core.tts.factory", level="WARNING"):
            voice.speak("Раз. Два. Три.")
        self.assertEqual(say.spoken, ["Два. Три."])                                         # «Раз.» уже прозвучало

    def test_error_on_the_first_sentence_says_everything_by_say(self):
        say = FakeSay()
        result = NeuralResult(sentences=["Раз.", "Два."], done=0, error=True)
        SpeechVoice(say, FakeNeural(result=result))
        voice = SpeechVoice(say, FakeNeural(result=result))
        with self.assertLogs("core.tts.factory", level="WARNING"):
            voice.speak("Раз. Два.")
        self.assertEqual(say.spoken, ["Раз. Два."])

    def test_exception_in_neural_falls_back_to_the_whole_phrase(self):
        say = FakeSay()
        voice = SpeechVoice(say, FakeNeural(error=RuntimeError("упал")))
        with self.assertLogs("core.tts.factory", level="WARNING") as logs:
            self.assertEqual(voice.speak("Привет."), 0.0)
        self.assertEqual(say.spoken, ["Привет."])
        self.assertNotIn("Привет", "\n".join(logs.output))

    def test_stopped_speech_is_not_repeated_by_say(self):
        say = FakeSay()
        result = NeuralResult(sentences=["Раз.", "Два."], done=1, error=True, stopped=True)
        voice = SpeechVoice(say, FakeNeural(result=result))
        voice.speak("Раз. Два.")
        self.assertEqual(say.spoken, [])

    def test_everything_said_means_no_say(self):
        say = FakeSay()
        result = NeuralResult(sentences=["Раз."], done=1)
        SpeechVoice(say, FakeNeural(result=result)).speak("Раз.")
        self.assertEqual(say.spoken, [])

    def test_notice_once_per_run(self):
        say = FakeSay()
        voice = SpeechVoice(say, FakeNeural(state="unavailable", unavailable=TTSUnavailable("нет пакета")))
        with self.assertLogs("core.tts.factory", level="WARNING") as logs:
            self.assertEqual(voice.take_notice(), "Нейронный голос не установлен, использую системный.")
        self.assertIn("нет пакета", logs.output[0])                                         # причина — в лог
        self.assertIsNone(voice.take_notice())
        self.assertIsNone(voice.take_notice())
        self.assertEqual(NOT_INSTALLED_NOTICE, "Нейронный голос не установлен, использую системный.")

    def test_notice_wording_for_a_failed_start(self):
        voice = SpeechVoice(FakeSay(), FakeNeural(state="unavailable", unavailable=TTSUnavailable("битая", kind="failed")))
        with self.assertLogs("core.tts.factory", level="WARNING"):
            self.assertEqual(voice.take_notice(), FAILED_NOTICE)

    def test_no_notice_when_ready_or_say_only(self):
        self.assertIsNone(SpeechVoice(FakeSay(), FakeNeural()).take_notice())
        self.assertIsNone(SpeechVoice(FakeSay()).take_notice())

    def test_start_loads_the_neural_voice_and_stop_stops_both(self):
        say, neural = FakeSay(), FakeNeural()
        voice = SpeechVoice(say, neural)
        voice.start()
        voice.stop()
        self.assertEqual((neural.started, neural.stopped, say.stopped), (1, 1, 1))


class TestFactory(unittest.TestCase):
    def test_say_by_default_loads_no_neural_part(self):
        with patch("core.settings.ENV_FILE", env_file("")), patch("core.tts.factory.build_neural") as build:
            voice = build_tts(FakeSay())
        build.assert_not_called()
        self.assertIsNone(voice.neural)
        self.assertNotIn("sherpa_onnx", sys.modules)

    def test_engine_say_never_imports_sherpa(self):
        with patch("core.settings.ENV_FILE", env_file("TTS_ENGINE=say\n")), patch("core.tts.factory.build_neural") as build:
            build_tts(FakeSay())
        build.assert_not_called()

    def test_sherpa_builds_the_neural_engine_from_settings(self):
        text = "TTS_ENGINE=sherpa\nTTS_VOICE=vits-x\nTTS_SPEED=1.2\nTTS_THREADS=2\nTTS_EFFECT=jarvis\nTTS_PITCH=-3\nTTS_REVERB=0.2\n"
        with patch("core.settings.ENV_FILE", env_file(text)):
            voice = build_tts(FakeSay())
        neural = voice.neural
        self.assertEqual((neural.voice, neural.speed, neural.threads, neural.effect_on, neural.pitch, neural.reverb),
                         ("vits-x", 1.2, 2, True, -3.0, 0.2))
        self.assertEqual(neural.state, "idle")                                            # создание ничего не грузит
        self.assertEqual(neural.models_dir, models_dir())

    def test_models_dir_is_inside_the_jarvis_folder(self):
        self.assertEqual(models_dir().parts[-2:], ("models", "tts"))

    def test_effect_is_disabled_without_numpy(self):
        with patch("core.settings.ENV_FILE", env_file("TTS_ENGINE=sherpa\nTTS_EFFECT=jarvis\n")), \
                patch("core.tts.effect.numpy_module", return_value=None):
            with self.assertLogs("core.tts.factory", level="WARNING") as logs:
                voice = build_tts(FakeSay())
        self.assertFalse(voice.neural.effect_on)
        self.assertIn("numpy недоступен", logs.output[0])

    def test_unknown_engine_is_say(self):
        with patch("core.settings.ENV_FILE", env_file("TTS_ENGINE=мусор\n")):
            with self.assertLogs("core.tts.base", level="WARNING"):
                voice = build_tts(FakeSay())
        self.assertIsNone(voice.neural)


# --- say как прежде ---------------------------------------------------------------------------------------------------
class TestSayEngine(unittest.TestCase):
    def test_command_puts_the_text_after_double_dash(self):
        self.assertEqual(say_command("-v Zarvox привет", "Milena"), ["say", "-v", "Milena", "--", "-v Zarvox привет"])
        self.assertEqual(say_command("привет", None), ["say", "--", "привет"])

    def test_speak_uses_popen_with_a_list_and_waits(self):
        with patch("core.tts.say.subprocess.Popen") as popen:
            SayEngine("Milena").speak("Привет")
        popen.assert_called_once_with(["say", "-v", "Milena", "--", "Привет"])
        popen.return_value.wait.assert_called_once()

    def test_stop_terminates_a_running_process(self):
        engine = SayEngine("Milena")
        engine._proc = MagicMock()
        engine._proc.poll.return_value = None
        engine.stop()
        engine._proc.terminate.assert_called_once()
        engine._proc.poll.return_value = 0
        engine.stop()
        engine._proc.terminate.assert_called_once()                                          # завершившийся не трогаем

    def test_oserror_is_logged_not_raised(self):
        with patch("core.tts.say.subprocess.Popen", side_effect=OSError("нет say")), self.assertLogs("core.tts.say", level="ERROR"):
            SayEngine().speak("Привет")

    def test_process_is_cleared_after_speech(self):
        engine = SayEngine()
        with patch("core.tts.say.subprocess.Popen"):
            engine.speak("Привет")
        self.assertIsNone(engine._proc)

    def test_pick_voice(self):
        out = "Alex en_US # x\nMilena ru_RU # Здравствуйте\nYuri ru_RU # x"
        self.assertEqual(pick_voice(out), "Milena")
        self.assertEqual(pick_voice("Yuri ru_RU # x"), "Yuri")
        self.assertIsNone(pick_voice("Alex en_US # x"))

    def test_engine_contract(self):
        self.assertTrue(SayEngine.local)
        self.assertEqual(SayEngine.name, "say")
        self.assertTrue(issubclass(SayEngine, TTSEngine))
        self.assertTrue(SherpaEngine.local)
        with self.assertRaises(NotImplementedError):
            TTSEngine().speak("x")


# --- интеграция в SpeechEngine ----------------------------------------------------------------------------------------
@patch("core.engine._detect_voice", return_value="Milena")
@patch("core.engine.sr.Microphone")
@patch("core.engine.subprocess.Popen")
class TestSpeechEngineIntegration(unittest.TestCase):
    def test_default_path_is_say_and_loads_nothing(self, popen, *_):
        engine = SpeechEngine()
        self.assertIsNone(engine.tts.neural)
        engine.speak("Привет")
        popen.assert_called_once_with(["say", "-v", "Milena", "--", "Привет"])
        self.assertNotIn("sherpa_onnx", sys.modules)

    def test_proc_property_delegates_to_say(self, *_):
        engine = SpeechEngine()
        marker = object()
        engine._proc = marker
        self.assertIs(engine.say._proc, marker)
        self.assertIs(engine._proc, marker)

    def test_neural_tail_is_added_to_the_end_of_speech(self, popen, *_):
        engine = SpeechEngine()
        engine.tts = SpeechVoice(engine.say, FakeNeural(result=NeuralResult(sentences=["x"], done=1, tail=0.15)))
        with patch("core.engine.time.monotonic", return_value=500.0):
            engine.speak("Привет")
        self.assertAlmostEqual(engine._last_speech_end, 500.15, places=6)
        self.assertAlmostEqual(engine.window_opened_at, 500.15, places=6)
        popen.assert_not_called()                                                           # say не понадобился
        self.assertFalse(engine.speaking.is_set())

    def test_tail_is_zero_for_say(self, popen, *_):
        engine = SpeechEngine()
        with patch("core.engine.time.monotonic", return_value=500.0):
            engine.speak("Привет")
        self.assertEqual(engine._last_speech_end, 500.0)

    def test_tail_remaining_counts_the_tail(self, *_):
        engine = SpeechEngine()
        engine.tail_pause = 0.8
        engine._last_speech_end = 100.15
        with patch("core.engine.time.monotonic", return_value=100.0):
            self.assertAlmostEqual(engine.tail_remaining(), 0.95)

    def test_stop_speaking_stops_the_voice_and_bumps_the_generation(self, *_):
        engine = SpeechEngine()
        engine.tts = MagicMock()
        before = engine._stop_generation
        engine.stop_speaking()
        engine.tts.stop.assert_called_once()
        self.assertEqual(engine._stop_generation, before + 1)

    def test_start_stt_also_starts_the_voice(self, *_):
        engine = SpeechEngine()
        engine.tts = MagicMock()
        with patch.object(engine.stt, "start") as stt_start:
            engine.start_stt()
        stt_start.assert_called_once()
        engine.tts.start.assert_called_once()

    def test_notice_is_said_by_say_once_before_the_phrase(self, popen, *_):
        engine = SpeechEngine()
        neural = FakeNeural(state="unavailable", unavailable=TTSUnavailable("нет пакета"))
        engine.tts = SpeechVoice(engine.say, neural)
        with self.assertLogs("core.tts.factory", level="WARNING"):
            engine.speak("Первая фраза")
        engine.speak("Вторая фраза")
        spoken = [call.args[0][-1] for call in popen.call_args_list]
        self.assertEqual(spoken, ["Нейронный голос не установлен, использую системный.", "Первая фраза", "Вторая фраза"])
        self.assertEqual(neural.spoken, [])                                                  # нейронным движком не озвучивается
        self.assertIn("Нейронный голос не установлен, использую системный.", engine.recent_spoken)   # защита от эха
        self.assertEqual(spoken.count("Нейронный голос не установлен, использую системный."), 1)

    def test_echo_protection_still_knows_the_phrase(self, *_):
        engine = SpeechEngine()
        engine.tts = SpeechVoice(engine.say, FakeNeural())
        engine.speak("Открыто приложение Telegram.")
        self.assertTrue(engine.is_echo("открыто приложение telegram"))

    def test_window_is_not_opened_by_a_notice_phrase_but_by_an_answer(self, *_):
        engine = SpeechEngine()
        engine.tts = SpeechVoice(engine.say, FakeNeural())
        engine.speak("Секунду.", opens_window=False)
        self.assertIsNone(engine.window_opened_at)
        engine.speak("Готово.")
        self.assertIsNotNone(engine.window_opened_at)

    def test_the_lock_is_still_shared(self, popen, *_):
        engine = SpeechEngine()
        active, overlaps = [], []

        class Neural(FakeNeural):
            def speak(self_inner, text):
                active.append(1)
                if len(active) > 1:
                    overlaps.append(1)
                time.sleep(0.05)
                active.pop()
                return NeuralResult(sentences=[text], done=1)
        engine.tts = SpeechVoice(engine.say, Neural())
        threads = [threading.Thread(target=engine.speak, args=(f"Фраза {i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        self.assertEqual(overlaps, [])

    def test_stop_during_neural_speech_does_not_open_the_window(self, *_):
        engine = SpeechEngine()

        class Neural(FakeNeural):
            def speak(self_inner, text):
                engine.stop_speaking()
                return NeuralResult(sentences=[text], done=0, stopped=True)
        engine.tts = SpeechVoice(engine.say, Neural())
        engine.speak("Длинный ответ.")
        self.assertIsNone(engine.window_opened_at)

    def test_speech_log_keeps_the_existing_assistant_line(self, *_):
        engine = SpeechEngine()
        with self.assertLogs("core.engine", level="INFO") as logs:
            engine.speak("Привет")
        self.assertIn("🗣 Assistant: Привет", logs.output[0])


# --- core.tts.check ---------------------------------------------------------------------------------------------------
class FakeCheckEngine:
    def __init__(self, state="ready", reason="нет файлов"):
        self.state = state
        self.unavailable = TTSUnavailable(reason) if state != "ready" else None
        self.warmup_rtf, self.folder, self.loaded, self.spoken = 0.21, None, 0, []

    def load(self):
        self.loaded += 1

    def _synthesize(self, sentence):
        return [0.1] * 2205, 22050

    def speak(self, text):
        self.spoken.append(text)
        return NeuralResult(sentences=[text], done=1)


class TestCheckTool(unittest.TestCase):
    def run_check(self, *args, engine=None, env=""):
        engine = engine or FakeCheckEngine()
        lines = []
        tmp = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / "tts_samples"
        with patch("core.settings.ENV_FILE", env_file(env)), patch("core.tts.check.build_neural", return_value=engine) as build, \
                patch("core.tts.check.samples_dir", return_value=tmp), \
                patch("core.tts.check.importlib.util.find_spec", return_value=None), \
                patch("core.tts.check.AfplayPlayer") as player:
            code = check.main(list(args), out=lines.append)
        return code, "\n".join(lines), engine, tmp, build, player

    def test_plain_run_loads_and_measures_and_writes_nothing(self):
        code, text, engine, tmp, build, player = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(engine.loaded, 1)
        for part in ("Движок в .env: say", "sherpa-onnx: НЕ установлен", "Голос загружен и прогрет", "Первая фраза: синтез", "RTF"):
            self.assertIn(part, text)
        self.assertFalse(tmp.exists())
        self.assertEqual(engine.spoken, [])

    def test_not_ready_reports_the_reason_and_fails(self):
        code, text, *_ = self.run_check(engine=FakeCheckEngine(state="unavailable", reason="папка голоса не найдена: /x"))
        self.assertEqual(code, 1)
        self.assertIn("Не загружено: папка голоса не найдена", text)

    def test_samples_writes_ten_private_wavs_and_plays_nothing(self):
        code, text, engine, tmp, _, player = self.run_check("--samples")
        self.assertEqual(code, 0)
        files = sorted(p.name for p in tmp.iterdir())
        self.assertEqual(len(files), 10)
        self.assertEqual(files, sorted(f"{name}.wav" for name, _ in check.SAMPLE_PHRASES))
        self.assertEqual(stat.S_IMODE(os.stat(tmp).st_mode), 0o700)
        for name in files:
            self.assertEqual(stat.S_IMODE(os.stat(tmp / name).st_mode), 0o600)
        self.assertEqual(engine.spoken, [])
        player.return_value.play.assert_not_called()
        self.assertIn("RTF по фразам: медиана", text)

    def test_samples_do_not_leave_temp_files(self):
        with patch("core.tts.player.TempWavs.cleanup", autospec=True, side_effect=TempWavs.cleanup) as cleanup:
            self.run_check("--samples")
        self.assertGreaterEqual(cleanup.call_count, 10)

    def test_the_ten_phrases(self):
        texts = dict(check.SAMPLE_PHRASES)
        self.assertEqual(len(check.SAMPLE_PHRASES), 10)
        self.assertIn("Открыто приложение Telegram.", texts.values())
        self.assertIn("Сейчас вторник, 27 октября, 17:12.", texts.values())
        self.assertIn("Слушаю.", texts.values())
        self.assertIn("Не получилось: файл не найден.", texts.values())
        self.assertIn("Это слишком опасно, я этого не сделаю.", texts.values())
        self.assertTrue(any(t.startswith("Текст мог распознаться неточно") for t in texts.values()))
        self.assertTrue(any(len(split_sentences(t)) == 3 for t in texts.values()))        # длинная фраза в три предложения

    def test_effect_flag_turns_the_effect_on(self):
        _, _, _, _, build, _ = self.run_check("--effect")
        self.assertTrue(build.call_args.args[0].effect)
        _, _, _, _, build, _ = self.run_check()
        self.assertFalse(build.call_args.args[0].effect)

    def test_play_uses_the_engine_player_only_when_asked(self):
        _, text, engine, *_ = self.run_check("--play")
        self.assertEqual(len(engine.spoken), 10)
        _, _, engine, *_ = self.run_check("--samples")
        self.assertEqual(engine.spoken, [])

    def test_the_tool_never_touches_the_real_samples_folder(self):
        self.assertEqual(check.SAMPLES_DIR_NAME, "tts_samples")
        self.assertTrue(str(check.samples_dir()).startswith(str(tests_base.TEMP_ROOT)))     # DATA_DIR в тестах — временная папка


if __name__ == "__main__":
    unittest.main()
