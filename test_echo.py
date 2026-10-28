"""Эхо ассистента: пауза после озвучки (SPEECH_TAIL_PAUSE) и отбрасывание фраз, похожих на недавнюю речь."""
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

import main
from core.engine import SpeechEngine, speech_tail_pause


@patch('core.engine._detect_voice', return_value="Milena")
@patch('core.engine.sr.Microphone')
@patch('core.engine.subprocess.Popen')
class TestEchoFilter(unittest.TestCase):
    def engine(self):
        engine = SpeechEngine()
        return engine

    def say(self, engine, *phrases):
        for p in phrases:
            engine.speak(p)

    def test_similar_by_ratio_is_echo(self, *_):
        e = self.engine()
        self.say(e, "Подтвердите отправку сообщения привет себе в Telegram.")
        for heard in ("подтвердите отправку сообщения привет себе в telegram", "подтвердите отправку сообщения бэк слышь привет себе",
                      "Подтвердите отправку сообщения"):
            with self.subTest(heard=heard):
                self.assertTrue(e.is_echo(heard))

    def test_share_of_words_is_echo(self, *_):
        e = self.engine()
        self.say(e, "Кому отправить сообщение привет в Telegram, укажите получателя")
        self.assertTrue(e.is_echo("укажите получателя кому отправить"))               # ≥60% слов из фразы ассистента
        self.assertFalse(e.is_echo("открой сафари и закрой почту"))

    def test_last_three_phrases_only(self, *_):
        e = self.engine()
        self.say(e, "Первая фраза про погоду в Душанбе", "Вторая фраза про курс доллара", "Третья фраза про таймер на пять минут",
                 "Четвёртая фраза про заметки")
        self.assertFalse(e.is_echo("первая фраза про погоду в душанбе"))               # вытеснена: помним 3
        self.assertTrue(e.is_echo("вторая фраза про курс доллара"))
        self.assertTrue(e.is_echo("четвёртая фраза про заметки"))

    def test_answers_are_never_echo(self, *_):
        e = self.engine()
        self.say(e, "да", "нет", "отмена", "стоп")
        for heard in ("да", "нет", "отмена", "стоп", "Да.", "нет, отмена", "да нет стоп"):
            self.assertFalse(e.is_echo(heard), heard)
        self.say(e, "Скажите да или нет")
        self.assertFalse(e.is_echo("да"))

    def test_empty_history_and_ordinary_commands(self, *_):
        e = self.engine()
        self.assertFalse(e.is_echo("поставь таймер на пять минут"))
        self.say(e, "Открыто приложение Safari.")
        self.assertFalse(e.is_echo("открой сафари"))
        self.assertFalse(e.is_echo(""))
        self.assertFalse(e.is_echo(None))

    def test_short_phrase_uses_ratio_only(self, *_):
        e = self.engine()
        self.say(e, "Закрыто приложение Safari, громкость пятьдесят")
        self.assertFalse(e.is_echo("приложение"))                    # 1–2 слова: правило долей не применяется
        self.assertTrue(e.is_echo("закрыто приложение safari громкость пятьдесят"))

    def test_spoken_text_is_what_is_remembered(self, *_):
        e = self.engine()
        e.speak("Громкость 50.")
        self.assertTrue(e.is_echo("громкость пятьдесят"))             # распознавание отдаёт слова, не цифры

    def test_empty_speech_not_remembered(self, *_):
        e = self.engine()
        e.speak("   ")
        self.assertEqual(list(e.recent_spoken), [])


@patch('core.engine._detect_voice', return_value="Milena")
@patch('core.engine.sr.Microphone')
class TestTailPause(unittest.TestCase):
    def test_setting_from_env(self, *_):
        self.assertEqual(speech_tail_pause(), 0.8)                  # по умолчанию (в .env.example тоже 0.8)
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, ".env")
            for raw, expected in [("1.5", 1.5), ("abc", 0.8), ("-3", 0.0), ("99", 5.0), ("0", 0.0)]:
                with open(env, "w", encoding="utf-8") as f:
                    f.write(f"SPEECH_TAIL_PAUSE={raw}\n")
                with patch('core.settings.ENV_FILE', env):
                    self.assertEqual(SpeechEngine().tail_pause, expected, raw)
        text = open(os.path.join(os.path.dirname(__file__), ".env.example"), encoding="utf-8").read()
        self.assertIn("SPEECH_TAIL_PAUSE=0.8", text)

    def test_tail_remaining(self, *_):
        e = SpeechEngine()
        e.tail_pause = 0.8
        e._last_speech_end = 100.0
        with patch("core.engine.time.monotonic", return_value=100.3):
            self.assertAlmostEqual(e.tail_remaining(), 0.5)
        with patch("core.engine.time.monotonic", return_value=101.0):
            self.assertEqual(e.tail_remaining(), 0.0)
        e._last_speech_end = 0.0                                      # ещё ничего не говорили
        self.assertEqual(e.tail_remaining(), 0.0)


class TestLoop(unittest.TestCase):
    """background_loop: хвост речи не слушается; эхо отбрасывается с фиксированной строкой в логе."""

    def make_app(self, listens, tail=(0.0,), echo=(False,)):
        app = MagicMock()
        app.stop_event = threading.Event()
        app.engine.speaking.is_set.return_value = False
        app.engine.tail_remaining.side_effect = list(tail) + [0.0] * 20
        app.engine.speech_overlaps.return_value = False
        app.engine.is_echo.side_effect = list(echo) + [False] * 20
        texts = list(listens)

        def listen():
            if not texts:
                app.stop_event.set()
                return None
            return texts.pop(0)
        app.engine.listen.side_effect = listen
        app.is_listening = True
        return app

    def test_echo_dropped_and_logged_without_text(self):
        app = self.make_app(["подтвердите отправку сообщения привет"], echo=(True,))
        with self.assertLogs("main", level="INFO") as logs:
            main.VoiceApp.background_loop(app)
        self.assertIn("INFO:main:Отброшено (эхо ассистента)", logs.output)
        self.assertNotIn("привет", "\n".join(logs.output))
        app.process_command.assert_not_called()

    def test_real_command_goes_through(self):
        app = self.make_app(["поставь таймер на пять минут"], echo=(False,))
        main.VoiceApp.background_loop(app)
        app.process_command.assert_called_once_with("поставь таймер на пять минут", source="voice")

    def test_notices_flushed_after_checks_and_before_command(self):
        app = self.make_app(["открой сафари"], echo=(False,))
        order = []
        app.engine.flush_notices.side_effect = lambda: order.append("notices")
        app.process_command.side_effect = lambda *a, **k: order.append("command")
        main.VoiceApp.background_loop(app)
        self.assertEqual(order[:2], ["notices", "command"])

    def test_notices_flushed_when_nothing_was_recognized(self):
        app = self.make_app([None, "привет"])
        main.VoiceApp.background_loop(app)
        self.assertGreaterEqual(app.engine.flush_notices.call_count, 1)

    def test_answers_reach_command_processing(self):
        app = self.make_app(["да"], echo=(False,))
        main.VoiceApp.background_loop(app)
        app.process_command.assert_called_once_with("да", source="voice")

    def test_does_not_listen_during_tail_pause(self):
        app = self.make_app(["привет"], tail=(0.5, 0.4, 0.0))
        with patch("main.time.sleep") as sleep:
            main.VoiceApp.background_loop(app)
        self.assertGreaterEqual(sleep.call_count, 2)                   # ждали хвост, а не слушали
        # listen() первый раз вызван только после того, как хвост кончился
        self.assertEqual(app.engine.listen.call_count, 2)
        app.process_command.assert_called_once()


if __name__ == "__main__":
    unittest.main()
