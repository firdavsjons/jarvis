"""Этап 8.1.1: фразу не теряем при временных сбоях. Повтор после 500/502/503/504, ожидание окна токенов (≤ 12 с),
«Секунду.» один раз за фразу. Часы и паузы подменены, провайдеры фальшивые, сети нет."""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import httpx
import openai

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core.ai import WAIT_REPLY, AIClient
from core.llm import NO_CONNECTION_REPLY, FallbackClient, LLMClient, LLMResponse
from core.llm.router import (COOLDOWN_SECONDS, SERVER_RETRY_DELAY, SERVER_RETRY_STATUSES, TPM_MARGIN, TPM_MAX_WAIT,
                             TPM_REPLY, is_transient_server_error)
from core.llm.usage import TPM_WINDOW, UsageTracker
from test_tools import final

REQUEST = httpx.Request("POST", "https://x")


def status_error(code):
    cls = {500: openai.InternalServerError, 429: openai.RateLimitError, 400: openai.BadRequestError,
           401: openai.AuthenticationError, 404: openai.NotFoundError}.get(code, openai.APIStatusError)
    if code in (502, 503, 504):
        cls = openai.InternalServerError
    return cls(str(code), response=httpx.Response(code, request=REQUEST), body=None)


def no_network():
    return openai.APIConnectionError(request=REQUEST)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Provider(LLMClient):
    """Отвечает по сценарию: исключение — бросает, иначе ответ. Сценарий закончился — последний элемент повторяется."""

    def __init__(self, name, script=("ok",), tpm=None):
        self.name, self.model, self.daily_limit, self.timezone = name, "m", None, "UTC"
        self.tpm_limit, self.max_tokens = tpm, None
        self.script, self.calls = list(script), 0

    def chat(self, messages, tools=None):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return LLMResponse(ok=True, text=step, provider=self.name)


class Case(unittest.TestCase):
    def setUp(self):
        self.clock, self.sleeps, self.waits = Clock(), [], []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.t += seconds

    def client(self, *providers):
        client = FallbackClient(list(providers), clock=self.clock, sleep=self.sleep)
        client.on_wait = lambda: self.waits.append(self.clock.t)
        return client


def big(tokens):
    return [{"role": "user", "content": "я" * int(tokens * 3.5)}]


class TestServerErrorRetry(Case):
    def test_503_then_success_retries_same_provider_after_2_seconds(self):
        groq, gemini = Provider("groq", [status_error(503), "ok"]), Provider("gemini")
        client = self.client(groq, gemini)
        with self.assertLogs("core.llm.router", level="INFO"):
            response = client.chat([])
        self.assertEqual((response.ok, response.provider), (True, "groq"))
        self.assertEqual(self.sleeps, [2])
        self.assertEqual((groq.calls, gemini.calls), (2, 0))
        self.assertNotIn("groq", client._down_until)            # пауза не поставлена
        self.assertEqual(SERVER_RETRY_DELAY, 2)

    def test_503_twice_goes_to_next_provider_and_pauses_the_first(self):
        groq, gemini = Provider("groq", [status_error(503)]), Provider("gemini")
        client = self.client(groq, gemini)
        with self.assertLogs("core.llm.router", level="INFO"):
            response = client.chat([])
        self.assertEqual(response.provider, "gemini")
        self.assertEqual(self.sleeps, [2])                       # один повтор, не больше
        self.assertEqual((groq.calls, gemini.calls), (2, 1))
        self.assertEqual(client._down_until["groq"] - self.clock.t, COOLDOWN_SECONDS)

    def test_pause_is_set_after_the_second_failure_not_the_first(self):
        seen = []

        class Watching(Provider):
            def chat(inner, messages, tools=None):
                seen.append(dict(client._down_until))
                return super().chat(messages, tools)

        groq = Watching("groq", [status_error(502), status_error(502)])
        client = self.client(groq, Provider("gemini"))
        with self.assertLogs("core.llm.router", level="INFO"):
            client.chat([])
        self.assertEqual(seen, [{}, {}])                         # ни до первого, ни до повтора пауза не стояла

    def test_every_transient_status_is_retried(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                self.setUp()
                groq = Provider("groq", [status_error(code), "ok"])
                with self.assertLogs("core.llm.router", level="INFO"):
                    self.assertEqual(self.client(groq).chat([]).provider, "groq")
                self.assertEqual((groq.calls, self.sleeps), (2, [2]))
        self.assertEqual(SERVER_RETRY_STATUSES, (500, 502, 503, 504))

    def test_other_errors_are_not_retried(self):
        for error in (status_error(400), status_error(401), status_error(404), status_error(429), no_network(),
                      openai.APITimeoutError(request=REQUEST), ValueError("x")):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                groq = Provider("groq", [error])
                with self.assertLogs("core.llm.router", level="WARNING"):
                    self.client(groq, Provider("gemini")).chat([])
                self.assertEqual((groq.calls, self.sleeps, self.waits), (1, [], []))

    def test_is_transient_server_error(self):
        self.assertTrue(is_transient_server_error(status_error(503)))
        self.assertFalse(is_transient_server_error(status_error(429)))
        self.assertFalse(is_transient_server_error(no_network()))
        self.assertFalse(is_transient_server_error(RuntimeError("503")))

    def test_both_requests_are_counted(self):
        groq = Provider("groq", [status_error(503), "ok"])
        client = self.client(groq)
        with self.assertLogs("core.llm.router", level="INFO"):
            client.chat([])
        self.assertEqual(client.usage._entry("groq")["count"], 2)

    def test_both_requests_count_towards_tpm_window(self):
        groq = Provider("groq", [status_error(503), "ok"], tpm=100000)
        client = self.client(groq)
        with self.assertLogs("core.llm.router", level="INFO"):
            client.chat(big(100))
        self.assertGreaterEqual(client.usage.tokens_last_minute("groq"), 2 * 100)

    def test_retry_success_clears_rate_limit_history(self):
        groq = Provider("groq", [status_error(504), "ok"])
        client = self.client(groq)
        with self.assertLogs("core.llm.router", level="INFO"):
            client.chat([])
        self.assertEqual(client._rate_limits, {})

    def test_second_failure_log_names_the_error(self):
        groq = Provider("groq", [status_error(503)])
        client = self.client(groq, Provider("gemini"))
        with self.assertLogs("core.llm.router", level="WARNING") as logs:
            client.chat([])
        text = "\n".join(logs.output)
        self.assertIn("ошибка сервера (503)", text)
        self.assertIn("повторяю тот же запрос через 2 с", text)
        self.assertIn("переключаюсь на следующего", text)

    def test_all_providers_503_means_no_connection(self):
        client = self.client(Provider("groq", [status_error(503)]), Provider("gemini", [status_error(500)]))
        with self.assertLogs("core.llm.router", level="WARNING"):
            response = client.chat([])
        self.assertEqual(response.text, NO_CONNECTION_REPLY)
        self.assertEqual(self.sleeps, [2, 2])


class TestTpmWait(Case):
    def client_with(self, *providers):
        return self.client(*providers)

    def busy(self, client, name, tokens, ago):
        """Окно токенов занято: запись в tokens токенов сделана ago секунд назад."""
        client.usage.record_tokens(name, tokens)
        self.clock.t += ago

    def test_all_on_tpm_waits_for_the_window_and_succeeds(self):
        groq = Provider("groq", tpm=1000)
        client = self.client_with(groq)
        self.busy(client, "groq", 800, ago=53)                   # запись выйдет из окна через 7 секунд
        response = client.chat(big(300))
        self.assertTrue(response.ok)
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 7 + TPM_MARGIN, places=3)
        self.assertLessEqual(self.sleeps[0], TPM_MAX_WAIT)
        self.assertEqual(groq.calls, 1)

    def test_wait_of_exactly_12_seconds_is_allowed(self):
        groq = Provider("groq", tpm=1000)
        client = self.client_with(groq)
        self.busy(client, "groq", 800, ago=48)
        self.assertTrue(client.chat(big(300)).ok)
        self.assertEqual(len(self.sleeps), 1)

    def test_wait_longer_than_12_seconds_says_too_many_requests_without_waiting(self):
        groq = Provider("groq", tpm=1000)
        client = self.client_with(groq)
        self.busy(client, "groq", 800, ago=40)                   # до освобождения 20 секунд
        response = client.chat(big(300))
        self.assertFalse(response.ok)
        self.assertEqual(response.text, TPM_REPLY)
        self.assertEqual(response.text, "Слишком много запросов подряд, подождите минуту.")
        self.assertEqual((self.sleeps, groq.calls, self.waits), ([], 0, []))

    def test_nearest_provider_decides(self):
        groq, gemini = Provider("groq", tpm=1000), Provider("gemini", tpm=1000)
        client = self.client_with(groq, gemini)
        client.usage.record_tokens("groq", 800)
        self.clock.t += 30                                       # у Groq освободится через 30 с
        client.usage.record_tokens("gemini", 800)
        self.clock.t += 20                                       # у Gemini — через 40 с, у Groq — через 10 с
        self.assertTrue(client.chat(big(300)).ok)
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 10.1, places=3)
        self.assertEqual((groq.calls, gemini.calls), (1, 0))

    def test_retry_only_once(self):
        groq = Provider("groq", tpm=1000)
        client = self.client_with(groq)
        self.busy(client, "groq", 800, ago=55)
        client.usage.record_tokens("groq", 800)                  # свежая запись: после ожидания окно снова занято
        response = client.chat(big(300))
        self.assertEqual(len(self.sleeps), 0)                    # 60 с > 12 с: даже не ждём
        self.assertEqual(response.text, TPM_REPLY)

    def test_tpm_plus_network_error_provider_means_no_connection(self):
        groq, gemini = Provider("groq", tpm=1000), Provider("gemini", [no_network()])
        client = self.client_with(groq, gemini)
        client._down_until["gemini"], client._down_reason["gemini"] = self.clock.t + 60, "error"   # Gemini в паузе: сеть
        self.busy(client, "groq", 800, ago=20)                   # у Groq окно освободится через 40 с
        with self.assertLogs("core.llm.router", level="WARNING"):
            response = client.chat(big(300))
        self.assertEqual(response.text, NO_CONNECTION_REPLY)     # ждать нечего: второй недоступен, первый — надолго
        self.assertEqual(self.sleeps, [])

    def test_tpm_plus_network_error_provider_still_waits_if_window_frees_soon(self):
        groq, gemini = Provider("groq", ["ok"], tpm=1000), Provider("gemini", [no_network()])
        client = self.client_with(groq, gemini)
        client._down_until["gemini"], client._down_reason["gemini"] = self.clock.t + 60, "error"
        self.busy(client, "groq", 800, ago=55)
        self.assertTrue(client.chat(big(300)).ok)
        self.assertEqual(len(self.sleeps), 1)

    def test_tpm_plus_daily_limit_is_still_too_many_requests(self):
        groq = Provider("groq", tpm=1000)
        gemini = Provider("gemini")
        gemini.daily_limit = 10
        client = FallbackClient([groq, gemini], clock=self.clock, sleep=self.sleep,
                                usage=UsageTracker({"gemini": 10}, {}, tpm_limits={"groq": 1000}, clock=self.clock))
        for _ in range(10):
            client.usage.record("gemini")
        self.busy(client, "groq", 800, ago=10)
        self.assertEqual(client.chat(big(300)).text, TPM_REPLY)

    def test_no_network_at_all_says_no_connection(self):
        client = self.client_with(Provider("groq", [no_network()]), Provider("gemini", [no_network()]))
        with self.assertLogs("core.llm.router", level="WARNING"):
            response = client.chat([])
        self.assertEqual(response.text, NO_CONNECTION_REPLY)
        self.assertEqual(self.sleeps, [])

    def test_seconds_until_fits(self):
        usage = UsageTracker({}, {}, tpm_limits={"groq": 1000}, clock=self.clock)
        self.assertEqual(usage.seconds_until_fits("groq", 100), 0.0)
        self.assertIsNone(usage.seconds_until_fits("local", 100))
        usage.record_tokens("groq", 500)                          # t = 0
        self.clock.t += 10
        usage.record_tokens("groq", 400)                          # t = 10
        self.assertEqual(usage.seconds_until_fits("groq", 0), 0.0)
        self.assertEqual(usage.seconds_until_fits("groq", 400), TPM_WINDOW - 10)    # хватит, когда уйдёт первая запись
        self.assertEqual(usage.seconds_until_fits("groq", 900), TPM_WINDOW - 10 + 10)    # нужны обе записи


class TestWaitNotice(Case):
    def test_notice_on_server_error_retry(self):
        client = self.client(Provider("groq", [status_error(503), "ok"]))
        with self.assertLogs("core.llm.router", level="INFO"):
            client.chat([])
        self.assertEqual(len(self.waits), 1)

    def test_notice_on_tpm_wait(self):
        groq = Provider("groq", tpm=1000)
        client = self.client(groq)
        client.usage.record_tokens("groq", 800)
        self.clock.t += 55
        client.chat(big(300))
        self.assertEqual(len(self.waits), 1)

    def test_no_notice_without_waiting(self):
        client = self.client(Provider("groq"))
        client.chat([])
        self.assertEqual(self.waits, [])

    def test_broken_callback_does_not_break_the_request(self):
        client = self.client(Provider("groq", [status_error(503), "ok"]))
        client.on_wait = MagicMock(side_effect=RuntimeError("озвучка упала"))
        with self.assertLogs("core.llm.router", level="INFO"):
            self.assertTrue(client.chat([]).ok)


class FlakyLLM(LLMClient):
    """FallbackClient-подобный: при вызове chat сообщает, что ждёт, и отвечает."""
    name = "flaky"

    def __init__(self, waits=1):
        self.on_wait, self.waits = None, waits

    def chat(self, messages, tools=None, json_messages=None):
        for _ in range(self.waits):
            self.on_wait()
        return LLMResponse(ok=True, text="Готово.", provider="flaky")


class TestAiSaysSecond(unittest.TestCase):
    def make(self, waits=1):
        llm = FlakyLLM(waits)
        client = AIClient(llm=llm)
        said, event = [], threading.Event()
        client.on_slow_tool = lambda text: (said.append(text), event.set())
        return client, said, event

    def test_second_is_said_once_per_phrase_in_a_thread(self):
        client, said, event = self.make(waits=3)
        client.respond("расскажи про луну")
        self.assertTrue(event.wait(2))
        self.assertEqual(said, [WAIT_REPLY])
        self.assertEqual(WAIT_REPLY, "Секунду.")

    def test_next_phrase_may_say_it_again(self):
        client, said, event = self.make()
        client.respond("расскажи про луну")
        self.assertTrue(event.wait(2))
        event.clear()
        client.respond("расскажи про солнце")
        self.assertTrue(event.wait(2))
        self.assertEqual(said, [WAIT_REPLY, WAIT_REPLY])

    def test_speech_does_not_block_the_request(self):
        client, _, _ = self.make()
        release, started = threading.Event(), threading.Event()
        client.on_slow_tool = lambda text: (started.set(), release.wait(5))
        try:
            turn = client.respond("расскажи про луну")          # вернулась, хотя «озвучка» ещё идёт
            self.assertEqual(turn.speech, "Готово.")
            self.assertTrue(started.wait(2))
        finally:
            release.set()

    def test_without_callback_nothing_happens(self):
        llm = FlakyLLM()
        client = AIClient(llm=llm)
        self.assertEqual(client.respond("расскажи про луну").speech, "Готово.")

    def test_ai_client_hooks_into_fallback_client(self):
        fallback = FallbackClient([Provider("groq")])
        AIClient(llm=fallback)
        self.assertIsNotNone(fallback.on_wait)


class TestSleepIsInstantInTests(unittest.TestCase):
    def test_default_sleep_does_not_really_sleep(self):
        client = FallbackClient([Provider("groq", [status_error(503), "ok"])])
        import time
        started = time.monotonic()
        with self.assertLogs("core.llm.router", level="INFO"):
            client.chat([])
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
