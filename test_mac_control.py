import sys
import os
import unittest
from unittest.mock import patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from modules.mac_control import MacControl

RUN_KWARGS = {"capture_output": True, "text": True, "timeout": 10}


class TestMacControl(unittest.TestCase):
    def setUp(self):
        self.mac = MacControl()
        patcher = patch('modules.mac_control.subprocess.run')
        self.mock_run = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_run.return_value.returncode = 0
        self.mock_run.return_value.stderr = ""

    def test_close_goes_to_ai(self):
        self.assertIsNone(self.mac.execute("закрой калькулятор"))
        self.assertIsNone(self.mac.execute("Закрыть браузер"))
        self.mock_run.assert_not_called()

    def test_open_with_verb(self):
        self.assertEqual(self.mac.execute("открой калькулятор"), "Калькулятор открыт")
        self.mock_run.assert_called_once_with(["open", "-a", "Calculator"], **RUN_KWARGS)

    def test_filler_words_allowed(self):
        self.assertEqual(self.mac.execute("А открой мне калькулятор, пожалуйста"), "Калькулятор открыт")

    def test_app_without_verb_not_intercepted(self):
        self.assertIsNone(self.mac.execute("калькулятор"))
        self.assertIsNone(self.mac.execute("найди в интернете погоду"))
        self.assertIsNone(self.mac.execute("какой браузер лучше"))
        self.mock_run.assert_not_called()

    def test_long_phrases_go_to_ai(self):
        for text in ["открой YouTube и найди музыка", "включи музыку miyagi", "открой калькулятор и посчитай 2+2",
                     "открой браузер и найди погоду", "открой спотифай и выключи звук",
                     "сделай громче и открой ютуб", "выключи звук в телеграме"]:
            with self.subTest(text=text):
                self.assertIsNone(self.mac.execute(text))
        self.mock_run.assert_not_called()

    def test_turn_off_music_not_opening_spotify(self):
        self.assertIsNone(self.mac.execute("выключи музыку"))
        self.mock_run.assert_not_called()

    def test_turn_on_music_opens_spotify(self):
        self.assertEqual(self.mac.execute("включи музыку"), "Открываю музыку")
        self.mock_run.assert_called_once_with(["open", "-a", "Spotify"], **RUN_KWARGS)

    def test_missing_app_returns_none(self):
        # Spotify нет: open -a завершается с ошибкой -> не врём «Открываю», отдаём нейросети
        self.mock_run.return_value.returncode = 1
        self.mock_run.return_value.stderr = "Unable to find application named 'Spotify'"
        with self.assertLogs("modules.mac_control", level="WARNING") as logs:
            self.assertIsNone(self.mac.execute("включи музыку"))
        self.assertIn("Unable to find application", logs.output[0])

    def test_open_browser(self):
        self.assertEqual(self.mac.execute("открой браузер"), "Открываю браузер")
        self.mock_run.assert_called_once_with(["open", "https://www.google.com"], **RUN_KWARGS)

    def test_volume(self):
        self.assertEqual(self.mac.execute("сделай громче"), "Сделал громче")
        self.assertEqual(self.mac.execute("сделай потише, пожалуйста"), "Сделал тише")
        self.assertEqual(self.mac.execute("выключи звук"), "Звук выключен")
        self.assertEqual(self.mac.execute("включи звук"), "Звук включен")
        self.assertEqual(self.mac.execute("ещё чуть громче"), "Сделал громче")
        self.assertEqual(self.mock_run.call_count, 5)
        mute_args = self.mock_run.call_args_list[2].args[0]
        self.assertEqual(mute_args, ["osascript", "-e", "set volume output muted true"])

    def test_volume_failure_returns_none(self):
        self.mock_run.return_value.returncode = 1
        self.assertIsNone(self.mac.execute("сделай громче"))

    def test_substrings_do_not_match(self):
        # «тише» внутри другого слова, «звук» без глагола
        for text in ["затишье", "звуковая карта", "привет"]:
            self.assertIsNone(self.mac.execute(text), text)
        self.mock_run.assert_not_called()
        print("✅ MacControl: OK")


if __name__ == '__main__':
    unittest.main()
