"""Этап 7.1.1, часть 2: «себе», перенос темы messaging, диалог о сообщении, лог. subprocess замокан (tests_base)."""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

import main
from core import contacts as contacts_store
from core import topics
from core.ai import AIClient
from core.apps import registry
from core.apps import telegram as tg
from core.errors import ActionError
from core.speech_text import confirmation_question
from core.tools import TOOLS
from fixtures_run import SAMPLE_CONTACTS
from modules.mac_control import MacControl
from test_telegram import TelegramCase
from test_tools import ScriptedLLM, call, final, tools_reply

UNKNOWN_SELF = "Не знаю такого контакта. Добавьте «себе» в contacts.json, инструкция в docs/telegram.md."
SELF_TITLE = "Избранное"


def names(tools):
    return [t["function"]["name"] for t in tools]


class TestSelfResolution(TelegramCase):
    def resolve(self, to, phrase):
        return tg.resolve_contact(to, phrase)

    def test_every_phrase_variant_leads_to_self(self):
        cases = [("себе", "напиши себе в телеграм привет"), ("мне", "напиши мне в телеграм привет"),
                 ("самому себе", "напиши самому себе в телеграм привет"),
                 ("в избранное", "отправь в избранное в телеграм привет"),
                 ("себе в избранное", "напиши себе в избранное в телеграм привет"),
                 ("Себе", "Напиши себе привет"), ("МНЕ", "напиши мне привет")]
        for to, phrase in cases:
            with self.subTest(to=to, phrase=phrase):
                contact = self.resolve(to, phrase)
                self.assertEqual((contact.key, contact.title), ("себе", SELF_TITLE))

    def test_model_renames_are_mapped_when_phrase_has_a_self_word(self):
        for to in ("Saved Messages", "saved messages", "Избранное", "Saved", "Self", "Me", "self", "myself"):
            for phrase in ("напиши себе в телеграм привет", "напиши мне привет", "отправь в избранное привет"):
                with self.subTest(to=to, phrase=phrase):
                    self.assertEqual(self.resolve(to, phrase).key, "себе")

    def test_model_renames_without_self_word_are_not_mapped(self):
        for to in ("Saved Messages", "Избранное", "Self", "Me", "Saved"):
            with self.subTest(to=to), self.assertRaises(ActionError) as ctx:
                self.resolve(to, "напиши маме привет")
            self.assertEqual(ctx.exception.reply, tg.UNKNOWN_CONTACT)

    def test_self_name_without_self_word_is_refused(self):
        for to in ("себе", "мне"):
            with self.subTest(to=to), self.assertRaises(ActionError) as ctx:
                self.resolve(to, "напиши маме привет")      # модель подсунула «себе», пользователь говорил про маму
            self.assertIn(ctx.exception.reply, (tg.NOT_IN_PHRASE + ".", tg.UNKNOWN_CONTACT))

    def test_name_with_self_word_elsewhere_in_phrase_is_not_redirected(self):
        self.assertEqual(self.resolve("маме", "напиши маме что мне скучно").key, "мама")
        self.assertEqual(self.resolve("мама", "напиши маме в телеграм купи себе кофе").key, "мама")
        with self.assertRaises(ActionError):
            self.resolve("Анна", "напиши маме что себе куплю кофе")        # другой получатель, чем названа во фразе

    def test_missing_self_contact_message(self):
        book = {k: v for k, v in SAMPLE_CONTACTS.items() if k != "себе"}
        self.contacts_file.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        for to in ("себе", "Saved Messages", "мне", "в избранное"):
            with self.subTest(to=to), self.assertRaises(ActionError) as ctx:
                self.resolve(to, "напиши себе в телеграм привет")
            self.assertEqual(ctx.exception.reply, UNKNOWN_SELF)
            self.assertEqual(ctx.exception.reply, tg.UNKNOWN_SELF)

    def test_missing_self_contact_nothing_touched_and_quick_rule_goes_to_model(self):
        book = {k: v for k, v in SAMPLE_CONTACTS.items() if k != "себе"}
        self.contacts_file.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        self.assertIsNone(registry.quick_actions("напиши себе в телеграм привет"))
        client = self.client(tools_reply(self.send_call(to="себе", text="привет")))
        turn = client.respond("напиши себе в телеграм привет")
        self.assertEqual(turn.speech, UNKNOWN_SELF)
        self.assertEqual(self.mac.events, [])

    def test_self_word_never_leads_to_another_contact(self):
        similar = {**SAMPLE_CONTACTS, "Сеня": {"username": "senya_user", "title": "Сеня"},
                   "Себастьян": {"username": "sebastian_user", "title": "Себастьян"},
                   "Серёжа": {"username": "serezha_user", "title": "Серёжа"}, "Сева": {"username": "seva_user"}}
        self.contacts_file.write_text(json.dumps(similar, ensure_ascii=False), encoding="utf-8")
        book = contacts_store.load_contacts()
        self.assertEqual([c.key for c in contacts_store.find("себе", book)], ["себе"])
        for other in ("Сеня", "Себастьян", "Серёжа", "Сева"):
            with self.subTest(other=other), self.assertRaises(ActionError) as ctx:
                self.resolve(other, "напиши себе в телеграм привет")       # модель назвала другого, чем «себе»
            self.assertEqual(ctx.exception.reply, tg.NOT_IN_PHRASE + ".")
        # без контакта «себе» — отказ, а не ближайший по звучанию
        del similar["себе"]
        self.contacts_file.write_text(json.dumps(similar, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(contacts_store.find("себе", contacts_store.load_contacts()), [])
        with self.assertRaises(ActionError) as ctx:
            self.resolve("себе", "напиши себе в телеграм привет")
        self.assertEqual(ctx.exception.reply, UNKNOWN_SELF)

    def test_self_chat_opens_by_self_username(self):
        client = self.client(tools_reply(self.send_call(to="Saved Messages", text="привет")))
        turn = client.respond("напиши себе в телеграм привет")
        self.assertEqual(self.mac.events[2], ("open", "tg://resolve?domain=my_self_user"))
        self.assertEqual(confirmation_question(turn.pending), "Отправить Избранное сообщение: «привет»?")

    def test_open_chat_for_self(self):
        client = self.client(tools_reply(call("telegram", {"action": "open_chat", "to": "в избранное", "text": ""})))
        turn = client.respond("открой в избранное")
        self.assertEqual(turn.speech, "Открыл чат с Избранное.")

    def test_quick_rules_with_self_words(self):
        cases = {"напиши себе в телеграм проверка связи": "себе", "напиши мне в телеграм проверка связи": "мне",
                 "напиши самому себе в телеграм проверка связи": "самому себе",
                 "отправь в избранное в телеграм проверка связи": "в избранное",
                 "отправь себе в телеграме что проверка связи": "себе"}
        for phrase, to in cases.items():
            with self.subTest(phrase=phrase):
                found = registry.quick_actions(phrase)
                self.assertEqual(found[1], {"action": "send", "to": to, "text": "проверка связи"})
        turn = AIClient(llm=ScriptedLLM()).quick_confirm("напиши мне в телеграм проверка связи")
        self.assertEqual(confirmation_question(turn.pending), "Отправить Избранное сообщение: «проверка связи»?")

    def test_to_description_and_budget(self):
        schema = next(t for t in TOOLS if t["function"]["name"] == "telegram")
        description = schema["function"]["parameters"]["properties"]["to"]["description"]
        self.assertEqual(description, "Имя получателя ровно так, как оно сказано в фразе пользователя "
                                      "(например «маме», «себе»), не переводи и не заменяй")
        tools, _ = topics.select("напиши маме в телеграм привет", TOOLS)
        self.assertLessEqual(topics.size(tools), 3500)
        self.assertLessEqual(topics.size(TOOLS), 14000)

    def test_mne_is_not_a_fragment(self):
        from core.fragments import is_fragment
        self.assertFalse(is_fragment("мне"))


class TestDialog(TelegramCase):
    def test_who_then_self_keeps_messaging_tools(self):
        """«напиши в телеграм привет» → «кому?» → «себе»: набор не падает в запасной, текст берётся из первой реплики."""
        llm = ScriptedLLM(final("Кому отправить сообщение?"), tools_reply(self.send_call(to="себе", text="привет")))
        client = AIClient(llm=llm)
        first = client.respond("напиши в телеграм привет")
        self.assertEqual(first.speech, "Кому отправить сообщение?")
        self.assertTrue(client.message_dialog_open())
        self.assertIn("telegram", names(llm.requests[0]["tools"]))
        turn = client.respond("себе")
        second_tools = names(llm.requests[0 + 1]["tools"])
        self.assertIn("telegram", second_tools)
        self.assertNotIn("more_tools", second_tools)                       # не запасной набор
        self.assertEqual(second_tools, [*topics.BASE, "telegram"])
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(confirmation_question(turn.pending), "Отправить Избранное сообщение: «привет»?")
        self.assertEqual(turn.pending[0]["check_phrase"], "напиши в телеграм привет себе")
        self.assertNotIn("enter", self.mac.names())
        self.assertFalse(client.message_dialog_open())                    # ожидающая отправка диалог закрывает
        [result] = client.run_actions(turn.pending, phrase="себе", confirmed=True)  # «да»: фраза в main — последняя
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.mac.names().count("enter"), 1)

    def test_without_dialog_the_text_of_the_first_phrase_is_not_accepted(self):
        """Контроль: без открытого диалога фраза «себе» одна, и текст «привет» из неё не следует."""
        client = AIClient(llm=ScriptedLLM(tools_reply(self.send_call(to="себе", text="привет"))))
        turn = client.respond("себе")
        self.assertEqual(turn.pending, [])
        self.assertIn("Не уверен", turn.speech)
        self.assertEqual(self.mac.events, [])

    def test_recipient_first_then_text(self):
        llm = ScriptedLLM(final("Какой текст отправить?"), tools_reply(self.send_call(to="маме", text="привет")))
        client = AIClient(llm=llm)
        client.respond("напиши маме в телеграм")
        turn = client.respond("привет")
        self.assertEqual(confirmation_question(turn.pending), "Отправить Мама сообщение: «привет»?")
        [result] = client.run_actions(turn.pending, phrase="привет", confirmed=True)   # получатель назван в первой реплике
        self.assertTrue(result["ok"], result)

    def test_ambiguous_name_question_opens_dialog(self):
        llm = ScriptedLLM(tools_reply(self.send_call(to="Иван", text="привет")),
                          tools_reply(self.send_call(to="Иван Петров", text="привет")))
        client = AIClient(llm=llm)
        first = client.respond("напиши ивану в телеграм привет")
        self.assertEqual(first.speech, "Кому именно: Иван Иванов, Иван Петров?")
        self.assertTrue(client.message_dialog_open())
        turn = client.respond("Иван Петров")
        self.assertEqual(confirmation_question(turn.pending), "Отправить Иван Петров сообщение: «привет»?")

    def test_dialog_closes_when_answer_is_not_a_question(self):
        llm = ScriptedLLM(final("Кому отправить?"), final("Расскажу анекдот."), final("Ок."))
        client = AIClient(llm=llm)
        client.respond("напиши в телеграм привет")
        self.assertTrue(client.message_dialog_open())
        client.respond("расскажи анекдот")
        self.assertFalse(client.message_dialog_open())

    def test_dialog_chain_of_questions_and_limit(self):
        llm = ScriptedLLM(final("Кому?"), final("Какой текст?"), final("Точно кому?"), final("И ещё раз кому?"))
        client = AIClient(llm=llm)
        for phrase in ("напиши в телеграм", "маме", "хм", "ещё"):
            client.respond(phrase)
        self.assertLessEqual(len(client._dialog["phrases"]), 3)

    def test_other_topics_do_not_open_a_dialog_but_carry_tools(self):
        llm = ScriptedLLM(final("Какой город?"), final("Хорошо."))
        client = AIClient(llm=llm)
        client.respond("какая погода")
        self.assertFalse(client.message_dialog_open())
        client.respond("Москва")
        self.assertIn("get_weather", names(llm.requests[1]["tools"]))     # набор не потерялся

    def test_dialog_context_only_for_messaging_tools(self):
        client = AIClient(llm=ScriptedLLM())
        client._ctx_phrases = ["напиши маме"]
        self.assertEqual(client._tool_phrase("alarm", "в 7"), "в 7")
        self.assertEqual(client._tool_phrase("telegram", "привет"), "напиши маме привет")

    def test_carry_lasts_two_phrases(self):
        llm = ScriptedLLM(final("Кому?"), final("Ок."), final("Ок."), final("Ок."), final("Ок."))
        client = AIClient(llm=llm)
        client.respond("напиши в телеграм привет")
        for phrase in ("расскажи анекдот", "расскажи ещё анекдот про кота", "расскажи про собак анекдот"):
            client.respond(phrase)
        sent = [names(r["tools"]) for r in llm.requests]
        self.assertIn("telegram", sent[1])
        self.assertIn("telegram", sent[2])
        self.assertNotIn("telegram", sent[3])

    def test_used_tool_stays_for_next_phrase_even_when_rejected(self):
        llm = ScriptedLLM(tools_reply(self.send_call(to="Незнакомец", text="привет")), final("Ок."))
        client = AIClient(llm=llm)
        client.respond("напиши незнакомцу в телеграм привет")        # отклонено: неизвестный контакт
        client.respond("хорошо, спасибо")
        self.assertIn("telegram", names(llm.requests[-1]["tools"]))


class TestLogs(TelegramCase):
    def make_app(self, client):
        app = MagicMock()
        app.ai = client
        app.mac = MacControl()
        app.has_pending.return_value = False
        app.pending_is_private.side_effect = lambda: main.VoiceApp.pending_is_private(app)
        app.pending_actions, app.pending_lock = [], main.Lock()
        app.deliver = lambda turn, text: main.VoiceApp.deliver(app, turn, text)
        app.report_result = lambda results: main.VoiceApp.report_result(app, results)
        app.ask_confirmation = lambda pending, phrase: main.VoiceApp.ask_confirmation(app, pending, phrase)
        return app

    def log_line(self, app, phrase):
        with self.assertLogs("main", level="INFO") as logs:
            main.VoiceApp.process_command(app, phrase, "voice")
        return logs.output[0]

    def test_follow_up_phrase_in_open_dialog_is_hidden(self):
        llm = ScriptedLLM(final("Кому отправить сообщение?"), tools_reply(self.send_call(to="себе", text="привет")))
        app = self.make_app(AIClient(llm=llm))
        first = self.log_line(app, "напиши в телеграм привет")
        self.assertEqual(first, "INFO:main:Обработка (voice): (сообщение, 24 символов)")
        second = self.log_line(app, "себе")
        self.assertEqual(second, "INFO:main:Обработка (voice): (сообщение, 4 символов)")      # диалог открыт

    def test_phrase_not_hidden_when_no_dialog(self):
        llm = ScriptedLLM(final("Хорошо."), final("Хорошо."))
        app = self.make_app(AIClient(llm=llm))
        self.assertEqual(self.log_line(app, "какая погода"), "INFO:main:Обработка (voice): какая погода")
        self.assertEqual(self.log_line(app, "сегодня"), "INFO:main:Обработка (voice): сегодня")

    def test_dialog_ends_and_logging_returns_to_normal(self):
        llm = ScriptedLLM(final("Кому?"), final("Ок, не буду."), final("Хорошо."))
        app = self.make_app(AIClient(llm=llm))
        self.log_line(app, "напиши в телеграм привет")
        self.assertEqual(self.log_line(app, "не надо"), "INFO:main:Обработка (voice): (сообщение, 7 символов)")
        self.assertEqual(self.log_line(app, "какая погода"), "INFO:main:Обработка (voice): какая погода")

    def test_phrase_while_send_is_pending_is_hidden(self):
        app = self.make_app(AIClient(llm=ScriptedLLM(tools_reply(self.send_call(to="себе", text="привет")))))
        self.log_line(app, "напиши себе привет")
        pending = app.set_pending.call_args.args[0]
        app.pending_actions = pending
        app.has_pending.return_value = True
        app.take_pending.return_value = (pending, "напиши себе привет")
        line = self.log_line(app, "подожди")
        self.assertEqual(line, "INFO:main:Обработка (voice): (сообщение, 7 символов)")
        app.pending_actions = []
        app.has_pending.return_value = False
        self.assertEqual(self.log_line(app, "какая погода"), "INFO:main:Обработка (voice): какая погода")

    def test_pending_helper_only_for_private_tools(self):
        app = MagicMock()
        app.pending_lock = main.Lock()
        app.pending_actions = [{"name": "quit_app", "args": {"app": "Safari"}}]
        self.assertFalse(main.VoiceApp.pending_is_private(app))
        app.pending_actions = [{"name": "telegram", "args": {"action": "send", "to": "мама", "text": "x"}}]
        self.assertTrue(main.VoiceApp.pending_is_private(app))

    def test_bare_confirmation_in_dialog_is_hidden_too(self):
        llm = ScriptedLLM(final("Кому?"), final("Понял."))
        app = self.make_app(AIClient(llm=llm))
        self.log_line(app, "напиши в телеграм привет")
        self.assertEqual(self.log_line(app, "нет"), "INFO:main:Обработка (voice): (сообщение, 3 символов)")


class TestContactsFormat(TelegramCase):
    def test_username_and_phone_limits_follow_telegram(self):
        import tempfile
        from pathlib import Path
        path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / "contacts.json"
        path.write_text(json.dumps({
            "четыре": {"username": "abcd"}, "пять": {"username": "abcde"}, "тридцатьдве": {"username": "a" * 32},
            "тридцатьтри": {"username": "a" * 33}, "семь": {"phone": "1234567"}, "восемь": {"phone": "12345678"},
            "пятнадцать": {"phone": "1" * 15}, "шестнадцать": {"phone": "1" * 16}}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(set(contacts_store.load_contacts(path)), {"пять", "тридцатьдве", "восемь", "пятнадцать"})


if __name__ == "__main__":
    unittest.main()
