import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import httpx
import openai

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import installed_apps, prompts
from core.ai import AIClient, summarize_results, template_speech
from core.llm import FallbackClient, LLMClient, LLMResponse
from core.llm.router import COOLDOWN_SECONDS
from core.llm.usage import UsageTracker
from core.memory import SECRET_REFUSAL
from fixtures_apps import patch_installed
import main

_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()
    installed_apps.set_user_aliases({})


def call(name, args=None):
    return {"id": f"id_{name}", "name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}


def tools_reply(*calls):
    return LLMResponse(ok=True, tool_calls=list(calls), mode="tools")


def final(text):
    return LLMResponse(ok=True, text=text, mode="tools")


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def chat(self, messages, tools=None, json_messages=None):
        self.requests.append(messages)
        return self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]


class RunMock(unittest.TestCase):
    def setUp(self):
        patcher = patch('core.ai.subprocess.run')
        self.mock_run = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_run.return_value.returncode = 0
        self.mock_run.return_value.stderr = ""
        self.addCleanup(installed_apps.set_user_aliases, {})


class TestNoSecondRequest(RunMock):
    def test_open_app_and_volume_one_request(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Safari"}), call("set_volume", {"level": 50})),
                          final("не должно понадобиться"))
        turn = AIClient(llm=llm).respond("открой сафари и громкость 50")
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Открыто приложение Safari, громкость 50.")
        self.assertEqual(self.mock_run.call_count, 2)

    def test_templates(self):
        cases = [
            (call("quit_app", {"app": "Safari"}), "Закрыто приложение Safari."),
            (call("set_volume", {"level": 30}), "Громкость 30."),
            (call("open_url", {"url": "https://www.youtube.com/watch?v=1"}), "Открыта ссылка ютуб точка ком."),
        ]
        for tool_call, expected in cases:
            llm = ScriptedLLM(tools_reply(tool_call))
            self.assertEqual(AIClient(llm=llm).respond("сделай").speech, expected)
            self.assertEqual(len(llm.requests), 1)

    def test_more_than_three_actions_says_done(self):
        results = [{"action": {"name": "set_volume", "args": {}}, "ok": True, "result": f"Громкость {i}"}
                   for i in range(4)]
        self.assertEqual(template_speech(results), "Готово.")

    def test_failure_keeps_second_request(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Spotify"})), final("Не получилось: Spotify нет."))
        self.assertEqual(AIClient(llm=llm).respond("открой спотифай").speech, "Не получилось: Spotify нет.")
        self.assertEqual(len(llm.requests), 2)

    def test_data_tools_keep_second_request(self):
        for tool_call in [call("get_time"), call("find_apps", {"query": "tele"}), call("list_apps"),
                          call("list_memory")]:
            llm = ScriptedLLM(tools_reply(tool_call), final("Ответ по данным."))
            self.assertEqual(AIClient(llm=llm).respond("скажи").speech, "Ответ по данным.")
            self.assertEqual(len(llm.requests), 2, tool_call["name"])

    def test_mixed_simple_and_data_keeps_second_request(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Safari"}), call("get_time")), final("Готово, вечер."))
        AIClient(llm=llm).respond("открой сафари и скажи время")
        self.assertEqual(len(llm.requests), 2)
        print("✅ Без второго запроса: OK")


class TestNotFoundIsFailure(RunMock):
    def test_forget_memory_not_found(self):
        client = AIClient(llm=MagicMock())
        [result] = client.run_actions([{"name": "forget_memory", "args": {"query": "пиво"}}], phrase="забудь про пиво")
        self.assertEqual(summarize_results([result]), (False, "Не получилось: не нашёл."))

    def test_find_apps_not_found_goes_to_model_as_error(self):
        llm = ScriptedLLM(tools_reply(call("find_apps", {"query": "qwertyuiop"})), final("Нашёл, всё готово!"))
        turn = AIClient(llm=llm).respond("найди qwertyuiop")
        tool_msg = json.loads([m for m in llm.requests[1] if m["role"] == "tool"][0]["content"])
        self.assertEqual((tool_msg["ok"], tool_msg["error"]), (False, "не нашёл"))
        self.assertEqual(turn.speech, "Не получилось: не нашёл.")  # страховка: модель объявила успех

    def test_empty_list_memory_is_success(self):
        [result] = AIClient(llm=MagicMock()).run_actions([{"name": "list_memory", "args": {}}], phrase="что ты знаешь")
        self.assertTrue(result["ok"])
        print("✅ «Не найдено» — неудача: OK")


class TestRememberWithConfirmation(RunMock):
    def make_app(self, client):
        app = MagicMock()
        app.ai = client
        app.has_pending.return_value = False
        app.mac.execute.return_value = None
        app.report_result = lambda results: main.VoiceApp.report_result(app, results)
        app.deliver = lambda turn, text: main.VoiceApp.deliver(app, turn, text)
        app.ask_confirmation = lambda pending, phrase: main.VoiceApp.ask_confirmation(app, pending, phrase)
        return app

    def spoken(self, app):
        return [c.args[0] for c in app.engine.speak.call_args_list]

    def ask(self, phrase, args):
        client = AIClient(llm=ScriptedLLM(tools_reply(call("remember", args))))
        app = self.make_app(client)
        main.VoiceApp.process_command(app, phrase)
        return client, app

    def test_without_word_asks_then_yes(self):
        client, app = self.ask("моя машина красная", {"text": "Машина пользователя красная"})
        self.assertEqual(self.spoken(app), ["Запомнить: Машина пользователя красная?"])
        self.assertEqual(client.memory.facts, [])  # до «да» — ничего
        pending = app.set_pending.call_args.args[0]

        app.engine.speak.reset_mock()
        app.take_pending.return_value = (pending, "моя машина красная")
        main.VoiceApp.handle_confirmation(app, "да")
        self.assertEqual(client.memory.facts, ["Машина пользователя красная"])
        self.assertEqual(self.spoken(app), ["Выполняю.", "Запомнил."])

    def test_without_word_no(self):
        client, app = self.ask("меня зовут Фирдавс", {"text": "меня зовут Фирдавс"})
        pending = app.set_pending.call_args.args[0]
        app.take_pending.return_value = (pending, "меня зовут Фирдавс")
        main.VoiceApp.handle_confirmation(app, "нет")
        self.assertEqual(client.memory.facts, [])

    def test_secret_without_word_refused_at_once(self):
        client = AIClient(llm=ScriptedLLM(LLMResponse(ok=True, mode="json", text=json.dumps(
            {"response": "Сохраняю.", "actions": [{"name": "remember", "args": {"text": "мой пароль qwerty"}}]},
            ensure_ascii=False))))
        turn = client.respond("мой пароль qwerty")
        self.assertEqual(turn.pending, [])  # без вопроса
        self.assertEqual(turn.speech, SECRET_REFUSAL)
        self.assertEqual(client.memory.facts, [])

    def test_with_word_no_question(self):
        llm = ScriptedLLM(tools_reply(call("remember", {"text": "Машина красная"})), final("Запомнил."))
        client = AIClient(llm=llm)
        turn = client.respond("Запомни: моя машина красная")
        self.assertEqual((turn.pending, client.memory.facts), ([], ["Машина красная"]))

    def test_unconfirmed_execution_still_rejected(self):
        # Страховка: без «запомни» и без «да» исполнитель сам не запишет
        client = AIClient(llm=MagicMock())
        [result] = client.run_actions([{"name": "remember", "args": {"text": "факт"}}], phrase="просто факт")
        self.assertFalse(result["ok"])
        self.assertEqual(client.memory.facts, [])
        print("✅ Память через подтверждение: OK")


class TestPrompt(unittest.TestCase):
    def test_length_and_rules(self):
        prompt = prompts.tools_prompt()
        self.assertLessEqual(len(prompt), prompts.MAX_PROMPT_CHARS)
        self.assertIn("list_memory — только если спрашивают", prompt)
        self.assertIn("«кто ты»", prompt)
        self.assertIn("без «запомни» ассистент сам спросит подтверждение", prompt)
        print("✅ Промпт: OK")


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


REQUEST = httpx.Request("POST", "https://x")


def rate_limit():
    return openai.RateLimitError("429", response=httpx.Response(429, request=REQUEST), body=None)


class Provider(LLMClient):
    def __init__(self, name, outcomes):
        self.name, self.model, self.daily_limit, self.timezone = name, "m", None, "UTC"
        self.outcomes, self.calls = list(outcomes), 0

    def chat(self, messages, tools=None):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(ok=True, text=outcome, provider=self.name)


class TestRateLimitPause(unittest.TestCase):
    def down_for(self, client, name):
        return client._down_until[name] - client._clock()

    def hit(self, client, clock, seconds_after):
        clock.t += seconds_after
        client.chat([{"role": "user", "content": "x"}])

    def test_three_429_in_ten_minutes(self):
        clock = Clock()
        gemini, groq = Provider("gemini", [rate_limit()]), Provider("groq", ["ok"])
        client = FallbackClient([gemini, groq], clock=clock)
        self.hit(client, clock, 0)
        self.assertEqual(self.down_for(client, "gemini"), COOLDOWN_SECONDS)
        self.hit(client, clock, 61)
        with self.assertLogs("core.llm.router", level="WARNING") as logs:
            self.hit(client, clock, 61)
        self.assertEqual(logs.output, ["WARNING:core.llm.router:лимит Gemini, пауза 15 мин"])
        self.assertEqual(self.down_for(client, "gemini"), 15 * 60)
        self.hit(client, clock, 14 * 60)
        self.assertEqual(gemini.calls, 3)  # всё ещё на паузе
        self.hit(client, clock, 61)
        self.assertEqual(gemini.calls, 4)

    def test_success_resets_streak(self):
        clock = Clock()
        gemini = Provider("gemini", [rate_limit(), rate_limit(), "ok", rate_limit()])
        client = FallbackClient([gemini, Provider("groq", ["ok"])], clock=clock)
        for _ in range(4):
            self.hit(client, clock, 61)
        self.assertEqual(self.down_for(client, "gemini"), COOLDOWN_SECONDS)

    def test_other_error_resets_streak(self):
        clock = Clock()
        timeout = openai.APITimeoutError(request=REQUEST)
        gemini = Provider("gemini", [rate_limit(), rate_limit(), timeout, rate_limit()])
        client = FallbackClient([gemini, Provider("groq", ["ok"])], clock=clock)
        for _ in range(4):
            self.hit(client, clock, 61)
        self.assertEqual(self.down_for(client, "gemini"), COOLDOWN_SECONDS)

    def test_spread_over_ten_minutes(self):
        clock = Clock()
        gemini = Provider("gemini", [rate_limit()])
        client = FallbackClient([gemini, Provider("groq", ["ok"])], clock=clock)
        for gap in (0, 6 * 60, 6 * 60):  # 1-й и 3-й — 12 минут разницы
            self.hit(client, clock, gap)
        self.assertEqual(self.down_for(client, "gemini"), COOLDOWN_SECONDS)

    def test_pause_from_env(self):
        import tempfile
        from pathlib import Path
        from core.llm.router import build_default_client
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("LLM_PROVIDERS=local\nLLM_RATE_LIMIT_PAUSE_MIN=30\n", encoding="utf-8")
            with patch('openai.OpenAI'):
                self.assertEqual(build_default_client(env).rate_limit_pause, 30 * 60)
        print("✅ Пауза после 429: OK")


class TestUsageLog(unittest.TestCase):
    def test_every_ten_requests(self):
        usage = UsageTracker({"gemini": 200, "groq": 800}, {"gemini": "America/Los_Angeles"})
        with self.assertLogs("core.llm.usage", level="INFO") as logs:
            for i in range(20):
                usage.record("gemini" if i < 12 else "groq")
        self.assertEqual(logs.output, ["INFO:core.llm.usage:запросы сегодня: gemini 10, groq 0",
                                       "INFO:core.llm.usage:запросы сегодня: gemini 12, groq 8"])
        print("✅ Счётчик в логе: OK")


if __name__ == '__main__':
    unittest.main()
