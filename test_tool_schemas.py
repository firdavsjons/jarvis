import copy
import io
import logging
import os
import re
import sys
import unittest

import httpx
import openai

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.llm import NO_CONNECTION_REPLY, RATE_LIMIT_REPLY, FallbackClient, LLMClient, LLMResponse
from core.llm.base import server_message
from core.llm.router import BAD_REQUEST_PAUSE, COOLDOWN_SECONDS
from core.llm.usage import UsageTracker
from core.logger import SecretFilter, register_secret
from core.tools import TOOLS

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
FUNCTION_KEYS = {"name", "description", "parameters"}
PARAMETERS_KEYS = {"type", "properties", "required"}
# Ключи свойства, которые принимают и Gemini, и Groq (проверено вживую в 4.2 и 5.1)
PROPERTY_KEYS = {"type", "description", "minimum", "maximum", "enum"}
SIMPLE_TYPES = {"string", "integer", "number", "boolean"}


def schema_problems(tools):
    """Нарушения общих правил OpenAI и правила, найденного на Groq (5.1): необязательный параметр модель
    может передать как null, а Groq проверяет вызов по схеме -> 400 tool_use_failed."""
    problems = []
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name", "?")
        if tool.get("type") != "function":
            problems.append(f"{name}: type != function")
        if not NAME_RE.match(str(fn.get("name", ""))):
            problems.append(f"{name}: имя не подходит под ^[a-zA-Z0-9_-]{{1,64}}$")
        if set(fn) - FUNCTION_KEYS:
            problems.append(f"{name}: лишние ключи функции {sorted(set(fn) - FUNCTION_KEYS)}")
        description = fn.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            problems.append(f"{name}: описание пустое или длиннее 1024")
        params = fn.get("parameters", {})
        if params.get("type") != "object":
            problems.append(f"{name}: parameters.type != object")
        if set(params) - PARAMETERS_KEYS:
            problems.append(f"{name}: лишние ключи parameters {sorted(set(params) - PARAMETERS_KEYS)}")
        properties, required = params.get("properties", {}), params.get("required", [])
        if not set(required) <= set(properties):
            problems.append(f"{name}: required не подмножество properties")
        for prop, spec in properties.items():
            if not isinstance(spec.get("type"), str) or spec["type"] not in SIMPLE_TYPES:
                problems.append(f"{name}.{prop}: тип {spec.get('type')!r} (нужен простой тип без null и вложенности)")
            if not isinstance(spec.get("description"), str) or not spec["description"].strip():
                problems.append(f"{name}.{prop}: нет description")
            if set(spec) - PROPERTY_KEYS:
                problems.append(f"{name}.{prop}: неподдерживаемые ключи {sorted(set(spec) - PROPERTY_KEYS)}")
            if prop not in required:
                problems.append(f"{name}.{prop}: необязательный параметр — модель может прислать null, "
                                f"Groq ответит 400 tool_use_failed; сделайте его обязательным")
    return problems


class TestToolSchemas(unittest.TestCase):
    def test_all_tools_follow_rules(self):
        self.assertEqual(schema_problems(TOOLS), [])

    def test_catches_the_groq_bug(self):
        # Схема get_weather до исправления: city необязательный
        old = copy.deepcopy([t for t in TOOLS if t["function"]["name"] == "get_weather"])
        old[0]["function"]["parameters"]["required"] = []
        self.assertEqual(len(schema_problems(old)), 1)
        self.assertIn("get_weather.city: необязательный параметр", schema_problems(old)[0])

    def test_catches_other_violations(self):
        bad = copy.deepcopy(TOOLS[:1])
        fn = bad[0]["function"]
        fn["name"] = "open app!"
        fn["description"] = "x" * 1025
        fn["parameters"]["properties"]["app"] = {"type": ["string", "null"], "format": "uri"}
        fn["parameters"]["required"] = ["app", "missing"]
        problems = " | ".join(schema_problems(bad))
        for expected in ["имя не подходит", "длиннее 1024", "required не подмножество", "нет description",
                         "неподдерживаемые ключи", "нужен простой тип"]:
            self.assertIn(expected, problems)
        print("✅ Схемы инструментов: OK")


REQUEST = httpx.Request("POST", "https://x")
SECRET = "gsk_SCHEMA_TEST_SECRET_1234567890"


def status_error(cls, code, message):
    body = {"error": {"message": message, "code": "tool_use_failed"}}
    return cls(message, response=httpx.Response(code, request=REQUEST), body=body)


class Provider(LLMClient):
    def __init__(self, name, error=None, daily_limit=None):
        self.name, self.model, self.daily_limit, self.timezone = name, "m", daily_limit, "UTC"
        self.error = error

    def chat(self, messages, tools=None):
        if self.error:
            raise self.error
        return LLMResponse(ok=True, text="ok", provider=self.name)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestServerMessage(unittest.TestCase):
    def setUp(self):
        register_secret(SECRET)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.addFilter(SecretFilter())
        logging.getLogger().addHandler(self.handler)

    def tearDown(self):
        logging.getLogger().removeHandler(self.handler)

    def test_400_logged_with_reason_and_short_pause(self):
        error = status_error(openai.BadRequestError, 400, "Tool call validation failed: parameters for tool "
                             "get_weather did not match schema: errors: [`/city`: expected string, but got null] "
                             + "x" * 300 + f" key={SECRET}")
        clock = Clock()
        client = FallbackClient([Provider("groq", error), Provider("local")], clock=clock)
        with self.assertLogs("core.llm.router", level="WARNING") as logs:
            self.assertEqual(client.chat([]).provider, "local")
        line = logs.output[0]
        self.assertIn("ошибка запроса (400): [tool_use_failed] Tool call validation failed", line)
        self.assertIn("пауза 5 с", line)
        self.assertEqual(client._down_until["groq"] - clock.t, BAD_REQUEST_PAUSE)
        self.assertLessEqual(len(server_message(error)), 200)
        self.assertNotIn(SECRET, self.stream.getvalue())

    def test_secret_redacted_in_server_message(self):
        error = status_error(openai.AuthenticationError, 401, f"Invalid API Key {SECRET}")
        self.assertNotIn(SECRET, server_message(error))
        self.assertIn("***", server_message(error))

    def test_other_errors_keep_60s_and_no_message(self):
        clock = Clock()
        client = FallbackClient([Provider("groq", status_error(openai.InternalServerError, 503, "secret body")),
                                 Provider("local")], clock=clock)
        client.chat([])
        self.assertEqual(client._down_until["groq"] - clock.t, COOLDOWN_SECONDS)
        self.assertEqual(server_message(status_error(openai.InternalServerError, 503, "x")), "")
        print("✅ Сообщение сервера в логе: OK")


def rate_limit():
    return openai.RateLimitError("429", response=httpx.Response(429, request=REQUEST), body=None)


class TestFailureReply(unittest.TestCase):
    def test_all_429_says_limit(self):
        client = FallbackClient([Provider("gemini", rate_limit()), Provider("groq", rate_limit())], clock=Clock())
        self.assertEqual(client.chat([]).text, RATE_LIMIT_REPLY)
        self.assertEqual(client.chat([]).text, RATE_LIMIT_REPLY)  # оба на паузе из-за лимита — всё ещё «лимит»

    def test_mixed_says_no_connection(self):
        bad = status_error(openai.BadRequestError, 400, "bad")
        client = FallbackClient([Provider("gemini", rate_limit()), Provider("groq", bad)], clock=Clock())
        self.assertEqual(client.chat([]).text, NO_CONNECTION_REPLY)
        offline = FallbackClient([Provider("gemini", openai.APIConnectionError(request=REQUEST))], clock=Clock())
        self.assertEqual(offline.chat([]).text, NO_CONNECTION_REPLY)

    def test_daily_limit_counts_as_limit(self):
        usage = UsageTracker({"gemini": 1, "groq": 800}, {})
        usage.record("gemini")  # 1 из 1 — пропуск заранее
        client = FallbackClient([Provider("gemini"), Provider("groq", rate_limit())], usage=usage, clock=Clock())
        self.assertEqual(client.chat([]).text, RATE_LIMIT_REPLY)
        print("✅ «Лимит» или «нет связи»: OK")


if __name__ == '__main__':
    unittest.main()
