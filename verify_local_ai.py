import sys
import os
import unittest
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core.ai import AIClient
from core.llm import NO_CONNECTION_REPLY, LLMResponse


class TestAIClientAsk(unittest.TestCase):
    def _client_with_reply(self, response):
        llm = MagicMock()
        llm.chat.return_value = response
        return AIClient(llm=llm), llm

    def test_ask_success(self):
        content = '{"response": "Открываю Safari.", "actions": [{"name": "open_app", "args": {"app": "Safari"}}]}'
        client, llm = self._client_with_reply(LLMResponse(ok=True, text=content, provider="gemini"))

        response = client.ask("Открой Сафари")

        self.assertEqual(response, {
            "response": "Открываю Safari.",
            "actions": [{"name": "open_app", "args": {"app": "Safari"}}],
        })
        messages = llm.chat.call_args.args[0]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], "Открой Сафари")
        print("\n✅ Test ask success: Passed")

    def test_ask_plain_text(self):
        # Модель ответила не JSON — текст становится response, действий нет
        client, _ = self._client_with_reply(LLMResponse(ok=True, text="Привет! Я работаю."))

        response = client.ask("Привет")

        self.assertEqual(response, {"response": "Привет! Я работаю.", "actions": []})
        print("\n✅ Test plain text: Passed")

    def test_ask_all_providers_down(self):
        client, _ = self._client_with_reply(LLMResponse(ok=False, text=NO_CONNECTION_REPLY))

        response = client.ask("Привет")

        self.assertEqual(response, {"response": NO_CONNECTION_REPLY, "actions": []})
        print("\n✅ Test all providers down: Passed")

    def test_ask_unexpected_exception(self):
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("boom")
        response = AIClient(llm=llm).ask("Привет")

        self.assertEqual(response["actions"], [])
        self.assertIn("ошибка", response["response"])
        print("\n✅ Test unexpected exception: Passed")


if __name__ == '__main__':
    unittest.main()
