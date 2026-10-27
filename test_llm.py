import io
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import openai

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.ai import AIClient
from core.llm import NO_CONNECTION_REPLY, LLMClient, LLMResponse, FallbackClient
from core.llm import check
from core.llm.base import EmptyResponseError, describe_error
from core.llm.providers import OpenAICompatProvider, build_providers, load_config
from core.llm.router import COOLDOWN_SECONDS
from core.llm.usage import UsageTracker
from core.logger import SecretFilter, register_secret

FAKE_KEY = "gsk_TEST_SECRET_KEY_must_never_be_logged_1234567890"
REQUEST = httpx.Request("POST", "https://example.test/v1/chat/completions")


def status_error(cls, code):
    return cls(f"error {code}", response=httpx.Response(code, request=REQUEST), body=None)


def rate_limit():
    return status_error(openai.RateLimitError, 429)


def timeout():
    return openai.APITimeoutError(request=REQUEST)


class FakeProvider(LLMClient):
    """Провайдер без сети: каждое значение в outcomes — ответ (str) или исключение."""

    def __init__(self, name, outcomes=("ok",), daily_limit=None, tz="UTC"):
        self.name, self.model = name, f"{name}-model"
        self.daily_limit, self.timezone = daily_limit, tz
        self.outcomes = list(outcomes)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(ok=True, text=f"{self.name}: {outcome}", provider=self.name, model=self.model)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def chain(*providers, clock=None, usage=None):
    return FallbackClient(providers, usage=usage, clock=clock or Clock())


MSG = [{"role": "user", "content": "привет"}]


class TestFallback(unittest.TestCase):
    def test_first_provider_answers(self):
        gemini, groq = FakeProvider("gemini"), FakeProvider("groq")
        response = chain(gemini, groq).chat(MSG)
        self.assertEqual((response.ok, response.provider), (True, "gemini"))
        self.assertEqual(groq.calls, 0)

    def test_switch_on_429(self):
        gemini, groq = FakeProvider("gemini", [rate_limit()]), FakeProvider("groq")
        with self.assertLogs("core.llm.router", level="WARNING") as logs:
            response = chain(gemini, groq).chat(MSG)
        self.assertEqual(response.provider, "groq")
        self.assertIn("лимит запросов (429)", logs.output[0])

    def test_switch_on_timeout(self):
        gemini, groq = FakeProvider("gemini", [timeout()]), FakeProvider("groq")
        self.assertEqual(chain(gemini, groq).chat(MSG).provider, "groq")

    def test_switch_on_server_and_config_errors(self):
        for error in [status_error(openai.InternalServerError, 503), status_error(openai.AuthenticationError, 401),
                      status_error(openai.NotFoundError, 404), openai.APIConnectionError(request=REQUEST),
                      EmptyResponseError()]:
            with self.subTest(error=type(error).__name__):
                gemini, groq = FakeProvider("gemini", [error]), FakeProvider("groq")
                self.assertEqual(chain(gemini, groq).chat(MSG).provider, "groq")

    def test_failed_provider_skipped_for_60_seconds(self):
        clock = Clock()
        gemini, groq = FakeProvider("gemini", [rate_limit(), "ok"]), FakeProvider("groq")
        client = chain(gemini, groq, clock=clock)

        client.chat(MSG)
        self.assertEqual(gemini.calls, 1)
        clock.t += COOLDOWN_SECONDS - 1
        self.assertEqual(client.chat(MSG).provider, "groq")
        self.assertEqual(gemini.calls, 1)  # пропущен — не вызывался
        clock.t += 2  # прошло больше 60 с
        self.assertEqual(client.chat(MSG).provider, "gemini")
        self.assertEqual(gemini.calls, 2)

    def test_all_down_returns_russian_phrase(self):
        client = chain(FakeProvider("gemini", [rate_limit()]), FakeProvider("groq", [timeout()]),
                       FakeProvider("local", [openai.APIConnectionError(request=REQUEST)]))
        response = client.chat(MSG)  # без исключения
        self.assertFalse(response.ok)
        self.assertEqual(response.text, NO_CONNECTION_REPLY)
        # Сразу после — все на паузе, всё равно фраза, а не исключение
        self.assertEqual(client.chat(MSG).text, NO_CONNECTION_REPLY)

    def test_no_providers(self):
        self.assertEqual(FallbackClient([]).chat(MSG).text, NO_CONNECTION_REPLY)

    def test_ai_client_all_down(self):
        llm = chain(FakeProvider("gemini", [rate_limit()]), FakeProvider("local", [timeout()]))
        self.assertEqual(AIClient(llm=llm).ask("привет"), {"response": NO_CONNECTION_REPLY, "actions": []})
        print("✅ Цепочка провайдеров: OK")


class TestLimits(unittest.TestCase):
    def test_switch_at_90_percent(self):
        usage = UsageTracker({"gemini": 10, "groq": 100}, {"gemini": "America/Los_Angeles"})
        gemini, groq = FakeProvider("gemini"), FakeProvider("groq")
        client = chain(gemini, groq, usage=usage)

        for _ in range(9):  # 9 из 10 = 90%
            self.assertEqual(client.chat(MSG).provider, "gemini")
        with self.assertLogs("core.llm.usage", level="WARNING") as logs:
            self.assertEqual(client.chat(MSG).provider, "groq")
            self.assertEqual(client.chat(MSG).provider, "groq")
        self.assertEqual(gemini.calls, 9)
        self.assertEqual(len(logs.output), 1)  # предупреждение один раз
        self.assertIn("9 из 10", logs.output[0])

    def test_local_has_no_limit(self):
        usage = UsageTracker({"local": None}, {})
        local = FakeProvider("local")
        client = chain(local, usage=usage)
        for _ in range(50):
            self.assertTrue(client.chat(MSG).ok)
        self.assertEqual(local.calls, 50)

    def test_pacific_day_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "llm_usage.json")
            # 06:59 UTC 28 октября = 23:59 27 октября по тихоокеанскому (PDT, UTC-7): в UTC уже 28-е, у Gemini ещё 27-е
            now = datetime(2026, 10, 28, 6, 59, tzinfo=timezone.utc)
            usage = UsageTracker({"gemini": 200}, {"gemini": "America/Los_Angeles"}, path=path, now=lambda: now)
            usage.record("gemini")
            usage.record("gemini")
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data, {"gemini": {"date": "2026-10-27", "count": 2}})

    def test_day_boundary_is_pacific(self):
        now = [datetime(2026, 10, 27, 6, 59, tzinfo=timezone.utc)]  # 23:59 26 октября PDT
        usage = UsageTracker({"gemini": 200}, {"gemini": "America/Los_Angeles"}, now=lambda: now[0])
        usage.record("gemini")
        self.assertEqual(usage.count("gemini"), 1)
        now[0] = datetime(2026, 10, 27, 7, 1, tzinfo=timezone.utc)  # 00:01 27 октября PDT — новые сутки
        self.assertEqual(usage.count("gemini"), 0)

    def test_counter_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "llm_usage.json")
            now = lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
            first = UsageTracker({"groq": 800}, {"groq": "UTC"}, path=path, now=now)
            for _ in range(3):
                first.record("groq")
            second = UsageTracker({"groq": 800}, {"groq": "UTC"}, path=path, now=now)
            self.assertEqual(second.count("groq"), 3)
        print("✅ Лимиты: OK")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = Path(self._tmp.name) / ".env"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_only_env_file(self):
        self.env.write_text(f"GROQ_API_KEY={FAKE_KEY}\nLLM_PROVIDERS=groq,local\n", encoding="utf-8")
        with patch.dict(os.environ, {"GROQ_MODEL": "from-shell", "GEMINI_API_KEY": "shell-key-123456"}):
            config = load_config(self.env)
            self.assertNotIn("GROQ_API_KEY", os.environ)  # .env не попадает в окружение
        self.assertEqual(config["GROQ_MODEL"], "openai/gpt-oss-120b")  # не из окружения терминала
        self.assertNotIn("GEMINI_API_KEY", config)
        with patch('openai.OpenAI'):
            providers = build_providers(config)
        self.assertEqual([p.name for p in providers], ["groq", "local"])

    def test_missing_key_skips_provider(self):
        self.env.write_text("LLM_PROVIDERS=gemini,groq,local\nGROQ_API_KEY=\n", encoding="utf-8")
        with patch('openai.OpenAI'), self.assertLogs("core.llm.providers", level="WARNING") as logs:
            providers = build_providers(load_config(self.env))
        self.assertEqual([p.name for p in providers], ["local"])
        self.assertIn("GEMINI_API_KEY", logs.output[0])

    def test_provider_client_settings(self):
        with patch('openai.OpenAI') as MockOpenAI:
            OpenAICompatProvider("groq", "https://api.groq.com/openai/v1", "m", api_key=FAKE_KEY)
        kwargs = MockOpenAI.call_args.kwargs
        self.assertEqual((kwargs["timeout"], kwargs["max_retries"]), (15, 0))
        print("✅ Настройки: OK")


class TestSecrets(unittest.TestCase):
    def setUp(self):
        register_secret(FAKE_KEY)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.handler.addFilter(SecretFilter())
        root = logging.getLogger()
        self.old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.handler)

    def tearDown(self):
        logging.getLogger().removeHandler(self.handler)
        logging.getLogger().setLevel(self.old_level)

    def provider_leaking_key(self):
        """Настоящий провайдер; сервер «вернул» ошибку, в тексте которой есть ключ."""
        with patch('openai.OpenAI') as MockOpenAI:
            provider = OpenAICompatProvider("groq", "https://api.groq.com/openai/v1", "m", api_key=FAKE_KEY)
        client = MockOpenAI.return_value
        client.chat.completions.create.side_effect = openai.AuthenticationError(
            f"Invalid API key: {FAKE_KEY}", response=httpx.Response(401, request=REQUEST), body=None)
        client.models.list.side_effect = RuntimeError(f"key={FAKE_KEY}")
        return provider

    def test_key_not_in_log(self):
        provider = self.provider_leaking_key()
        response = chain(provider).chat(MSG)
        self.assertFalse(response.ok)
        # Даже если кто-то залогирует исключение целиком — фильтр уберёт ключ
        try:
            provider.chat(MSG)
        except Exception:
            logging.getLogger("test").exception("упало: %s", FAKE_KEY)
        logged = self.stream.getvalue()
        self.assertIn("ошибка настройки: ключ не подходит (401)", logged)
        self.assertIn("упало: ***", logged)
        self.assertNotIn(FAKE_KEY, logged)
        self.assertNotIn(FAKE_KEY, repr(provider))

    def test_key_not_in_check_output(self):
        provider = self.provider_leaking_key()
        out = io.StringIO()
        with patch('core.llm.check.build_providers', return_value=[provider]):
            check.main(["--models"], config={"LLM_PROVIDERS": "groq"}, out=out)
        printed = out.getvalue()
        self.assertIn("groq", printed)
        self.assertIn("ошибка", printed)
        self.assertNotIn(FAKE_KEY, printed)

    def test_key_not_in_usage_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "llm_usage.json")
            provider = self.provider_leaking_key()
            usage = UsageTracker({"groq": 800}, {"groq": "UTC"}, path=path)
            chain(provider, usage=usage).chat(MSG)
            self.assertNotIn(FAKE_KEY, Path(path).read_text(encoding="utf-8"))

    def test_describe_error_has_no_server_text(self):
        error = openai.AuthenticationError(f"bad {FAKE_KEY}", response=httpx.Response(401, request=REQUEST), body=None)
        self.assertNotIn(FAKE_KEY, describe_error(error))
        print("✅ Ключи не утекают: OK")


if __name__ == '__main__':
    unittest.main()
