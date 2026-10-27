import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.ai import AIClient
from fixtures_apps import patch_installed
from main import parse_confirmation

_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()


class TestLocalAIActions(unittest.TestCase):
    def setUp(self):
        with patch('openai.OpenAI'):
            self.client = AIClient(llm=MagicMock())

    def test_parse_json(self):
        json_response = '{"response": "OK", "actions": [{"name": "open_app", "args": {"app": "Safari"}}]}'
        parsed = self.client._parse_json(json_response)
        self.assertEqual(parsed['actions'][0], {"name": "open_app", "args": {"app": "Safari"}})
        print("✅ JSON Parsing: OK")

    def test_dirty_json(self):
        # Test extracting JSON from markdown
        dirty_response = 'Here is the json:\n```json\n{"response": "Hi", "actions": []}\n```'
        parsed = self.client._parse_json(dirty_response)
        self.assertEqual(parsed['response'], "Hi")
        print("✅ Dirty JSON Parsing: OK")

    @patch('subprocess.run')
    def test_open_app_runs_without_shell(self, mock_run):
        mock_run.return_value.returncode = 0
        log = self.client.execute_actions([{"name": "open_app", "args": {"app": "Safari"}}])

        mock_run.assert_called_once_with(["open", "-a", "Safari"], capture_output=True, text=True, timeout=10)
        self.assertNotIn("shell", mock_run.call_args.kwargs)
        self.assertIn("✅", log)
        print("✅ open_app: OK")

    @patch('subprocess.run')
    def test_app_aliases(self, mock_run):
        mock_run.return_value.returncode = 0
        cases = [("калькулятор", "Calculator"), ("Сафари", "Safari"), ("Xcode", "Xcode"),
                 ("калькулятора", "Calculator"), ("файндер", "Finder"), ("Финдер", "Finder")]
        for app, expected in cases:
            mock_run.reset_mock()
            self.client.execute_actions([{"name": "open_app", "args": {"app": app}}])
            mock_run.assert_called_once_with(["open", "-a", expected], capture_output=True, text=True, timeout=10)

        # Короткий ключ «хром» — только точное совпадение; «хрома» не установлено -> отклонено
        mock_run.reset_mock()
        log = self.client.execute_actions([{"name": "open_app", "args": {"app": "хрома"}}])
        self.assertIn("не найдено", log)
        mock_run.assert_not_called()

        # quit_app тоже использует словарь
        mock_run.reset_mock()
        self.client.execute_actions([{"name": "quit_app", "args": {"app": "Сафари"}}])
        self.assertEqual(mock_run.call_args.args[0][-1], "Safari")
        print("✅ App aliases: OK")

    @patch('subprocess.run')
    def test_forbidden_action_rejected(self, mock_run):
        log = self.client.execute_actions([
            {"name": "run_shell", "args": {"command": "rm -rf ~"}},
            {"step": 1, "command": "rm -rf ~"},  # старый формат
            "rm -rf ~",
        ])

        mock_run.assert_not_called()
        self.assertEqual(log.count("⛔"), 3)
        print("✅ Forbidden action rejected: OK")

    @patch('subprocess.run')
    def test_invalid_app_name_rejected(self, mock_run):
        for app in ["", "   ", "-n", "--args", "Safari\nrm", "Safari\x00", None, 42]:
            for name in ("open_app", "quit_app"):
                log = self.client.execute_actions([{"name": name, "args": {"app": app}}])
                self.assertIn("⛔", log, f"{name}({app!r})")
        mock_run.assert_not_called()
        print("✅ Invalid app names rejected: OK")

    @patch('subprocess.run')
    def test_wrong_args_rejected(self, mock_run):
        log = self.client.execute_actions([
            {"name": "open_app", "args": {"command": "rm -rf ~"}},
            {"name": "open_app", "args": "Safari"},
        ])
        mock_run.assert_not_called()
        self.assertEqual(log.count("⛔"), 2)

    @patch('subprocess.run')
    def test_set_volume(self, mock_run):
        mock_run.return_value.returncode = 0
        self.client.execute_actions([{"name": "set_volume", "args": {"level": "50"}}])
        mock_run.assert_called_with(["osascript", "-e", "set volume output volume 50"],
                                    capture_output=True, text=True, timeout=10)
        self.client.execute_actions([{"name": "set_volume", "args": {"level": 150}}])
        mock_run.assert_called_with(["osascript", "-e", "set volume output volume 100"],
                                    capture_output=True, text=True, timeout=10)

        mock_run.reset_mock()
        for level in ["громко", None, "", True, [50], float("nan")]:
            log = self.client.execute_actions([{"name": "set_volume", "args": {"level": level}}])
            self.assertIn("⛔", log, repr(level))
        mock_run.assert_not_called()
        print("✅ set_volume: OK")

    @patch('subprocess.run')
    def test_open_url_only_http(self, mock_run):
        for url in ["file:///etc/passwd", "javascript:alert(1)", "-a Terminal", "https://", "ssh://host"]:
            log = self.client.execute_actions([{"name": "open_url", "args": {"url": url}}])
            self.assertIn("⛔", log, url)
        mock_run.assert_not_called()

        mock_run.return_value.returncode = 0
        # example.com не в доверенных — CONFIRM, выполняется только с confirmed=True
        self.client.execute_actions([{"name": "open_url", "args": {"url": "https://example.com"}}], confirmed=True)
        mock_run.assert_called_once_with(["open", "https://example.com"], capture_output=True, text=True, timeout=10)
        print("✅ open_url: OK")

    def test_split_actions(self):
        actions = [
            {"name": "open_app", "args": {"app": "Safari"}},
            {"name": "quit_app", "args": {"app": "Safari"}},
            {"name": "set_volume", "args": {"level": 30}},
            {"name": "open_url", "args": {"url": "https://example.com"}},
            {"name": "run_shell", "args": {}},
        ]
        safe, to_confirm, blocked = self.client.split_actions(actions)
        self.assertEqual([a["name"] for a in safe], ["open_app", "quit_app", "set_volume"])
        self.assertEqual([a["name"] for a in to_confirm], ["open_url"])
        self.assertEqual([a["name"] for a in blocked], ["run_shell"])
        self.assertEqual(self.client.split_actions("не список"), ([], [], []))
        print("✅ split_actions: OK")


class TestConfirmation(unittest.TestCase):
    def test_whole_words(self):
        self.assertTrue(parse_confirmation("Да"))
        self.assertTrue(parse_confirmation("подтверждаю"))
        self.assertTrue(parse_confirmation("ну выполняй уже"))
        self.assertFalse(parse_confirmation("нет"))
        self.assertFalse(parse_confirmation("Отмена!"))
        # Подстроки не считаются
        self.assertIsNone(parse_confirmation("когда"))
        self.assertIsNone(parse_confirmation("надо подумать"))
        self.assertIsNone(parse_confirmation("интернет"))
        # И «да», и «нет» — отмена
        self.assertFalse(parse_confirmation("да нет"))
        print("✅ Confirmation by whole words: OK")


if __name__ == '__main__':
    unittest.main()
