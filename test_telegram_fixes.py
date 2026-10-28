"""Этап 7.1.2: фаза 1 Telegram (шаги в лог, фокус, пауза после Cmd+V), «собой/мной», подтверждение словами, ложный
успех по шаблонам, история с «[действие: …]», список приложений, история в диалоге. subprocess замокан (tests_base)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

import json

from core import topics
from core.ai import (CONFIRM_RETRY_NOTE, PROMISE_FAIL_REPLY, PROMISE_RETRY_NOTE, AIClient, claims_unperformed_action,
                     incomplete_note, requested_apps, strip_service_marks)
from core.apps import registry
from core.apps import telegram as tg
from core.errors import ActionError
from core.history import action_line
from core.speech_text import confirmation_question
from core.tools import TOOLS
from fixtures_apps import patch_installed
from fixtures_run import isolated_apps
from test_telegram import FakeMac, TelegramCase
from test_tools import ScriptedLLM, call, final, tools_reply


class TestPhase1Logging(TelegramCase):
    def ask(self, text="привет", phrase=None):
        client = self.client(tools_reply(self.send_call(text=text)))
        return client, client.respond(phrase or f"напиши маме в телеграм {text}")

    def test_every_step_logged_without_text(self):
        text = "приветствие-из-теста"
        with self.assertLogs("core.apps.telegram", level="INFO") as logs:
            client, turn = self.ask(text=text, phrase=f"напиши маме в телеграм {text}")
        joined = "\n".join(logs.output)
        for expected in ("чат «мама» открыт по ссылке", "на переднем плане с окном — да, ждали", "вставка нажата (способ keycode), длина текста 20",
                         "после вставки ждали 1.0 с", "прежний буфер обмена возвращён"):
            self.assertIn(expected, joined)
        self.assertNotIn(text, joined)
        self.assertNotIn("mama_user", joined)
        order = [joined.index(x) for x in ("открыт по ссылке", "на переднем плане", "вставка нажата", "после вставки", "буфер обмена возвращён")]
        self.assertEqual(order, sorted(order))

    def test_not_in_front_at_paste_time_aborts_without_keys(self):
        self.mac.front = "com.apple.Safari"       # окно Telegram было, но к моменту Cmd+V впереди другое приложение
        with self.assertLogs("core.apps.telegram", level="WARNING") as logs:
            client, turn = self.ask()
        self.assertEqual(turn.pending, [])
        self.assertEqual(turn.speech, "Не получилось: Telegram не в фокусе.")
        self.assertNotIn("paste_keycode", self.mac.names())                # Cmd+V не нажат
        self.assertNotIn("enter", self.mac.names())
        self.assertEqual(self.mac.pasteboard, "старый буфер")              # буфер возвращён
        self.assertIn("Telegram: перед вставкой впереди другое приложение", "\n".join(logs.output))
        self.assertIsNone(tg._state)

    def test_focus_checked_right_before_paste_after_pbcopy(self):
        self.ask()
        names = self.mac.names()
        self.assertEqual(names[names.index("pbcopy"):names.index("pbcopy") + 3], ["pbcopy", "front", "pbpaste"])

    def test_buffer_restored_only_after_the_wait(self):
        self.ask()
        events = self.mac.events
        paste = events.index(("paste_keycode",))
        self.assertEqual(events[paste + 1], ("sleep", 1.0))                 # TELEGRAM_PASTE_WAIT
        self.assertEqual(events[paste + 2], ("pbcopy", "старый буфер"))     # и только потом возврат прежнего буфера

    def test_wait_follows_the_setting(self):
        with patch("core.apps.telegram.paste_wait", return_value=0.3):
            self.ask()
        events = self.mac.events
        self.assertEqual(events[events.index(("paste_keycode",)) + 1], ("sleep", 0.3))

    def test_buffer_restored_even_if_wait_is_interrupted(self):
        self.mac.sleep = lambda s: (_ for _ in ()).throw(RuntimeError("прервано")) if s == 1.0 else None
        with patch("core.apps.telegram.time.sleep", side_effect=self.mac.sleep):
            with self.assertRaises(RuntimeError):
                self.ask()
        self.assertEqual(self.mac.pasteboard, "старый буфер")


class TestPasteWaitSetting(unittest.TestCase):
    def test_default_and_env(self):
        self.assertEqual(tg.DEFAULT_PASTE_WAIT, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, ".env")
            for raw, expected in [(None, 1.0), ("0.3", 0.3), ("0", 0.0), ("abc", 1.0), ("-2", 0.0), ("99", 5.0)]:
                with open(env, "w", encoding="utf-8") as f:
                    f.write("" if raw is None else f"TELEGRAM_PASTE_WAIT={raw}\n")
                with patch("core.settings.ENV_FILE", env):
                    self.assertEqual(tg.paste_wait(), expected, raw)
        text = Path(__file__).with_name(".env.example").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_PASTE_WAIT=1.0", text)


class TestSelfForms(TelegramCase):
    FORMS = ["собой", "собою", "мной", "мною", "обо мне", "со мной", "самим собой"]

    def test_every_form_leads_to_self(self):
        for form in self.FORMS:
            phrase = f"открой чат с {form}" if form not in ("обо мне", "со мной") else f"открой чат {form}"
            with self.subTest(form=form):
                self.assertEqual(tg.resolve_contact(form, phrase).key, "себе")
                self.assertTrue(tg.phrase_has_self_word(phrase))

    def test_open_chat_with_sobo(self):
        self.assertEqual(registry.quick_actions("открой чат с собой")[1], {"action": "open_chat", "to": "собой", "text": ""})
        from modules.mac_control import MacControl
        self.assertEqual(MacControl().execute("открой чат с собой"), "Открыл чат с Избранное.")
        self.assertEqual(self.mac.events[0], ("open", "tg://resolve?domain=my_self_user"))

    def test_open_chat_through_model(self):
        client = self.client(tools_reply(call("telegram", {"action": "open_chat", "to": "собой", "text": ""})))
        self.assertEqual(client.respond("открой чат с собой в тг").speech, "Открыл чат с Избранное.")

    def test_quick_send_forms(self):
        for phrase, to in {"напиши мной в телеграм тест": "мной", "напиши обо мне в телеграм тест": "обо мне"}.items():
            self.assertEqual(registry.quick_actions(phrase)[1]["to"], to, phrase)

    def test_forms_do_not_lead_to_others(self):
        for form in ("собой", "мной"):
            with self.assertRaises(ActionError):
                tg.resolve_contact("Анна", f"напиши маме что я с {form}")      # другой получатель — не «себе»
        self.assertEqual(tg.resolve_contact("маме", "напиши маме что я с собой").key, "мама")

    def test_without_self_word_in_phrase_form_is_refused(self):
        with self.assertRaises(ActionError):
            tg.resolve_contact("собой", "напиши маме привет")


class TestConfirmationInWords(TelegramCase):
    def dialog(self, *replies):
        llm = ScriptedLLM(*replies)
        return AIClient(llm=llm), llm

    def test_chain_who_self_expects_send_and_jarvis_question(self):
        client, llm = self.dialog(final("Кому отправить сообщение?"),
                                  final("Подтвердите отправку сообщения «привет» себе в Telegram."),
                                  tools_reply(self.send_call(to="себе", text="привет")))
        client.respond("напиши в телеграм привет")
        turn = client.respond("себе")
        self.assertEqual(len(llm.requests), 3)                                  # ответ словами → один повтор
        self.assertEqual(llm.requests[2]["messages"][-1], {"role": "user", "content": CONFIRM_RETRY_NOTE})
        self.assertEqual(CONFIRM_RETRY_NOTE, "Вызови telegram(action=send); подтверждение запросит сам Jarvis, "
                                             "не проси его словами.")
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(confirmation_question(turn.pending), "Отправить Избранное сообщение: «привет»?")
        self.assertNotIn("enter", self.mac.names())

    def test_question_about_sending_also_triggers(self):
        client, llm = self.dialog(final("Кому отправить сообщение?"), final("Отправить «привет» себе в Telegram?"),
                                  tools_reply(self.send_call(to="себе", text="привет")))
        client.respond("напиши в телеграм привет")
        turn = client.respond("себе")
        self.assertEqual(len(turn.pending), 1)

    def test_retry_only_once(self):
        client, llm = self.dialog(final("Кому отправить сообщение?"), final("Подтвердите отправку, пожалуйста."),
                                  final("Подтвердите отправку сообщения ещё раз."))
        client.respond("напиши в телеграм привет")
        turn = client.respond("себе")
        self.assertEqual(len(llm.requests), 3)
        self.assertEqual(turn.pending, [])
        self.assertIn("Подтвердите", turn.speech)                              # второй раз ответ идёт пользователю

    def test_not_triggered_when_text_is_unknown(self):
        client, llm = self.dialog(final("Какой текст отправить?"))
        turn = client.respond("напиши маме в телеграм")
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Какой текст отправить?")

    def test_not_triggered_when_recipient_is_unknown(self):
        client, llm = self.dialog(final("Кому отправить сообщение? Подтвердите получателя."))
        client.respond("напиши в телеграм привет")
        self.assertEqual(len(llm.requests), 1)

    def test_not_triggered_outside_messaging(self):
        client, llm = self.dialog(final("Подтвердите удаление файла?"))
        client.respond("удали файл со стола")
        self.assertEqual(len(llm.requests), 1)

    def test_text_known_helper(self):
        for phrase, known in {"напиши маме в телеграм": False, "напиши в телеграм привет себе": True,
                              "напиши маме в телеграм привет": True, "отправь сообщение": False,
                              "напиши в телеграм привет маме": True, "открой чат с мамой": False}.items():
            self.assertEqual(tg.text_known(phrase), known, phrase)

    def test_description_tells_the_model(self):
        schema = next(t for t in TOOLS if t["function"]["name"] == "telegram")
        self.assertIn("Подтверждение отправки запрашивает сам Jarvis, не проси его словами.",
                      schema["function"]["description"])
        tools, _ = topics.select("напиши маме в телеграм привет", TOOLS)
        self.assertLessEqual(topics.size(tools), 3500)
        self.assertLessEqual(topics.size(TOOLS), 14000)


class AppCase(unittest.TestCase):
    def setUp(self):
        isolated_apps(self, {})
        installed = patch_installed()
        installed.start()
        self.addCleanup(installed.stop)
        patcher = patch("core.ai.subprocess.run")
        self.run_mock = patcher.start()
        self.run_mock.return_value.returncode = 0
        self.run_mock.return_value.stderr = ""
        self.addCleanup(patcher.stop)


class TestTemplateClaims(AppCase):
    TEMPLATES = ["Закрыто приложение System Settings.", "Открыто приложение Safari.", "Громкость 50.",
                 "Тёмная тема включена.", "Тёмная тема выключена.", "Яркость 80 процентов.", "Скриншот сохранён на рабочем столе.",
                 "Экран заблокирован.", "Нажал Enter в чате с Избранное.", "Открыл чат с Мама.",
                 "Открыл настройки Wi-Fi.", "Открыта ссылка ютуб точка ком.", "Таймер на 5 минут запущен.",
                 "Будильник на сегодня в 7:00. Он сработает, только если Jarvis запущен.",
                 "Напоминание «Молоко» создано.", "Событие «Планёрка» создано на завтра в 10:00.",
                 "Заметка «Покупки» создана.", "Хорошо. Закрыто приложение Mail."]

    def test_template_phrase_without_call_is_a_claim_even_without_command_verb(self):
        for answer in self.TEMPLATES:
            with self.subTest(answer=answer):
                self.assertTrue(claims_unperformed_action("системные настройки", answer))
                self.assertTrue(claims_unperformed_action("", answer))

    def test_system_settings_case_from_the_log(self):
        llm = ScriptedLLM(final("Закрыто приложение System Settings."), final("Закрыто приложение System Settings."))
        turn = AIClient(llm=llm).respond("системные настройки")
        self.assertEqual(turn.speech, PROMISE_FAIL_REPLY)
        self.assertEqual(len(llm.requests), 2)                                 # повтор ровно один
        self.assertEqual(llm.requests[1]["messages"][-1], {"role": "user", "content": PROMISE_RETRY_NOTE})
        self.run_mock.assert_not_called()

    def test_retry_with_real_call_succeeds(self):
        llm = ScriptedLLM(final("Закрыто приложение Safari."), tools_reply(call("quit_app", {"app": "Safari"})))
        turn = AIClient(llm=llm).respond("сафари")
        self.assertEqual(turn.speech, "Закрыто приложение Safari.")
        self.run_mock.assert_called_once()

    def test_ordinary_answers_untouched(self):
        for phrase, answer in [("до скольки открыт музей", "Музей открыт до 18:00."),
                               ("какая громкость лучше", "Громкость 50 процентов — это умеренно."),
                               ("что такое яркость", "Яркость 80 процентов экрана обычно слепит."),
                               ("расскажи про тему", "Тёмная тема бережёт глаза."), ("кто ты", "Я Jarvis."),
                               ("где приложение", "Приложение закрыто на обновление."),
                               ("что с чатом", "Открыто окно чата, но я его не вижу.")]:
            with self.subTest(answer=answer):
                llm = ScriptedLLM(final(answer))
                self.assertEqual(AIClient(llm=llm).respond(phrase).speech, answer)
                self.assertEqual(len(llm.requests), 1)

    def test_after_real_call_template_phrase_is_fine(self):
        llm = ScriptedLLM(tools_reply(call("get_time")), final("Закрыто приложение — шутка. Сейчас вечер."))
        self.assertIn("Сейчас вечер", AIClient(llm=llm).respond("который час").speech)


class TestHistoryHasNoTemplates(AppCase):
    def test_template_turn_stored_as_action(self):
        client = AIClient(llm=ScriptedLLM(tools_reply(call("quit_app", {"app": "Safari"}))))
        turn = client.respond("закрой сафари")
        self.assertEqual(turn.speech, "Закрыто приложение Safari.")
        history = client.history.messages()
        self.assertEqual(history[-1], {"role": "assistant", "content": "[действие: закрыто приложение Safari]"})
        self.assertNotIn("Закрыто приложение Safari.", json.dumps(history, ensure_ascii=False))

    def test_model_does_not_see_templates_as_assistant_text(self):
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Safari"})), final("Хорошо."))
        client = AIClient(llm=llm)
        client.respond("закрой сафари")
        client.respond("спасибо большое")
        assistant = [m["content"] for m in llm.requests[1]["messages"] if m["role"] == "assistant"]
        self.assertEqual(assistant, ["[действие: закрыто приложение Safari]"])

    def test_several_actions_and_failures(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Safari"}, "a"), call("set_volume", {"level": 50}, "b")))
        client = AIClient(llm=llm)
        client.respond("открой сафари и поставь громкость 50")
        self.assertEqual(client.history.messages()[-1]["content"], "[действие: открыто приложение Safari; громкость 50]")

    def test_non_template_turns_keep_old_format(self):
        llm = ScriptedLLM(tools_reply(call("get_time")), final("Сейчас вечер."))
        client = AIClient(llm=llm)
        client.respond("который час")
        self.assertEqual(client.history.messages()[-1]["content"], "Сейчас вечер. [get_time: успешно]")
        client = AIClient(llm=ScriptedLLM(final("Привет!")))
        client.respond("привет")
        self.assertEqual(client.history.messages()[-1]["content"], "Привет!")

    def test_action_line_helper(self):
        results = [{"action": {"name": "quit_app", "args": {"app": "X"}}, "ok": True, "result": "Закрыто приложение X"},
                   {"action": {"name": "open_app", "args": {"app": "Y"}}, "ok": False, "short": "не найдено"}]
        self.assertEqual(action_line(results), "[действие: закрыто приложение X; open_app: ошибка: не найдено]")
        self.assertEqual(action_line([]), "")

    def test_service_marks_are_stripped_from_answers(self):
        for raw, clean in {"Какой текст отправить себе в Telegram?[ожидание ответа]": "Какой текст отправить себе в Telegram?",
                           "Готово. [действие: закрыто приложение X]": "Готово.", "Ответ [1] подробнее": "Ответ [1] подробнее",
                           "Привет [тест] [ещё]": "Привет", "": "", None: ""}.items():
            self.assertEqual(strip_service_marks(raw), clean)
        llm = ScriptedLLM(final("Кому отправить сообщение?[ожидание ответа]"))
        self.assertEqual(AIClient(llm=llm).respond("привет, как жизнь").speech, "Кому отправить сообщение?")

    def test_prompt_rule_and_limit(self):
        from core import prompts
        prompt = prompts.tools_prompt()
        self.assertIn("[действие: …]", prompt)
        self.assertLessEqual(len(prompt), prompts.MAX_PROMPT_CHARS)


class TestListOfApps(AppCase):
    def test_requested_apps(self):
        self.assertEqual(requested_apps("закрой календарь, чатгпт и настройки"), ["календарь", "чатгпт", "настройки"])
        self.assertEqual(requested_apps("закрой приложении календарь чат чпт и настройки системы"),
                         ["календарь чат чпт", "настройки системы"])
        self.assertEqual(requested_apps("Открой Safari и Mail"), ["safari", "mail"])
        self.assertEqual(requested_apps("закрой сафари"), [])
        self.assertEqual(requested_apps("привет и пока"), [])
        self.assertEqual(requested_apps("закрой safari, пожалуйста, и mail"), ["safari", "mail"])

    def test_three_apps_one_call_says_what_is_left(self):
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Safari"})))
        client = AIClient(llm=llm)
        turn = client.respond("закрой Safari, Mail и Notes")
        self.assertEqual(turn.speech, "Закрыто приложение Safari. Остальное я не сделал: mail, notes.")
        self.assertIn("Остальное я не сделал: mail, notes.", client.history.messages()[-1]["content"])

    def test_log_example_calendar_chatgpt_settings(self):
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Notes"})))
        turn = AIClient(llm=llm).respond("закрой заметки, чатгпт и календарь")
        self.assertEqual(turn.speech, "Закрыто приложение Notes. Остальное я не сделал: чатгпт, календарь.")

    def test_all_called_no_note(self):
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Safari"}, "a"), call("quit_app", {"app": "Mail"}, "b"),
                                      call("quit_app", {"app": "Notes"}, "c")))
        self.assertEqual(AIClient(llm=llm).respond("закрой Safari, Mail и Notes").speech,
                         "Закрыто приложение Safari, закрыто приложение Mail, закрыто приложение Notes.")

    def test_two_of_three_note_lists_the_third(self):
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Safari"}, "a"), call("quit_app", {"app": "Mail"}, "b")))
        turn = AIClient(llm=llm).respond("закрой Safari, Mail и Notes")
        self.assertTrue(turn.speech.endswith("Остальное я не сделал: notes."), turn.speech)

    def test_other_tools_cover_their_segment(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Safari"}, "a"), call("set_volume", {"level": 50}, "b")))
        turn = AIClient(llm=llm).respond("открой сафари и громкость 50")
        self.assertEqual(turn.speech, "Открыто приложение Safari, громкость 50.")

    def test_no_note_without_app_calls_or_list(self):
        self.assertEqual(incomplete_note("закрой Safari и Mail", []), "")
        results = [{"action": {"name": "quit_app", "args": {"app": "Safari"}}, "ok": True}]
        self.assertEqual(incomplete_note("закрой сафари", results), "")
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Safari"})))
        self.assertEqual(AIClient(llm=llm).respond("закрой сафари").speech, "Закрыто приложение Safari.")

    def test_descriptions_ask_for_separate_calls(self):
        for name in ("open_app", "quit_app"):
            schema = next(t for t in TOOLS if t["function"]["name"] == name)
            self.assertIn("вызывай инструмент отдельно для каждого приложения из фразы", schema["function"]["description"].lower())

    def test_template_path_ignores_second_model_answer(self):
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Safari"})), final("Закрыл Safari, остальное пока не смог."))
        self.run_mock.return_value.returncode = 0
        # шаблонный путь: ответ строится без второго запроса, поэтому слова модели сюда не попадают
        turn = AIClient(llm=llm).respond("закрой Safari, Mail и Notes")
        self.assertIn("Остальное я не сделал", turn.speech)


class TestHistoryInDialog(TelegramCase):
    def fill(self, client):
        for i in range(3):
            client.history.add_exchange(f"вопрос номер {i} " + "ы" * 300, f"ответ номер {i} " + "о" * 300)

    def test_dialog_keeps_only_last_two_exchanges(self):
        llm = ScriptedLLM(final("Кому отправить сообщение?"), tools_reply(self.send_call(to="себе", text="привет")))
        client = AIClient(llm=llm)
        self.fill(client)
        client.respond("напиши в телеграм привет")
        full = llm.requests[0]["messages"]                      # диалога ещё нет — вся история
        client.respond("себе")
        in_dialog = llm.requests[1]["messages"]
        history = [m for m in in_dialog if m["role"] != "system"][:-1]
        self.assertEqual(len(history), 4)                      # 2 обмена
        self.assertEqual(history[0]["content"][:14], "вопрос номер 2")        # последние, а не первые
        self.assertEqual(history[-1]["content"], "Кому отправить сообщение?")
        self.assertLess(len(json.dumps(in_dialog, ensure_ascii=False)), len(json.dumps(full, ensure_ascii=False)))
        self.assertEqual(len([m for m in full if m["role"] != "system"]) - 1, 6)

    def test_request_size_is_smaller_in_dialog(self):
        def size(open_dialog):
            llm = ScriptedLLM(final("Кому отправить сообщение?"), final("Хорошо."))
            client = AIClient(llm=llm)
            self.fill(client)
            if open_dialog:
                client.respond("напиши в телеграм привет")
            else:
                client.respond("привет, как дела")                  # ответ без вопроса — диалога нет
            client.respond("себе" if open_dialog else "ещё привет")
            return len(json.dumps(llm.requests[1]["messages"], ensure_ascii=False))
        self.assertLess(size(True), size(False))

    def test_history_not_trimmed_outside_dialog(self):
        llm = ScriptedLLM(final("Хорошо."))
        client = AIClient(llm=llm)
        self.fill(client)
        client.respond("расскажи анекдот")
        history = [m for m in llm.requests[0]["messages"] if m["role"] != "system"][:-1]
        self.assertEqual(len(history), 6)

    def test_history_in_dialog_does_not_touch_stored_history(self):
        llm = ScriptedLLM(final("Кому отправить сообщение?"), tools_reply(self.send_call(to="себе", text="привет")))
        client = AIClient(llm=llm)
        self.fill(client)
        client.respond("напиши в телеграм привет")
        client.respond("себе")
        self.assertGreaterEqual(len(client.history.messages()), 6)


if __name__ == "__main__":
    unittest.main()
