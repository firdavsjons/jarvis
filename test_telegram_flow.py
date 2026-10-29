"""Этап 7.1, часть 2: быстрые правила, цепочка main (вопрос → «да» / «нет» / таймаут), журнал и лог без текста,
чужие данные, бюджет схем. subprocess замокан (tests_base)."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

import main
from core import journal, topics
from core.ai import GENERIC_REFUSAL_REPLY, AIClient
from core.apps import registry
from core.apps import telegram as tg
from core.tools import TOOLS
from modules.mac_control import MacControl
from test_telegram import FakeMac, TelegramCase, call, final, tools_reply, ScriptedLLM

MESSAGE = "это личный текст сообщения номер один"


class FlowCase(TelegramCase):
    def make_app(self, client=None):
        app = MagicMock()
        app.ai = client or AIClient(llm=ScriptedLLM(final("Хорошо.")))
        app.mac = MacControl()
        app.has_pending.return_value = False
        app.report_result = lambda results: main.VoiceApp.report_result(app, results)
        app.deliver = lambda turn, text: main.VoiceApp.deliver(app, turn, text)
        app.ask_confirmation = lambda pending, phrase: main.VoiceApp.ask_confirmation(app, pending, phrase)
        return app

    @staticmethod
    def spoken(app):
        return [c.args[0] for c in app.engine.speak.call_args_list]


class TestQuickRules(FlowCase):
    def parse(self, phrase):
        found = registry.quick_actions(phrase)
        return None if found is None else found[1]

    def test_send_phrases(self):
        cases = {
            "напиши маме в телеграм привет": ("мам", "привет"),
            "напиши маме в телеграм привет, как дела?": ("мам", "привет, как дела?"),
            "Отправь маме в телеграм Скоро буду": ("мам", "Скоро буду"),
            "отправь Анне в телеграм ок": ("Анн", "ок"),
            "напиши маме в телеграме что буду поздно": ("мам", "буду поздно"),
            "отправь маме в телеграме что всё хорошо": ("мам", "всё хорошо"),
            "напиши, пожалуйста, маме в телеграм привет": None,         # лишние слова — отдаём модели
            "напиши Ивану Петрову в телеграм привет": ("Ивану Петрову", "привет"),
            "напиши себе в телеграм проверка связи": ("себе", "проверка связи"),
            "напишите маме в телеграм привет": ("мам", "привет"),
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                args = self.parse(phrase)
                if expected is None:
                    self.assertIsNone(args)
                else:
                    self.assertEqual(args["action"], "send")
                    self.assertTrue(args["to"].startswith(expected[0]) or args["to"] == expected[0], args)
                    self.assertEqual(args["text"], expected[1])

    def test_open_chat_phrases(self):
        for phrase, name in {"открой чат с мамой": "мамой", "Открой чат с Анной": "Анной",
                             "открой чат с Иваном Петровым": "Иваном Петровым", "покажи чат с мамой": "мамой"}.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(self.parse(phrase), {"action": "open_chat", "to": name, "text": ""})

    def test_uncertain_goes_to_model(self):
        for phrase in ["напиши привет в телеграм", "напиши маме в телеграм", "отправь маме в телеграм ",
                       "напиши маме привет", "напиши незнакомцу в телеграм привет", "напиши Ивану в телеграм привет",
                       "открой чат с незнакомцем", "открой чат", "что написать маме в телеграм",
                       "напиши Ане в телеграм привет"]:       # «Ане» — короткое имя в падеже: не угадываем
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.parse(phrase))

    def test_send_quick_rule_is_not_executed_by_mac_control(self):
        self.assertIsNone(MacControl().execute("напиши маме в телеграм привет"))
        self.assertEqual(self.mac.events, [])

    def test_open_chat_quick_rule_runs_without_model(self):
        self.assertEqual(MacControl().execute("открой чат с мамой"), "Открыл чат с Мама.")
        self.assertEqual(self.mac.names(), ["open", "wait"])

    def test_quick_send_always_asks_confirmation(self):
        client = AIClient(llm=ScriptedLLM())
        turn = client.quick_confirm("напиши маме в телеграм привет")
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(confirm_text(turn), "Отправить Мама сообщение: «привет»?")
        self.assertEqual(client.llm.requests, [])                  # без модели
        self.assertNotIn("enter", self.mac.names())
        self.assertEqual(turn.pending[0]["args"], {"action": "send", "to": "маме", "text": "привет"})

    def test_quick_confirm_ignores_other_phrases(self):
        client = AIClient(llm=ScriptedLLM())
        for phrase in ["открой чат с мамой", "какая погода", "поставь таймер на пять минут", "напиши маме привет"]:
            self.assertIsNone(client.quick_confirm(phrase), phrase)
        self.assertEqual(self.mac.events, [])

    def test_quick_confirm_failure_has_no_question(self):
        client = AIClient(llm=ScriptedLLM())
        self.mac.clipboard_info = "«class PNGf», 100"
        turn = client.quick_confirm("напиши маме в телеграм привет")
        self.assertEqual(turn.pending, [])
        self.assertIn("В буфере обмена не текст", turn.speech)


def confirm_text(turn):
    from core.speech_text import confirmation_question
    return confirmation_question(turn.pending)


class TestMainChain(FlowCase):
    def test_quick_phrase_question_yes(self):
        app = self.make_app()
        main.VoiceApp.process_command(app, "напиши маме в телеграм привет")
        self.assertEqual(self.spoken(app), ["Отправить Мама сообщение: «привет»?"])
        self.assertEqual(app.ai.llm.requests, [])
        pending = app.set_pending.call_args.args[0]
        self.assertNotIn("enter", self.mac.names())
        app.engine.speak.reset_mock()
        app.take_pending.return_value = (pending, "напиши маме в телеграм привет")
        main.VoiceApp.handle_confirmation(app, "да")
        self.assertEqual(self.spoken(app), ["Выполняю.", "Нажал Enter в чате с Мама. Если сообщения нет, поле было пустым."])
        self.assertEqual(self.mac.names().count("enter"), 1)

    def test_quick_phrase_question_no(self):
        app = self.make_app()
        main.VoiceApp.process_command(app, "напиши маме в телеграм привет")
        pending = app.set_pending.call_args.args[0]
        before = list(self.mac.events)
        app.engine.speak.reset_mock()
        app.take_pending.return_value = (pending, "напиши маме в телеграм привет")
        main.VoiceApp.handle_confirmation(app, "нет")
        self.assertEqual(self.spoken(app), ["Отменил, черновик остался в чате, можете стереть его вручную."])
        self.assertEqual(self.mac.events, before)               # после «нет» не вызвана ни одна клавиша и команда
        self.assertNotIn("enter", self.mac.names())

    def test_timeout_speaks_draft_note_and_presses_nothing(self):
        app = self.make_app()
        main.VoiceApp.process_command(app, "напиши маме в телеграм привет")
        pending = app.set_pending.call_args.args[0]
        before = list(self.mac.events)
        app.engine.speak.reset_mock()
        app.pending_actions, app.pending_phrase = pending, "напиши маме в телеграм привет"
        app.pending_lock = main.Lock()
        main.VoiceApp.expire_pending(app, pending)
        self.assertEqual(self.spoken(app), ["Отменил, черновик остался в чате, можете стереть его вручную."])
        self.assertEqual(self.mac.events, before)

    def test_ordinary_cancel_phrases_unchanged(self):
        from test_tools import call as c
        app = self.make_app(AIClient(llm=ScriptedLLM(tools_reply(c("quit_app", {"app": "Terminal"})))))
        main.VoiceApp.process_command(app, "закрой терминал")
        pending = app.set_pending.call_args.args[0]
        app.engine.speak.reset_mock()
        app.take_pending.return_value = (pending, "закрой терминал")
        main.VoiceApp.handle_confirmation(app, "нет")
        self.assertEqual(self.spoken(app), ["Отменено."])

    def test_confirmation_timeout_is_20_seconds(self):
        self.assertEqual(main.CONFIRM_TIMEOUT, 20)

    def test_model_path_also_asks(self):
        client = AIClient(llm=ScriptedLLM(tools_reply(self.send_call(text="привет"))))
        app = self.make_app(client)
        app.mac.execute = MagicMock(return_value=None)
        main.VoiceApp.process_command(app, "скажи маме привет в телеграме")
        self.assertEqual(self.spoken(app), ["Отправить Мама сообщение: «привет»?"])
        self.assertNotIn("enter", self.mac.names())


class TestJournalAndLogsHaveNoText(FlowCase):
    def setUp(self):
        super().setUp()
        self.journal_path = Path(tests_base.TEMP_ROOT) / "tg-actions.jsonl"
        self.journal_path.unlink(missing_ok=True)
        old = journal.JOURNAL_FILE
        journal.setup_journal(self.journal_path)
        self.addCleanup(setattr, journal, "JOURNAL_FILE", old)

    def entries(self):
        return [json.loads(line) for line in self.journal_path.read_text(encoding="utf-8").splitlines()]

    def run_flow(self, answer):
        phrase = f"напиши маме в телеграм {MESSAGE}"
        client = AIClient(llm=ScriptedLLM())
        turn = client.quick_confirm(phrase)
        if answer == "yes":
            client.run_actions(turn.pending, phrase=phrase, confirmed=True)
        else:
            client.reject_actions(turn.pending, phrase, "отменено пользователем", confirmed=False)
        return phrase

    def test_sent_message_logged_without_text(self):
        phrase = self.run_flow("yes")
        raw = self.journal_path.read_text(encoding="utf-8")
        self.assertNotIn(MESSAGE, raw)
        self.assertNotIn("личный", raw)
        [entry] = self.entries()
        self.assertEqual(entry["action"], {"name": "telegram", "args": {"action": "send", "to": "маме",
                                                                         "text_length": len(MESSAGE)}})
        self.assertEqual(entry["phrase"], f"(сообщение, {len(MESSAGE)} символов)")
        self.assertIs(entry["confirmed"], True)
        self.assertTrue(entry["ok"])
        self.assertIn("time", entry)
        self.assertEqual(entry["result"], "✅ Нажал Enter в чате с Мама. Если сообщения нет, поле было пустым.")

    def test_cancel_logged_without_text_and_with_draft_note(self):
        self.run_flow("no")
        raw = self.journal_path.read_text(encoding="utf-8")
        self.assertNotIn(MESSAGE, raw)
        [entry] = self.entries()
        self.assertIs(entry["confirmed"], False)
        self.assertFalse(entry["ok"])
        self.assertIn("черновик остался в чате", entry["result"])
        self.assertEqual(entry["action"]["args"]["text_length"], len(MESSAGE))

    def test_refusals_logged_without_text(self):
        client = AIClient(llm=ScriptedLLM())
        client.quick_confirm("напиши маме в телеграм пароль 12345")
        self.assertNotIn("12345", self.journal_path.read_text(encoding="utf-8"))

    def test_application_log_has_no_text(self):
        with self.assertLogs(level="INFO") as logs:
            phrase = self.run_flow("yes")
        joined = "\n".join(logs.output)
        self.assertNotIn(MESSAGE, joined)
        self.assertNotIn("личный", joined)

    def test_processing_log_for_message_words(self):
        for phrase in (f"напиши маме в телеграм {MESSAGE}", "отправь сообщение", "что нового в телеграме",
                       "пришли мне сообщения", "Напишите заметку"):
            with self.subTest(phrase=phrase):
                app = self.make_app()
                app.ai = MagicMock()
                app.ai.quick_confirm.return_value = None
                app.mac.execute = MagicMock(return_value="Готово")
                with self.assertLogs("main", level="INFO") as logs:
                    main.VoiceApp.process_command(app, phrase, "voice")
                self.assertEqual(logs.output[0], f"INFO:main:Обработка (voice): (сообщение, {len(phrase)} символов)")
                self.assertNotIn(phrase, "\n".join(logs.output))

    def test_processing_log_for_other_phrases_unchanged(self):
        app = self.make_app()
        app.ai = MagicMock()
        app.ai.quick_confirm.return_value = None
        app.mac.execute = MagicMock(return_value="Готово")
        with self.assertLogs("main", level="INFO") as logs:
            main.VoiceApp.process_command(app, "какая сегодня погода", "text")
        self.assertEqual(logs.output[0], "INFO:main:Обработка (text): какая сегодня погода")

    def test_loggable_helpers(self):
        self.assertEqual(journal.loggable_phrase("поставь таймер"), "поставь таймер")
        self.assertEqual(journal.loggable_phrase("Напиши маме"), "(сообщение, 11 символов)")
        action = {"name": "telegram", "args": {"action": "send", "to": "мама", "text": "секрет"}}
        self.assertEqual(journal.loggable_action(action)["args"], {"action": "send", "to": "мама", "text_length": 6})
        self.assertEqual(action["args"]["text"], "секрет")        # исходное действие не меняется
        plain = {"name": "timer", "args": {"action": "start", "label": "чай"}}
        self.assertEqual(journal.loggable_action(plain), plain)


class TestExternalData(FlowCase):
    def test_send_forbidden_after_external_data(self):
        self.mac.lenient = True       # источники (календарь, заметки) — любые команды osascript
        for source in (call("web_search", {"query": "новости"}, "w"),
                       call("calendar", {"action": "list_today", "title": "", "start": "", "minutes": 0}, "c"),
                       call("reminders", {"action": "list", "title": "", "when": "", "list_name": ""}, "r"),
                       call("notes", {"action": "read_last", "title": "", "text": ""}, "n")):
            with self.subTest(source=source["name"]), patch("core.web.search", return_value=("1. Текст", "окно", [])):
                self.mac.events.clear()
                client = AIClient(llm=ScriptedLLM(tools_reply(source, self.send_call(text="привет")), final("нет")))
                turn = client.respond("прочитай новости и напиши маме в телеграм привет")
                self.assertTrue(turn.speech.endswith(GENERIC_REFUSAL_REPLY), turn.speech)
                self.assertEqual(turn.pending, [])
                self.assertNotIn("paste", self.mac.names())            # черновика нет, клавиши не нажимались
                self.assertNotIn("enter", self.mac.names())

    def test_send_forbidden_in_next_round_after_page_data(self):
        client = AIClient(llm=ScriptedLLM(tools_reply(call("web_search", {"query": "x"}, "w")),
                                          tools_reply(self.send_call(text="привет")), final("нет")))
        with patch("core.web.search", return_value=("1. Текст", "окно", [])):
            turn = client.respond("найди и напиши маме в телеграм привет")
        self.assertEqual(turn.pending, [])
        self.assertNotIn("paste", self.mac.names())

    def test_open_chat_after_external_data_is_allowed(self):
        client = AIClient(llm=ScriptedLLM(tools_reply(call("web_search", {"query": "x"}, "w"),
                                                      call("telegram", {"action": "open_chat", "to": "мама", "text": ""}, "o")),
                                          final("ок")))
        with patch("core.web.search", return_value=("1. Текст", "окно", [])):
            client.respond("найди и открой чат с мамой")
        self.assertIn("open", self.mac.names())


class TestSchemasAndTopics(unittest.TestCase):
    def test_messaging_topic_and_keywords(self):
        self.assertIn("messaging", registry.TOPICS)
        tool = registry.get("telegram")
        self.assertEqual(tool.topic, "messaging")
        for word in ("сообщение", "напиши", "написать", "отправь", "чат", "переписка", "контакты"):
            self.assertGreater(registry.keyword_hits(tool.keywords, [word]), 0, word)
        for word in ("телеграм", "telegram", "телеграме", "ватсап"):   # название приложения темой messaging не является
            self.assertEqual(registry.keyword_hits(tool.keywords, [word]), 0, word)

    def test_selected_for_messaging_phrases(self):
        for phrase in ["напиши маме в телеграм привет", "отправь сообщение Ане", "открой чат с мамой",
                       "открой переписку с мамой", "покажи контакты", "хочу написать Ане", "открой телеграм и напиши маме привет"]:
            tools, info = topics.select(phrase, TOOLS)
            self.assertIn("telegram", [t["function"]["name"] for t in tools], phrase)
            self.assertLessEqual(topics.size(tools), 3500)

    def test_budgets(self):
        self.assertLessEqual(topics.size(TOOLS), 14000)
        by_name = {t["function"]["name"]: t for t in TOOLS}
        everything = [by_name[n] for n in [*topics.BASE, *topics.topic_tools("messaging"), "more_tools"]]
        self.assertLessEqual(topics.size(everything), 6500)
        worst, _ = topics.select(" ".join(k for _, kw in topics.catalog().values() for k in kw), TOOLS)
        self.assertLessEqual(topics.size(worst), 6500)


if __name__ == "__main__":
    unittest.main()
