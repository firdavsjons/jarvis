import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import openai

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core import installed_apps
from core.ai import AIClient
from core.cache import CommandCache
from core.llm import NO_CONNECTION_REPLY, FallbackClient, LLMClient
from core.llm.providers import DEFAULTS, build_providers, load_config
from fixtures_apps import patch_installed
from modules.mac_control import MacControl
import main

_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()
    installed_apps.set_user_aliases({})


class NoNetwork(LLMClient):
    """Облачный провайдер без интернета."""

    def __init__(self, name):
        self.name, self.model, self.daily_limit, self.timezone = name, "m", None, "UTC"
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://x"))


class TestLocalDisabledByDefault(unittest.TestCase):
    def test_default_chain_is_cloud_only(self):
        self.assertEqual(DEFAULTS["LLM_PROVIDERS"], "gemini,groq")
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("GEMINI_API_KEY=test-key-1234567890\nGROQ_API_KEY=test-key-0987654321\n", encoding="utf-8")
            with patch('openai.OpenAI'):
                self.assertEqual([p.name for p in build_providers(load_config(env))], ["gemini", "groq"])
                # Код local сохранён — включается строкой в .env
                env.write_text(env.read_text(encoding="utf-8") + "LLM_PROVIDERS=gemini,groq,local\n", encoding="utf-8")
                self.assertEqual([p.name for p in build_providers(load_config(env))], ["gemini", "groq", "local"])
        print("✅ Локальная модель выключена по умолчанию: OK")


class TestWithoutInternet(unittest.TestCase):
    """Оба облачных провайдера недоступны: простые команды работают, остальное — «Нет связи»."""

    def setUp(self):
        for target in ('core.ai.subprocess.run', 'modules.mac_control.subprocess.run'):
            patcher = patch(target)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            mock.return_value.returncode = 0
            mock.return_value.stderr = ""
        self.gemini, self.groq = NoNetwork("gemini"), NoNetwork("groq")
        cache = CommandCache()
        cache.store("открой telegram", [{"name": "open_app", "args": {"app": "Telegram"}}])
        self.app = MagicMock()
        self.app.ai = AIClient(llm=FallbackClient([self.gemini, self.groq]), cache=cache)
        self.app.mac = MacControl()
        self.app.has_pending.return_value = False
        self.app.report_result = lambda results: main.VoiceApp.report_result(self.app, results)
        self.app.deliver = lambda turn, text: main.VoiceApp.deliver(self.app, turn, text)

    def say(self, text):
        self.app.engine.speak.reset_mock()
        main.VoiceApp.process_command(self.app, text)
        return [c.args[0] for c in self.app.engine.speak.call_args_list]

    def test_short_commands_work_offline(self):
        self.assertEqual(self.say("сделай громче"), ["Сделал громче"])
        self.assertEqual(self.say("открой калькулятор"), ["Калькулятор открыт"])
        self.assertEqual(self.say("Открой, пожалуйста, Telegram!"), ["Открыто приложение Telegram."])
        self.assertEqual(self.say("забудь разговор"), ["Хорошо, разговор забыт."])
        self.assertEqual(self.gemini.calls + self.groq.calls, 0)  # модель не понадобилась

    def test_model_requests_say_no_connection(self):
        self.assertEqual(self.say("расскажи анекдот"), [NO_CONNECTION_REPLY])
        self.assertEqual((self.gemini.calls, self.groq.calls), (1, 1))
        print("✅ Без интернета: OK")


if __name__ == '__main__':
    unittest.main()
