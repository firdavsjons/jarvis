"""Выбор инструментов по теме: бюджет токенов Groq, базовый набор, запасной набор, more_tools, подсказка длительности."""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core import prompts, topics
from core.apps import registry
from core.llm import check
from core.tools import TOOLS, TOOL_NAMES
from fixtures_apps import patch_installed
from fixtures_run import isolated_apps
from test_tool_schemas import schema_problems
from test_tools import ScriptedLLM, call, final, tools_reply
from core.ai import AIClient

REQUEST_LIMIT = 6500
TYPICAL_LIMIT = 3500
TOTAL_LIMIT = 14000
TYPICAL = {"поставь таймер на 5 минут": "timer", "что у меня сегодня": "calendar",
           "открой настройки Wi-Fi": "settings", "какая погода": "get_weather"}


def names(tools):
    return [t["function"]["name"] for t in tools]


class TestBudget(unittest.TestCase):
    def test_total_size_of_all_schemas(self):
        self.assertLessEqual(topics.size(TOOLS), TOTAL_LIMIT)

    def test_all_schemas_follow_groq_rules(self):
        self.assertEqual(schema_problems(TOOLS), [])   # все параметры обязательные, простые типы

    def test_more_tools_enum_is_the_topic_list(self):
        schema = next(t for t in TOOLS if t["function"]["name"] == "more_tools")
        self.assertEqual(schema["function"]["parameters"]["properties"]["area"]["enum"], list(registry.TOPICS))
        self.assertEqual(schema["function"]["parameters"]["required"], ["area"])

    def test_typical_one_topic_requests_fit_3500(self):
        for phrase, tool in TYPICAL.items():
            with self.subTest(phrase=phrase):
                tools, info = topics.select(phrase, TOOLS)
                self.assertLessEqual(topics.size(tools), TYPICAL_LIMIT)
                self.assertEqual(names(tools), [*topics.BASE, tool])
                self.assertFalse(info["fallback"])

    def test_every_topic_with_base_fits_request_limit(self):
        by_name = {t["function"]["name"]: t for t in TOOLS}
        for topic in registry.TOPICS:
            chosen = [by_name[n] for n in [*topics.BASE, *topics.topic_tools(topic), "more_tools"]]
            self.assertLessEqual(topics.size(chosen), REQUEST_LIMIT, topic)

    def test_worst_case_every_keyword_stays_within_limit(self):
        words = []
        for _, keywords in topics.catalog().values():
            words += [k for k in keywords]
        phrase = " ".join(words)
        tools, info = topics.select(phrase, TOOLS)
        self.assertLessEqual(topics.size(tools), REQUEST_LIMIT)
        self.assertIn("more_tools", names(tools))      # то, что не влезло, модель запросит сама
        self.assertTrue(info["dropped"])
        self.assertEqual(names(tools)[:5], list(topics.BASE))

    def test_extra_topics_added_by_more_tools(self):
        tools, _ = topics.select("привет", TOOLS, extra_topics={"media"})
        self.assertIn("media", names(tools))

    def test_prompt_unchanged_limit(self):
        self.assertLessEqual(len(prompts.tools_prompt()), prompts.MAX_PROMPT_CHARS)
        self.assertLessEqual(len(prompts.tools_prompt(memory_block="м" * 600)), prompts.MAX_PROMPT_CHARS + 600)


class TestSelection(unittest.TestCase):
    def test_base_set_always_present(self):
        for phrase in ["", "привет", "поставь таймер на 5 минут", "какая погода", "сколько заряда"]:
            self.assertEqual(names(topics.select(phrase, TOOLS)[0])[:5], list(topics.BASE), phrase)
        self.assertEqual(set(topics.BASE), {"open_app", "quit_app", "set_volume", "get_time", "find_apps"})

    def test_topic_keywords(self):
        cases = {"создай заметку про молоко": "notes", "поставь будильник на семь": "alarm",
                 "напомни завтра позвонить": "reminders", "включи тёмную тему": "display",
                 "сколько заряда осталось": "system_info", "поставь на паузу музыку": "media",
                 "какой курс доллара": "get_exchange_rate", "забудь всё обо мне": "clear_memory",
                 "удали файл с рабочего стола": "trash_file", "запусти быструю команду": "shortcuts",
                 "что у меня на неделе": "calendar", "запомни что я люблю кофе": "remember"}
        for phrase, tool in cases.items():
            with self.subTest(phrase=phrase):
                tools, info = topics.select(phrase, TOOLS)
                self.assertIn(tool, names(tools))
                self.assertFalse(info["fallback"])

    def test_no_topic_gives_base_plus_web_plus_more_tools(self):
        tools, info = topics.select("расскажи что-нибудь интересное про космос и ёжиков", TOOLS)
        self.assertTrue(info["fallback"] or "web_search" in names(tools))
        tools, info = topics.select("привет", TOOLS)
        self.assertTrue(info["fallback"])
        self.assertEqual(names(tools), [*topics.BASE, *topics.topic_tools("web"), "more_tools"])
        self.assertLessEqual(topics.size(tools), REQUEST_LIMIT)

    def test_previous_turn_tools_carry_over(self):
        tools, _ = topics.select("а завтра?", TOOLS, carry={"calendar"})
        self.assertIn("calendar", names(tools))
        tools, info = topics.select("а завтра?", TOOLS)
        self.assertTrue(info["fallback"])

    def test_yo_is_e(self):
        tools, _ = topics.select("включи тёмную тему", TOOLS)
        self.assertIn("display", names(tools))

    def test_catalog_covers_every_tool(self):
        covered = set(topics.catalog()) | set(topics.BASE) | {"more_tools"}
        self.assertEqual(covered, TOOL_NAMES)


class TestInRequests(unittest.TestCase):
    def setUp(self):
        self.sched, _ = isolated_apps(self, {})
        installed = patch_installed()
        installed.start()
        self.addCleanup(installed.stop)

    def test_request_carries_only_selected_schemas(self):
        llm = ScriptedLLM(tools_reply(call("timer", {"action": "start", "seconds": 300, "label": ""})))
        AIClient(llm=llm).respond("поставь таймер на 5 минут")
        sent = names(llm.requests[0]["tools"])
        self.assertEqual(sent, [*topics.BASE, "timer"])
        self.assertLessEqual(topics.size(llm.requests[0]["tools"]), TYPICAL_LIMIT)

    def test_fixed_tool_list_is_respected(self):
        from core.tools import tools_subset
        llm = ScriptedLLM(final("ок"))
        AIClient(llm=llm, tools=tools_subset({"get_time"})).respond("привет как дела")
        self.assertEqual(names(llm.requests[0]["tools"]), ["get_time"])

    def test_more_tools_adds_schemas_for_next_round(self):
        llm = ScriptedLLM(tools_reply(call("more_tools", {"area": "personal"})),
                          tools_reply(call("timer", {"action": "start", "seconds": 60, "label": ""})))
        turn = AIClient(llm=llm).respond("привет")
        self.assertEqual(len(llm.requests), 2)          # ровно один дополнительный запрос
        self.assertIn("more_tools", names(llm.requests[0]["tools"]))
        self.assertNotIn("timer", names(llm.requests[0]["tools"]))
        self.assertIn("timer", names(llm.requests[1]["tools"]))
        self.assertTrue(set(topics.topic_tools("personal")) <= set(names(llm.requests[1]["tools"])))
        self.assertEqual(turn.speech, "Таймер на 1 минута запущен.")
        tool_result = json.loads(llm.requests[1]["messages"][-1]["content"])
        self.assertTrue(tool_result["ok"])

    def test_more_tools_unknown_area_is_blocked(self):
        llm = ScriptedLLM(tools_reply(call("more_tools", {"area": "всё сразу"})))
        turn = AIClient(llm=llm).respond("привет")
        self.assertEqual(turn.blocked[0]["name"], "more_tools")

    def test_extra_topics_do_not_leak_to_next_phrase(self):
        client = AIClient(llm=ScriptedLLM(tools_reply(call("more_tools", {"area": "media"})), final("ок"), final("ок")))
        client.respond("привет")
        client.llm.requests.clear()
        client.respond("расскажи анекдот")
        self.assertNotIn("media", names(client.llm.requests[0]["tools"]))

    def test_carry_between_phrases(self):
        replies = [tools_reply(call("calendar", {"action": "list_today", "title": "", "start": "", "minutes": 0})),
                   final("Завтра свободно.")]
        client = AIClient(llm=ScriptedLLM(*replies))
        with patch("core.apps.common.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            client.respond("что у меня сегодня")
            client.llm.requests.clear()
            client.llm.replies = [final("Завтра свободно.")]
            client.respond("а завтра?")
        self.assertIn("calendar", names(client.llm.requests[0]["tools"]))

    def test_duration_hint_added_by_code(self):
        llm = ScriptedLLM(final("Хорошо."))
        AIClient(llm=llm).respond("через 25 минут напомни позвонить маме")
        content = llm.requests[0]["messages"][-1]["content"]
        self.assertEqual(content, "через 25 минут напомни позвонить маме\n[25 минут = 1500 секунд]")

    def test_now_line_has_iso_for_date_math(self):
        prompt = prompts.tools_prompt()
        self.assertRegex(prompt, r"^Сейчас: \w+, \d{1,2} \w+ \d{4} года, \d{2}:\d{2} \(\d{4}-\d{2}-\d{2} \d{2}:\d{2}\)\.")


class TestCheckOutput(unittest.TestCase):
    def test_budget_report_has_four_requests_and_token_estimate(self):
        out = io.StringIO()
        ok = check.check_budget({"GROQ_TPM_LIMIT": "8000", "GEMINI_TPM_LIMIT": "200000"}, out)
        text = out.getvalue()
        self.assertTrue(ok)
        for phrase in TYPICAL:
            self.assertIn(phrase, text)
        self.assertIn("токенов", text)
        self.assertIn("Groq 8000", text)
        chars, tokens, _ = check.request_estimate("какая погода")
        self.assertAlmostEqual(tokens, (chars + len(prompts.tools_prompt()) + len("какая погода")) / 3.5, delta=1)

    def test_budget_flag(self):
        out = io.StringIO()
        self.assertEqual(check.main(["--budget"], config={"GROQ_TPM_LIMIT": "8000"}, out=out), 0)
        self.assertIn("типичный запрос", out.getvalue())


class TestFuzzyTopics(unittest.TestCase):
    """Этап 8.1.1: распознавание искажает слова; нет точных совпадений — нечёткое (difflib ≥ 0,8) вместо запасного набора."""

    def select(self, phrase, **kwargs):
        return topics.select(phrase, TOOLS, **kwargs)

    def test_distorted_words_reach_their_topics(self):
        cases = {"замтки": "personal", "колендарь на завтра": "personal",
                 "пагода в душанбе": "web", "напиши в телеграмм": "messaging", "открой телеграмм": "messaging"}
        for phrase, topic in cases.items():
            with self.subTest(phrase=phrase):
                _, info = self.select(phrase)
                self.assertFalse(info["fallback"])
                self.assertIn(topic, info["topics"])

    def test_distorted_words_are_found_by_fuzzy_match_not_by_prefix(self):
        for phrase in ("замтки", "пагода", "колендарь"):
            with self.subTest(phrase=phrase):
                tools, info = self.select(phrase)
                self.assertTrue(info["fuzzy"])
                self.assertNotIn(topics.MORE_TOOLS, names(tools))        # запасной набор не нужен

    def test_exact_match_is_not_fuzzy(self):
        _, info = self.select("какая погода")
        self.assertFalse(info["fuzzy"])
        _, info = self.select("телеграмм маме")                          # «телеграмм» начинается с «телеграм»
        self.assertFalse(info["fuzzy"])
        self.assertIn("messaging", info["topics"])

    def test_nonsense_still_gets_the_fallback_set(self):
        for phrase in ("абракадабра", "шшшшш ррррр", "открой колкулятор", "ы", ""):
            with self.subTest(phrase=phrase):
                tools, info = self.select(phrase)
                self.assertTrue(info["fallback"])
                self.assertFalse(info["fuzzy"])
                self.assertIn(topics.MORE_TOOLS, names(tools))
                self.assertEqual(info["topics"], ["web"])

    def test_calculator_stays_in_the_base_set(self):
        tools, _ = self.select("открой колкулятор")
        self.assertIn("open_app", names(tools))

    def test_short_words_and_keys_are_not_compared(self):
        self.assertEqual(topics.fuzzy_hits(("вай", "ip", "диск"), ["вой", "ипп", "дом"]), 0)

    def test_inflected_distorted_word(self):
        self.assertGreaterEqual(topics.fuzzy_hits(("календар",), ["колендаря"]), 1)

    def test_carry_and_extra_topics_skip_fuzzy(self):
        tools, info = self.select("абракадабра", extra_topics=["memory"])
        self.assertFalse(info["fuzzy"])
        self.assertFalse(info["fallback"])


if __name__ == "__main__":
    unittest.main()
