"""Лимит токенов в минуту: скользящее окно, пропуск на 90%, ожидание 10 с, итоговая фраза. Часы подменены."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core.llm import FallbackClient, LLMClient, LLMResponse
from core.llm.providers import DEFAULTS, build_providers, load_config
from core.llm.router import TPM_REPLY, TPM_WAIT
from core.llm.usage import UsageTracker, estimate_tokens


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Provider(LLMClient):
    def __init__(self, name, tpm=None, max_tokens=None):
        self.name, self.model, self.daily_limit, self.timezone = name, "m", None, "UTC"
        self.tpm_limit, self.max_tokens = tpm, max_tokens
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return LLMResponse(ok=True, text="ok", provider=self.name)


def big(tokens):
    """Сообщение, которое оценивается примерно в tokens токенов."""
    return [{"role": "user", "content": "я" * int(tokens * 3.5)}]


class TestEstimate(unittest.TestCase):
    def test_chars_over_3_5_plus_max_tokens(self):
        messages = [{"role": "user", "content": "я" * 350}]
        base = estimate_tokens(messages)
        self.assertAlmostEqual(base, len(json.dumps(messages, ensure_ascii=False)) / 3.5, delta=1)
        self.assertEqual(estimate_tokens(messages, max_tokens=500), base + 500)
        tools = [{"type": "function", "function": {"name": "x" * 100}}]
        self.assertGreater(estimate_tokens(messages, tools), base)


class TestWindow(unittest.TestCase):
    def test_sliding_window_forgets_after_60_seconds(self):
        clock = Clock()
        usage = UsageTracker({}, {}, tpm_limits={"groq": 1000}, clock=clock)
        usage.record_tokens("groq", 600)
        self.assertEqual(usage.tokens_last_minute("groq"), 600)
        clock.t += 59
        self.assertEqual(usage.tokens_last_minute("groq"), 600)
        clock.t += 1.5
        self.assertEqual(usage.tokens_last_minute("groq"), 0)

    def test_90_percent_threshold(self):
        clock = Clock()
        usage = UsageTracker({}, {}, tpm_limits={"groq": 1000}, clock=clock)
        usage.record_tokens("groq", 500)
        self.assertFalse(usage.tpm_exceeded("groq", 400))   # 900 из 1000: ровно 90% — ещё можно
        self.assertTrue(usage.tpm_exceeded("groq", 401))
        clock.t += 61
        self.assertFalse(usage.tpm_exceeded("groq", 401))   # окно очистилось

    def test_no_limit_and_empty_window(self):
        usage = UsageTracker({}, {}, tpm_limits={"groq": 1000, "local": None}, clock=Clock())
        self.assertFalse(usage.tpm_exceeded("local", 10 ** 9))
        self.assertFalse(usage.tpm_exceeded("unknown", 10 ** 9))
        self.assertFalse(usage.tpm_exceeded("groq", 5000))   # пустое окно: ожидание не поможет
        usage.record_tokens("local", 100)
        self.assertEqual(usage.tokens_last_minute("local"), 0)   # без лимита не копим


class TestRouter(unittest.TestCase):
    def client(self, providers, clock, sleeps=None):
        def sleep(seconds):
            sleeps.append(seconds)
            clock.t += seconds
        return FallbackClient(providers, clock=clock, sleep=sleep)

    def test_skips_provider_over_tpm_and_goes_next(self):
        clock, sleeps = Clock(), []
        groq, gemini = Provider("groq", tpm=1000), Provider("gemini", tpm=200000)
        client = self.client([groq, gemini], clock, sleeps)
        self.assertEqual(client.chat(big(500)).provider, "groq")
        with self.assertLogs("core.llm.router", level="WARNING") as logs:
            response = client.chat(big(500))   # 500 + 500 > 900 -> Groq пропускаем
        self.assertEqual(response.provider, "gemini")
        self.assertEqual(logs.output, ["WARNING:core.llm.router:лимит токенов в минуту, пропускаю Groq"])
        self.assertEqual((groq.calls, gemini.calls), (1, 1))
        self.assertEqual(sleeps, [])

    def test_groq_comes_back_after_a_minute(self):
        clock = Clock()
        groq, gemini = Provider("groq", tpm=1000), Provider("gemini")
        client = self.client([groq, gemini], clock, [])
        client.chat(big(600))
        clock.t += 61
        self.assertEqual(client.chat(big(600)).provider, "groq")

    def test_all_over_tpm_waits_10_seconds_and_retries_once(self):
        clock, sleeps = Clock(), []
        groq = Provider("groq", tpm=1000)
        client = self.client([groq], clock, sleeps)
        client.usage.record_tokens("groq", 800)
        # 10 секунд мало: окно ещё держит 800 токенов -> «Слишком много запросов подряд»
        response = client.chat(big(300))
        self.assertEqual(sleeps, [TPM_WAIT])
        self.assertEqual(TPM_WAIT, 10)
        self.assertFalse(response.ok)
        self.assertEqual(response.text, "Слишком много запросов подряд, подождите минуту.")
        self.assertEqual(response.text, TPM_REPLY)
        self.assertEqual(groq.calls, 0)

    def test_wait_helps_when_window_expires(self):
        clock, sleeps = Clock(), []
        groq = Provider("groq", tpm=1000)
        client = self.client([groq], clock, sleeps)
        client.usage.record_tokens("groq", 800)
        clock.t += 55                      # до конца окна осталось 5 секунд, ждём 10
        response = client.chat(big(300))
        self.assertEqual(sleeps, [TPM_WAIT])
        self.assertTrue(response.ok)
        self.assertEqual(groq.calls, 1)

    def test_no_wait_when_not_all_on_tpm(self):
        clock, sleeps = Clock(), []
        client = self.client([Provider("groq", tpm=1000), Provider("gemini")], clock, sleeps)
        client.chat(big(600))
        self.assertTrue(client.chat(big(600)).ok)   # ушли на Gemini без ожидания
        self.assertEqual(sleeps, [])

    def test_tokens_recorded_only_for_the_provider_that_was_asked(self):
        clock = Clock()
        client = self.client([Provider("groq", tpm=1000), Provider("gemini", tpm=2000)], clock, [])
        client.chat(big(600))
        client.chat(big(600))
        self.assertGreaterEqual(client.usage.tokens_last_minute("groq"), 600)
        self.assertGreaterEqual(client.usage.tokens_last_minute("gemini"), 600)

    def test_max_tokens_counted(self):
        clock = Clock()
        groq = Provider("groq", tpm=1000, max_tokens=800)
        client = self.client([groq], clock, [])
        client.usage.record_tokens("groq", 50)
        response = client.chat(big(100))   # 50 + 100 + 800 > 900
        self.assertFalse(response.ok)

    def test_json_mode_uses_json_messages_size(self):
        clock = Clock()
        groq = Provider("groq", tpm=1000)
        groq.supports_tools = False
        client = self.client([groq], clock, [])
        client.chat(big(2000), tools=[{"type": "function"}], json_messages=big(100))
        self.assertLess(client.usage.tokens_last_minute("groq"), 300)


class TestConfig(unittest.TestCase):
    def test_defaults_and_env(self):
        self.assertEqual(DEFAULTS["GROQ_TPM_LIMIT"], "8000")
        self.assertEqual(DEFAULTS["GEMINI_TPM_LIMIT"], "200000")
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("GEMINI_API_KEY=k1\nGROQ_API_KEY=k2\nGROQ_TPM_LIMIT=6000\nGEMINI_TPM_LIMIT=abc\n")
            providers = {p.name: p for p in build_providers(load_config(env))}
        self.assertEqual(providers["groq"].tpm_limit, 6000)
        self.assertEqual(providers["gemini"].tpm_limit, 200000)   # не число — по умолчанию

    def test_env_example_has_settings(self):
        text = (Path(__file__).parent / ".env.example").read_text(encoding="utf-8")
        self.assertIn("GROQ_TPM_LIMIT=8000", text)
        self.assertIn("GEMINI_TPM_LIMIT=200000", text)
        self.assertIn("console.groq.com", text)


if __name__ == "__main__":
    unittest.main()
