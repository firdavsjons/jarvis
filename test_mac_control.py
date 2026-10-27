import sys
import os
import unittest
from unittest.mock import patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from modules.mac_control import MacControl


@patch('modules.mac_control.subprocess.run')
class TestMacControl(unittest.TestCase):
    def setUp(self):
        self.mac = MacControl()

    def test_close_goes_to_ai(self, mock_run):
        self.assertIsNone(self.mac.execute("закрой калькулятор"))
        self.assertIsNone(self.mac.execute("Закрыть браузер"))
        mock_run.assert_not_called()

    def test_open_with_verb(self, mock_run):
        self.assertEqual(self.mac.execute("открой калькулятор"), "Калькулятор открыт")
        mock_run.assert_called_once_with(["open", "-a", "Calculator"], timeout=10)

    def test_app_without_verb_not_intercepted(self, mock_run):
        self.assertIsNone(self.mac.execute("калькулятор"))
        self.assertIsNone(self.mac.execute("найди в интернете погоду"))
        self.assertIsNone(self.mac.execute("какой браузер лучше"))
        mock_run.assert_not_called()

    def test_turn_off_music_not_opening_spotify(self, mock_run):
        self.assertIsNone(self.mac.execute("выключи музыку"))
        mock_run.assert_not_called()

    def test_turn_on_music_opens_spotify(self, mock_run):
        self.assertEqual(self.mac.execute("включи музыку"), "Открываю музыку")
        mock_run.assert_called_once_with(["open", "-a", "Spotify"], timeout=10)

    def test_open_browser(self, mock_run):
        self.assertEqual(self.mac.execute("открой браузер"), "Открываю браузер")
        mock_run.assert_called_once_with(["open", "https://www.google.com"], timeout=10)

    def test_volume(self, mock_run):
        self.assertEqual(self.mac.execute("сделай громче"), "Сделал громче")
        self.assertEqual(self.mac.execute("сделай потише"), "Сделал тише")
        self.assertEqual(self.mac.execute("выключи звук"), "Звук выключен")
        self.assertEqual(self.mac.execute("включи звук"), "Звук включен")
        self.assertEqual(mock_run.call_count, 4)
        mute_args = mock_run.call_args_list[2].args[0]
        self.assertEqual(mute_args, ["osascript", "-e", "set volume output muted true"])

    def test_substrings_do_not_match(self, mock_run):
        # «тише» внутри другого слова, «звук» без глагола
        for text in ["затишье", "звуковая карта", "привет"]:
            self.assertIsNone(self.mac.execute(text), text)
        mock_run.assert_not_called()
        print("✅ MacControl: OK")


if __name__ == '__main__':
    unittest.main()
