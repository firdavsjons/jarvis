"""Этап 8.2.3: «открой калькулятор и закрой телеграм». Тема messaging не для названия приложения, усиленные описания,
один повтор запроса для невыполненных приложений, «Читать ваши чаты я не умею». Модель и сеть подменены."""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core import installed_apps, topics
from core.ai import (APPS_RETRY_NOTE, CANT_READ_CHATS_REPLY, AIClient, reads_personal_messages, unfinished_segments)
from core.cache import CommandCache
from core.tools import TOOLS
from fixtures_apps import patch_installed
from test_tools import ScriptedLLM, call, final, tools_reply

PHRASE = "открой калькулятор и закрой телеграм"
_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()
    installed_apps.set_user_aliases({})


def names(tools):
    return [t["function"]["name"] for t in tools]


# --- темы -------------------------------------------------------------------------------------------------------------
class TestTopics(unittest.TestCase):
    def select(self, phrase):
        return topics.select(phrase, TOOLS)

    def test_the_issue_phrase_has_the_base_set_and_no_messaging(self):
        tools, info = self.select(PHRASE)
        listed = names(tools)
        for base in topics.BASE:
            self.assertIn(base, listed)
        self.assertIn("open_app", listed)
        self.assertIn("quit_app", listed)
        self.assertNotIn("telegram", listed)
        self.assertNotIn("messaging", info["topics"])
        self.assertEqual(info["topics"], [])
        self.assertTrue(info["app_command"])
        self.assertFalse(info["fallback"])                               # хватает базового набора
        self.assertNotIn(topics.MORE_TOOLS, listed)

    def test_messenger_as_an_app_name_next_to_an_open_or_close_verb(self):
        for phrase in ("закрой телеграм", "открой телеграм", "открой ватсап", "запусти telegram", "выйди из телеграм",
                       "выключи ватсап", "закрой whatsapp", "открой сафари и телеграм", "закрой приложение телеграм",
                       "открой вотсап"):
            with self.subTest(phrase=phrase):
                tools, info = self.select(phrase)
                self.assertNotIn("telegram", names(tools))
                self.assertNotIn("messaging", info["topics"])
                for base in topics.BASE:
                    self.assertIn(base, names(tools))
                if not info["topics"]:                                    # «приложение» включает тему apps, но не messaging
                    self.assertTrue(info["app_command"])

    def test_message_words_still_enable_messaging(self):
        for phrase in ("напиши маме в телеграм привет", "отправь сообщение Ане", "открой чат с мамой", "хочу написать Ане",
                       "открой переписку с мамой", "покажи контакты", "открой телеграм и напиши маме привет",
                       "закрой калькулятор и напиши маме в телеграм привет", "отправь файл в телеграм"):
            with self.subTest(phrase=phrase):
                tools, info = self.select(phrase)
                self.assertIn("telegram", names(tools))
                self.assertIn("messaging", info["topics"])
                self.assertFalse(info["app_command"])
                for base in topics.BASE:
                    self.assertIn(base, names(tools))

    def test_the_messaging_keywords(self):
        from core.apps import registry
        tool = registry.get("telegram")
        for word in ("сообщение", "сообщения", "напиши", "написать", "отправь", "чат", "чата", "переписка", "контакт"):
            self.assertGreater(registry.keyword_hits(tool.keywords, [word]), 0, word)
        for word in ("телеграм", "telegram", "ватсап", "телеграме", "открой", "закрой"):
            self.assertEqual(registry.keyword_hits(tool.keywords, [word]), 0, word)

    def test_a_messenger_name_without_a_verb_is_not_an_app_command(self):
        tools, info = self.select("что нового в телеграме")
        self.assertFalse(info["app_command"])
        self.assertTrue(info["fallback"])                                # запасной набор: веб + more_tools
        self.assertNotIn("telegram", names(tools))

    def test_ordinary_app_phrases_are_unchanged(self):
        tools, info = self.select("открой калькулятор")
        self.assertFalse(info["app_command"])
        self.assertTrue(info["fallback"])

    def test_the_request_stays_small(self):
        tools, _ = self.select(PHRASE)
        self.assertLessEqual(topics.size(tools), topics.TYPICAL_REQUEST_CHARS)


# --- описания и бюджет ------------------------------------------------------------------------------------------------
class TestDescriptions(unittest.TestCase):
    def schema(self, name):
        return next(t for t in TOOLS if t["function"]["name"] == name)["function"]

    def test_open_and_quit_ask_for_one_call_per_app_in_one_answer(self):
        for name in ("open_app", "quit_app"):
            text = self.schema(name)["description"]
            self.assertIn("В одном ответе вызывай столько инструментов, сколько приложений названо в фразе", text)
            self.assertIn("по одному вызову на каждое", text)

    def test_example_in_open_app(self):
        self.assertIn("«открой калькулятор и закрой телеграм» — два вызова: open_app(Calculator) и quit_app(Telegram)",
                      self.schema("open_app")["description"])

    def test_web_search_does_not_read_personal_chats(self):
        self.assertIn("Не используй для чтения личных сообщений и чатов пользователя", self.schema("web_search")["description"])

    def test_schema_budget(self):
        self.assertLessEqual(topics.size(TOOLS), 14000)
        everything = [t for t in TOOLS if t["function"]["name"] in [*topics.BASE, "web_search", "get_weather",
                                                                  "get_exchange_rate", "open_url", "read_page", "more_tools"]]
        self.assertLessEqual(topics.size(everything), topics.MAX_REQUEST_CHARS)
        base_only, _ = topics.select(PHRASE, TOOLS)
        self.assertLessEqual(topics.size(base_only), topics.TYPICAL_REQUEST_CHARS)

    def test_schemas_stay_valid_for_groq(self):
        from test_tool_schemas import schema_problems
        self.assertEqual(schema_problems(TOOLS), [])


# --- повтор для невыполненных приложений -------------------------------------------------------------------------------
class RetryCase(unittest.TestCase):
    def setUp(self):
        patcher = patch("core.ai.subprocess.run")
        self.run = patcher.start()
        self.run.return_value.returncode = 0
        self.addCleanup(patcher.stop)

    def respond(self, phrase, *replies, cache=None):
        llm = ScriptedLLM(*replies)
        client = AIClient(llm=llm, cache=cache)
        return client, llm, client.respond(phrase)

    def opened(self):
        return [c.args[0] for c in self.run.call_args_list]


class TestRetryForMissingApps(RetryCase):
    def test_retry_completes_the_rest_and_there_is_no_leftover_note(self):
        client, llm, turn = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})),
                                         tools_reply(call("quit_app", {"app": "Telegram"})))
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(turn.speech, "Открыто приложение Calculator, закрыто приложение Telegram.")
        self.assertNotIn("Остальное", turn.speech)
        self.assertEqual(len(turn.results), 2)
        self.assertTrue(all(r["ok"] for r in turn.results))
        self.assertEqual(self.run.call_count, 2)
        names_run = [r["action"]["name"] for r in turn.results]
        self.assertEqual(names_run, ["open_app", "quit_app"])

    def test_the_retry_request_names_the_missing_apps_and_carries_the_first_round(self):
        _, llm, _ = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})),
                                 tools_reply(call("quit_app", {"app": "Telegram"})))
        retry = llm.requests[1]["messages"]
        self.assertEqual(retry[-1], {"role": "user", "content": "В фразе названы ещё приложения: Телеграм. Вызови инструменты для них."})
        self.assertEqual(APPS_RETRY_NOTE.format(apps="Телеграм"), retry[-1]["content"])
        roles = [m["role"] for m in retry]
        self.assertEqual(roles[-3:], ["assistant", "tool", "user"])               # вызов, его результат, просьба
        self.assertTrue(retry[-3].get("tool_calls"))
        self.assertEqual(llm.requests[0]["messages"][-1]["content"], PHRASE)      # первый запрос — чистая фраза

    def test_retry_that_does_not_complete_keeps_the_leftover_note(self):
        _, llm, turn = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})), final("Готово."))
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: Телеграм.")

    def test_only_one_retry_even_if_the_model_repeats_itself(self):
        _, llm, turn = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})))   # всегда один и тот же ответ
        self.assertEqual(len(llm.requests), 2)                                    # не больше одного повтора
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: Телеграм.")

    def test_retry_with_another_wrong_app_still_ends_with_the_note(self):
        _, llm, turn = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})),
                                    tools_reply(call("quit_app", {"app": "Safari"})))
        self.assertEqual(len(llm.requests), 2)
        self.assertTrue(turn.speech.endswith("Остальное я не сделал: Телеграм."), turn.speech)

    def test_everything_done_at_once_needs_no_retry(self):
        _, llm, turn = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"}, "a"),
                                                        call("quit_app", {"app": "Telegram"}, "b")))
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Открыто приложение Calculator, закрыто приложение Telegram.")

    def test_unknown_app_is_not_retried_but_is_reported(self):
        _, llm, turn = self.respond("открой калькулятор и закрой чатгпт", tools_reply(call("open_app", {"app": "Calculator"})))
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: чатгпт.")

    def test_known_and_unknown_apps_retry_only_for_the_known_one(self):
        _, llm, turn = self.respond("закрой калькулятор, чатгпт и телеграм", tools_reply(call("quit_app", {"app": "Calculator"})),
                                    tools_reply(call("quit_app", {"app": "Telegram"})))
        self.assertEqual(len(llm.requests), 2)
        self.assertIn("ещё приложения: Телеграм.", llm.requests[1]["messages"][-1]["content"])
        self.assertNotIn("чатгпт", llm.requests[1]["messages"][-1]["content"])
        self.assertEqual(turn.speech, "Закрыто приложение Calculator, закрыто приложение Telegram. Остальное я не сделал: чатгпт.")

    def test_a_single_app_never_triggers_a_retry(self):
        _, llm, turn = self.respond("закрой сафари", tools_reply(call("quit_app", {"app": "Safari"})))
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Закрыто приложение Safari.")

    def test_three_apps_two_done_one_retry_for_the_third(self):
        _, llm, turn = self.respond("закрой Safari, Mail и Notes", tools_reply(call("quit_app", {"app": "Safari"}, "a"),
                                                                              call("quit_app", {"app": "Mail"}, "b")),
                                    tools_reply(call("quit_app", {"app": "Notes"})))
        self.assertEqual(len(llm.requests), 2)
        self.assertIn("ещё приложения: Notes.", llm.requests[1]["messages"][-1]["content"])
        self.assertEqual(turn.speech, "Закрыто приложение Safari, закрыто приложение Mail, закрыто приложение Notes.")

    def test_repeating_the_same_call_is_said_once(self):
        _, _, turn = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})),
                                  tools_reply(call("open_app", {"app": "Calculator"})))
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: Телеграм.")

    def test_history_has_the_final_result_not_the_retry_text(self):
        client, _, _ = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})),
                                    tools_reply(call("quit_app", {"app": "Telegram"})))
        last = client.history.messages()[-1]["content"]
        self.assertNotIn("В фразе названы ещё приложения", last)
        self.assertEqual(len(client.history.messages()), 2)


class TestCacheAfterRetry(RetryCase):
    def test_completed_phrase_is_cached_whole(self):
        cache = CommandCache()
        _, _, turn = self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})),
                                  tools_reply(call("quit_app", {"app": "Telegram"})), cache=cache)
        self.assertEqual(len(cache), 1)
        self.assertEqual([a["name"] for a in cache.lookup(PHRASE)], ["open_app", "quit_app"])
        # второй раз — из кэша, без модели
        llm = ScriptedLLM()
        again = AIClient(llm=llm, cache=cache).respond(PHRASE)
        self.assertEqual(llm.requests, [])
        self.assertEqual(again.speech, "Открыто приложение Calculator. Закрыто приложение Telegram.")

    def test_incomplete_phrase_is_not_cached(self):
        cache = CommandCache()
        self.respond(PHRASE, tools_reply(call("open_app", {"app": "Calculator"})), final("Готово."), cache=cache)
        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.lookup(PHRASE))


class TestUnfinishedSegments(unittest.TestCase):
    def result(self, name, app):
        return {"ok": True, "action": {"name": name, "args": {"app": app}}}

    def test_segments(self):
        self.assertEqual(unfinished_segments(PHRASE, [self.result("open_app", "Calculator")]), ["телеграм"])
        self.assertEqual(unfinished_segments(PHRASE, [self.result("open_app", "Calculator"),
                                                       self.result("quit_app", "Telegram")]), [])
        self.assertEqual(unfinished_segments("закрой сафари", [self.result("quit_app", "Safari")]), [])
        self.assertEqual(unfinished_segments(PHRASE, []), [])


# --- «что нового в телеграме» -----------------------------------------------------------------------------------------
class TestReadingChats(RetryCase):
    def test_detector(self):
        for phrase in ("что нового в телеграме", "прочитай мои сообщения в телеграме", "что нового в телеграм",
                       "есть новые сообщения в ватсапе", "кто мне написал в телеграм", "покажи непрочитанные сообщения",
                       "что мне пришло в чате", "прочитай последние сообщения", "прочти мои чаты"):
            with self.subTest(phrase=phrase):
                self.assertTrue(reads_personal_messages(phrase))
        for phrase in ("напиши маме в телеграм привет", "отправь сообщение Ане", "открой чат с мамой", "открой телеграм",
                       "закрой телеграм", "что нового в мире", "прочитай первую ссылку", "прочитай сайт example.com",
                       "найди новости про Тома Круза", "какая погода", "открой калькулятор и закрой телеграм"):
            with self.subTest(phrase=phrase):
                self.assertFalse(reads_personal_messages(phrase))

    def test_web_search_is_replaced_by_the_honest_phrase_and_never_runs(self):
        for phrase in ("что нового в телеграме", "прочитай мои сообщения в телеграме", "есть новые сообщения в ватсапе"):
            with self.subTest(phrase=phrase), patch("core.web.search") as search:
                client = AIClient(llm=ScriptedLLM(tools_reply(call("web_search", {"query": "updates"}))))
                with self.assertLogs("core.ai", level="INFO") as logs:
                    turn = client.respond(phrase)
                search.assert_not_called()                                # в интернет запрос не уходил
                self.assertEqual(turn.speech, "Читать ваши чаты я не умею.")
                self.assertEqual(turn.speech, CANT_READ_CHATS_REPLY)
                self.assertTrue(any("веб-инструмент не выполняю" in line for line in logs.output))
                self.assertNotIn(phrase, "\n".join(logs.output))

    def test_read_page_is_blocked_too(self):
        with patch("core.web.fetch_page") as fetch:
            client = AIClient(llm=ScriptedLLM(tools_reply(call("read_page", {"link_number": 1}))))
            turn = client.respond("прочитай мои сообщения в телеграме")
        fetch.assert_not_called()
        self.assertEqual(turn.speech, CANT_READ_CHATS_REPLY)

    def test_ordinary_search_still_works(self):
        with patch("core.web.search", return_value=("1. Новость — Источник, сегодня", "1. Новость\n   https://example.com/a",
                                                    ["https://example.com/a"])) as search:
            llm = ScriptedLLM(tools_reply(call("web_search", {"query": "новости про Тома Круза"})), final("Вот новость."))
            turn = AIClient(llm=llm).respond("найди новости про Тома Круза")
        search.assert_called_once()
        self.assertNotIn("Читать ваши чаты", turn.speech)

    def test_other_actions_in_the_same_answer_still_run(self):
        with patch("core.web.search") as search:
            llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Telegram"}, "a"),
                                          call("web_search", {"query": "сообщения"}, "b")))
            turn = AIClient(llm=llm).respond("открой телеграм и прочитай мои сообщения")
        search.assert_not_called()
        self.assertEqual(turn.speech, "Открыто приложение Telegram. Читать ваши чаты я не умею.")
        self.assertEqual(self.run.call_count, 1)

    def test_no_web_call_means_the_models_own_answer(self):
        llm = ScriptedLLM(final("Я не умею читать ваши чаты."))
        turn = AIClient(llm=llm).respond("что нового в телеграме")
        self.assertEqual(turn.speech, "Я не умею читать ваши чаты.")

    def test_the_phrase_is_a_fallback_topic_not_messaging(self):
        client = AIClient(llm=ScriptedLLM(final("ок")))
        client.respond("что нового в телеграме")
        sent = client.llm.requests[0]["tools"]
        self.assertNotIn("telegram", names(sent))
        self.assertIn("web_search", names(sent))


if __name__ == "__main__":
    unittest.main()
