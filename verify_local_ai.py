import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.ai import LocalAIClient

class TestLocalAIClient(unittest.TestCase):
    @patch('openai.OpenAI')
    def test_ask_success(self, MockOpenAI):
        # Mocking the client and response
        mock_client_instance = MockOpenAI.return_value
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Привет! Я работаю."
        mock_client_instance.chat.completions.create.return_value = mock_response

        client = LocalAIClient()
        response = client.ask("Привет")
        
        self.assertEqual(response, "Привет! Я работаю.")
        print("\n✅ Test ask success: Passed")

    @patch('openai.OpenAI')
    def test_ask_connection_error(self, MockOpenAI):
        import openai
        # Mocking the client to raise ConnectionError
        mock_client_instance = MockOpenAI.return_value
        mock_client_instance.chat.completions.create.side_effect = openai.APIConnectionError(request=MagicMock())

        client = LocalAIClient()
        response = client.ask("Привет")
        
        expected_error = "Ошибка: Локальный сервер Gemma не запущен. Пожалуйста, запустите LM Studio и нажмите Start Server."
        self.assertEqual(response, expected_error)
        print("\n✅ Test connection error: Passed")

if __name__ == '__main__':
    unittest.main()
