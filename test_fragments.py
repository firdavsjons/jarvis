"""Обрывки речи: модель не спрашиваем, просим повторить. Ответы и управление обрывками не считаются."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import installed_apps
from core.ai import AIClient
from core.cache import CommandCache
from core.fragments import FRAGMENT_REPLY, is_fragment
from fixtures_apps import patch_installed
from test_tools import ScriptedLLM, call, final, tools_reply
import main

_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()
    installed_apps.set_user_aliases({})


class TestIsFragment(unittest.TestCase):
    def test_fragments(self):
        for text in ["то", "То.", "скрыть бо", "бо", "что то"]:
            with self.subTest(text=text):
                self.assertTrue(is_fragment(text))

    def test_control_words_are_not_fragments(self):
        for text in ["да", "нет", "стоп", "хватит", "ок", "окей", "отмена", "не надо", "ладно", "пока", "привет",
                     "Да.", "Нет!", "Не надо, спасибо"]:
            with self.subTest(text=text):
                self.assertFalse(is_fragment(text))

    def test_known_single_commands_are_not_fragments(self):
        for text in ["пауза", "громче", "тише", "скриншот", "заряд", "Mail", "Safari", "ip", "50"]:
            with self.subTest(text=text):
                self.assertFalse(is_fragment(text))

    def test_user_alias_is_known(self):
        installed_apps.set_user_aliases({"бр": "Google Chrome"})
        self.addCleanup(installed_apps.set_user_aliases, {})
        self.assertFalse(is_fragment("бр"))

    def test_real_questions_go_to_model(self):
        for text in ["кто ты", "что играет", "фазы луны", "погода душанбе", "расскажи анекдот", "кто такой Пушкин",
                     "абракадабра", "открой сафари и закрой почту"]:
            with self.subTest(text=text):
                self.assertFalse(is_fragment(text))

    def test_empty(self):
        self.assertFalse(is_fragment(""))
        self.assertFalse(is_fragment("   "))


class TestRespond(unittest.TestCase):
    def test_fragment_does_not_call_model(self):
        for text in ["то", "скрыть бо"]:
            llm = ScriptedLLM(final("не должно прозвучать"))
            client = AIClient(llm=llm)
            turn = client.respond(text)
            self.assertEqual(turn.speech, "Не расслышал. Повторите, пожалуйста.")
            self.assertEqual(turn.speech, FRAGMENT_REPLY)
            self.assertEqual(llm.requests, [])
            self.assertEqual(client.history.messages(), [])   # в историю обрывок не пишем

    def test_control_words_reach_model(self):
        for text in ["да", "нет", "стоп", "хватит", "ок", "окей", "отмена", "не надо", "ладно", "пока", "привет"]:
            with self.subTest(text=text):
                llm = ScriptedLLM(final("Хорошо."))
                self.assertEqual(AIClient(llm=llm).respond(text).speech, "Хорошо.")
                self.assertEqual(len(llm.requests), 1)

    def test_cached_command_wins_over_fragment_rule(self):
        cache = CommandCache()
        cache.store("вк", [{"name": "open_url", "args": {"url": "https://youtube.com"}}])
        with patch("core.ai.subprocess.run") as run:
            run.return_value.returncode = 0
            llm = ScriptedLLM()
            turn = AIClient(llm=llm, cache=cache).respond("вк")
        self.assertEqual(llm.requests, [])
        self.assertNotEqual(turn.speech, FRAGMENT_REPLY)

    def test_known_fast_commands_untouched_by_main(self):
        # «пауза» и «скриншот» обрабатывает MacControl раньше respond
        app = MagicMock()
        app.has_pending.return_value = False
        app.mac.execute.return_value = "Пауза."
        main.VoiceApp.process_command(app, "пауза")
        app.engine.speak.assert_called_once_with("Пауза.")
        app.ai.respond.assert_not_called()


class TestPendingConfirmation(unittest.TestCase):
    def test_yes_with_pending_action_is_not_a_fragment(self):
        """Пока ждём подтверждения, main отдаёт фразу в handle_confirmation и respond не вызывает."""
        app = MagicMock()
        app.has_pending.return_value = True
        main.VoiceApp.process_command(app, "да")
        app.handle_confirmation.assert_called_once_with("да")
        app.ai.respond.assert_not_called()
        app.engine.speak.assert_not_called()

    def test_short_answers_with_pending(self):
        for text in ["нет", "то"]:   # «то» тоже ответ на вопрос: handle_confirmation скажет «Скажите да или нет»
            app = MagicMock()
            app.has_pending.return_value = True
            main.VoiceApp.process_command(app, text)
            app.handle_confirmation.assert_called_once_with(text)
            app.ai.respond.assert_not_called()


if __name__ == "__main__":
    unittest.main()
