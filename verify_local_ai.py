import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.ai import LocalAIClient


class TestLocalAIClient(unittest.TestCase):
    def _client_with_reply(self, MockOpenAI, content):
        mock_response = MagicMock()
        mock_response.choices[0].message.content = content
        MockOpenAI.return_value.chat.completions.create.return_value = mock_response
        return LocalAIClient()

    @patch('openai.OpenAI')
    def test_ask_success(self, MockOpenAI):
        content = '{"response": "Открываю Safari.", "actions": [{"name": "open_app", "args": {"app": "Safari"}}]}'
        client = self._client_with_reply(MockOpenAI, content)

        response = client.ask("Открой Сафари")

        self.assertEqual(response, {
            "response": "Открываю Safari.",
            "actions": [{"name": "open_app", "args": {"app": "Safari"}}],
        })
        print("\n✅ Test ask success: Passed")

    @patch('openai.OpenAI')
    def test_ask_plain_text(self, MockOpenAI):
        # Модель ответила не JSON — текст становится response, действий нет
        client = self._client_with_reply(MockOpenAI, "Привет! Я работаю.")

        response = client.ask("Привет")

        self.assertEqual(response, {"response": "Привет! Я работаю.", "actions": []})
        print("\n✅ Test plain text: Passed")

    @patch('openai.OpenAI')
    def test_ask_connection_error(self, MockOpenAI):
        import openai
        # Mocking the client to raise ConnectionError
        mock_client_instance = MockOpenAI.return_value
        mock_client_instance.chat.completions.create.side_effect = openai.APIConnectionError(request=MagicMock())

        client = LocalAIClient()
        response = client.ask("Привет")

        self.assertIsInstance(response, dict)
        self.assertEqual(response["actions"], [])
        self.assertIn("LM Studio", response["response"])
        print("\n✅ Test connection error: Passed")


if __name__ == '__main__':
    unittest.main()
