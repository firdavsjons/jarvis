"""Этап 7.1.1, часть 1: голые «да» / «нет» / «отмена» без ожидающего действия."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

import main
from core.ai import AIClient
from core.fragments import NOTHING_TO_CONFIRM, asks_question, is_bare_confirmation
from fixtures_apps import patch_installed
from fixtures_run import isolated_apps
from modules.mac_control import MacControl
from test_tools import ScriptedLLM, call, final, tools_reply

BARE = ["да", "нет", "подтверждаю", "выполняй", "отмена", "Да!", "Нет, отмена", "да да", "ДА."]


class TestPredicates(unittest.TestCase):
    def test_bare_words(self):
        for text in BARE:
            self.assertTrue(is_bare_confirmation(text), text)
        for text in ["", "   ", "да пожалуйста", "не надо", "стоп", "ок", "да, открой сафари", "нет такого", "привет"]:
            self.assertFalse(is_bare_confirmation(text), text)

    def test_question_detection(self):
        for text in ["Поставить внутренний будильник?", "Кому именно: мама, Мария?", "Отправить «привет»?»",
                     "Хотите? ", "Что-то (да?)"]:
            self.assertTrue(asks_question(text), text)
        for text in ["Готово.", "", None, "Что? Хорошо.", "Вопрос? Нет, ответ."]:
            self.assertFalse(asks_question(text), text)


class ConfirmCase(unittest.TestCase):
    def setUp(self):
        isolated_apps(self, {})
        installed = patch_installed()
        installed.start()
        self.addCleanup(installed.stop)


class TestRespond(ConfirmCase):
    def test_bare_after_answer_without_question_is_not_sent_to_model(self):
        for text in BARE:
            with self.subTest(text=text):
                llm = ScriptedLLM(final("Хорошо."))
                client = AIClient(llm=llm)
                client.respond("привет, как дела")                      # ответ «Хорошо.» — без вопроса
                before = len(llm.requests)
                turn = client.respond(text)
                self.assertEqual(turn.speech, "Сейчас нечего подтверждать.")
                self.assertEqual(turn.speech, NOTHING_TO_CONFIRM)
                self.assertEqual(len(llm.requests), before)             # модель не вызывалась
                self.assertEqual(turn.results, [])                      # инструменты не запускались

    def test_bare_on_fresh_client(self):
        llm = ScriptedLLM(final("не должно прозвучать"))
        turn = AIClient(llm=llm).respond("да")
        self.assertEqual(turn.speech, "Сейчас нечего подтверждать.")
        self.assertEqual(llm.requests, [])

    def test_bare_after_question_goes_to_model_with_history(self):
        llm = ScriptedLLM(final("Поставить внутренний будильник?"), final("Поставил внутренний будильник."))
        client = AIClient(llm=llm)
        client.respond("поставь будильник в часах")
        turn = client.respond("да")
        self.assertEqual(turn.speech, "Поставил внутренний будильник.")
        self.assertEqual(len(llm.requests), 2)
        roles = [(m["role"], m["content"]) for m in llm.requests[1]["messages"][1:]]
        self.assertEqual(roles, [("user", "поставь будильник в часах"),
                                 ("assistant", "Поставить внутренний будильник?"), ("user", "да")])

    def test_every_bare_word_after_question_goes_to_model(self):
        for text in BARE:
            with self.subTest(text=text):
                llm = ScriptedLLM(final("Хотите продолжить?"), final("Понял."))
                client = AIClient(llm=llm)
                client.respond("расскажи что-нибудь")
                self.assertEqual(client.respond(text).speech, "Понял.")
                self.assertEqual(len(llm.requests), 2)

    def test_flag_resets_after_the_next_user_phrase(self):
        llm = ScriptedLLM(final("Поставить будильник?"), final("Понял."), final("Привет!"), final("Хорошо."))
        client = AIClient(llm=llm)
        client.respond("что-то про будильник")           # вопрос
        client.respond("нет")                            # ушло модели («Понял.» — не вопрос)
        self.assertEqual(client.respond("да").speech, "Сейчас нечего подтверждать.")
        self.assertEqual(len(llm.requests), 2)

    def test_flag_reset_by_unrelated_phrase_in_between(self):
        llm = ScriptedLLM(final("Поставить будильник?"), final("Привет!"))
        client = AIClient(llm=llm)
        client.respond("будильник")
        client.respond("привет")                         # другая реплика — вопрос больше не ждёт ответа
        self.assertEqual(client.respond("да").speech, "Сейчас нечего подтверждать.")
        self.assertEqual(len(llm.requests), 2)

    def test_second_question_keeps_working(self):
        llm = ScriptedLLM(final("Первый вопрос?"), final("А второй?"), final("Всё."))
        client = AIClient(llm=llm)
        client.respond("начни")
        client.respond("да")
        self.assertEqual(client.respond("нет").speech, "Всё.")
        self.assertEqual(len(llm.requests), 3)

    def test_non_bare_words_still_reach_model(self):
        for text in ["стоп", "хватит", "ок", "окей", "ладно", "пока", "привет", "не надо", "да пожалуйста"]:
            with self.subTest(text=text):
                llm = ScriptedLLM(final("Хорошо."))
                self.assertEqual(AIClient(llm=llm).respond(text).speech, "Хорошо.")

    def test_bare_does_not_touch_history(self):
        client = AIClient(llm=ScriptedLLM(final("Хорошо.")))
        client.respond("привет, как дела")
        before = client.history.messages()
        client.respond("да")
        self.assertEqual(client.history.messages(), before)

    def test_question_from_tool_failure_counts(self):
        """Вопрос ассистента может прийти и из инструмента («Кому именно?»), не только из текста модели."""
        client = AIClient(llm=ScriptedLLM(tools_reply(call("telegram", {"action": "send", "to": "Иван", "text": "привет"})),
                                          final("Понял.")))
        turn = client.respond("напиши ивану в телеграм привет")
        self.assertTrue(turn.speech.endswith("?"), turn.speech)
        self.assertEqual(client.respond("нет").speech, "Понял.")


class TestMain(ConfirmCase):
    def make_app(self, llm):
        app = MagicMock()
        app.ai = AIClient(llm=llm)
        app.mac = MacControl()
        app.has_pending.return_value = False
        app.pending_is_private.return_value = False
        app.deliver = lambda turn, text: main.VoiceApp.deliver(app, turn, text)
        app.report_result = lambda results: main.VoiceApp.report_result(app, results)
        app.ask_confirmation = lambda pending, phrase: main.VoiceApp.ask_confirmation(app, pending, phrase)
        return app

    @staticmethod
    def spoken(app):
        return [c.args[0] for c in app.engine.speak.call_args_list]

    def test_bare_without_pending_says_nothing_to_confirm(self):
        llm = ScriptedLLM(final("Хорошо."))
        app = self.make_app(llm)
        main.VoiceApp.process_command(app, "привет, как дела")
        app.engine.speak.reset_mock()
        before = len(llm.requests)
        for text in ("да", "нет", "отмена", "подтверждаю", "выполняй"):
            main.VoiceApp.process_command(app, text)
        self.assertEqual(self.spoken(app), ["Сейчас нечего подтверждать."] * 5)
        self.assertEqual(len(llm.requests), before)
        app.set_idle_status.assert_called()

    def test_bare_after_question_in_main_goes_to_model(self):
        llm = ScriptedLLM(final("Поставить внутренний будильник?"), final("Поставил."))
        app = self.make_app(llm)
        main.VoiceApp.process_command(app, "будильник в часах на 7")
        app.engine.speak.reset_mock()
        main.VoiceApp.process_command(app, "да")
        self.assertEqual(self.spoken(app), ["Поставил."])
        self.assertEqual(len(llm.requests), 2)

    def test_pending_action_still_gets_ordinary_confirmation(self):
        app = self.make_app(ScriptedLLM(tools_reply(call("quit_app", {"app": "Terminal"}))))
        main.VoiceApp.process_command(app, "закрой терминал")
        pending = app.set_pending.call_args.args[0]
        self.assertEqual(self.spoken(app), ["Закрыть Terminal?"])
        app.has_pending.return_value = True
        app.take_pending.return_value = (pending, "закрой терминал")
        app.engine.speak.reset_mock()
        with unittest_patch("core.ai.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            main.VoiceApp.process_command(app, "да")
        app.handle_confirmation.assert_called_once_with("да")     # настоящий обработчик подтверждения

    def test_real_handle_confirmation_runs_the_action(self):
        app = self.make_app(ScriptedLLM(tools_reply(call("quit_app", {"app": "Terminal"}))))
        main.VoiceApp.process_command(app, "закрой терминал")
        pending = app.set_pending.call_args.args[0]
        app.take_pending.return_value = (pending, "закрой терминал")
        app.engine.speak.reset_mock()
        with unittest_patch("core.ai.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            main.VoiceApp.handle_confirmation(app, "да")
        self.assertEqual(self.spoken(app), ["Выполняю.", "Готово."])
        self.assertNotIn("Сейчас нечего подтверждать.", self.spoken(app))

    def test_bare_while_pending_is_never_nothing_to_confirm(self):
        app = self.make_app(ScriptedLLM(final("не должно прозвучать")))
        app.has_pending.return_value = True
        for text in ("да", "нет", "отмена"):
            main.VoiceApp.process_command(app, text)
        self.assertEqual(app.handle_confirmation.call_count, 3)
        app.engine.speak.assert_not_called()


def unittest_patch(target):
    from unittest.mock import patch
    return patch(target)


if __name__ == "__main__":
    unittest.main()
