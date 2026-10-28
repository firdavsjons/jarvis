import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import journal
from modules.mac_control import MacControl
from fixtures_apps import patch_installed

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


PMSET = " -InternalBattery-0 (id=1)\t62%; discharging; 3:05 remaining present: true\n"


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestQuickRules(unittest.TestCase):
    """Быстрые правила Этапа 6.1: без модели, по целым словам, с записью в журнал."""

    def setUp(self):
        self.mac = MacControl()
        installed = patch_installed()
        installed.start()
        self.addCleanup(installed.stop)
        # subprocess один и тот же модуль и в core.mac_actions, и в modules.mac_control: достаточно одной подмены
        patcher = patch("core.mac_actions.subprocess.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.return_value = completed(PMSET)

    def test_battery_without_model(self):
        for phrase in ["какой заряд", "Сколько заряда?", "процент зарядки"]:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.mac.execute(phrase), "Заряд 62 процента, не заряжается, хватит на 3 часа 5 минут.")
        self.assertEqual(self.mac.execute("пожалуйста, какой заряд"),
                         "Заряд 62 процента, не заряжается, хватит на 3 часа 5 минут.")
        self.assertEqual(self.run.call_args.args[0], ["pmset", "-g", "batt"])

    def test_extra_words_go_to_model(self):
        for phrase in ["какой заряд у телефона", "закрой Safari", "пауза и следующий трек", "какой заряд и погода",
                       "скриншот экрана и отправь маме", "включи тёмную тему в браузере", "заблокируй экран через час",
                       "что играет в Spotify", "играй музыку"]:
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.mac.execute(phrase))
        self.run.assert_not_called()

    def test_close_safari_not_intercepted(self):
        self.assertIsNone(self.mac.execute("закрой Safari"))
        self.run.assert_not_called()

    def test_screenshot_lock_dark_mode(self):
        with tempfile.TemporaryDirectory() as tmp, patch("core.mac_actions.Path.home", return_value=Path(tmp)):
            (Path(tmp) / "Desktop").mkdir()

            def fake(args, **kwargs):
                if args[0] == "screencapture":
                    Path(args[-1]).write_bytes(b"png")
                return completed()
            self.run.side_effect = fake
            for phrase in ("сделай скриншот", "скриншот"):
                self.assertEqual(self.mac.execute(phrase), "Скриншот сохранён на рабочем столе.")
        self.assertEqual(self.mac.execute("заблокируй экран"), "Экран заблокирован.")
        self.assertEqual(self.mac.execute("заблокируй компьютер"), "Экран заблокирован.")
        self.assertEqual(self.mac.execute("Включи тёмную тему"), "Тёмная тема включена.")
        self.assertEqual(self.mac.execute("выключи тёмную тему"), "Тёмная тема выключена.")
        self.assertEqual(self.mac.execute("выключи темную тему"), "Тёмная тема выключена.")  # «е» вместо «ё»

    def test_media_uses_running_player(self):
        # Music запущен и играет, Spotify в списке «установленных» нет
        self.run.side_effect = [completed("true"), completed("playing\nBelieve\nCher"), completed("true"), completed("")]
        self.assertEqual(self.mac.execute("поставь на паузу"), "Пауза.")
        last = self.run.call_args.args[0]
        self.assertEqual(last[-1], "Music")
        self.assertIn("pause", " ".join(last))

    def test_media_without_player_goes_to_model(self):
        self.run.side_effect = [completed("false")]
        self.assertIsNone(self.mac.execute("пауза"))

    def test_now_playing_phrase(self):
        self.run.side_effect = [completed("true"), completed("playing\nBelieve\nCher")] * 2
        self.assertEqual(self.mac.execute("что играет"), "Играет трек Believe — Cher.")

    def test_permission_error_is_spoken(self):
        self.run.return_value = completed("", 1, "Not authorized to send Apple events to System Events. (-1743)")
        reply = self.mac.execute("включи тёмную тему")
        self.assertIn("Нужно разрешение", reply)
        self.assertIn("Автоматизация", reply)

    def test_failure_reason_spoken_not_none(self):
        self.run.return_value = completed("", 1, "boom")
        self.assertEqual(self.mac.execute("заблокируй экран"), "Не получилось: команда завершилась с ошибкой.")


class TestQuickJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "actions.jsonl"
        old = journal.JOURNAL_FILE
        journal.setup_journal(self.path)
        self.addCleanup(setattr, journal, "JOURNAL_FILE", old)
        self.mac = MacControl()
        patcher = patch("core.mac_actions.subprocess.run", return_value=completed(PMSET))
        patcher.start()
        self.addCleanup(patcher.stop)

    def entries(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_new_command_is_journaled(self):
        self.mac.execute("какой заряд")
        [entry] = self.entries()
        self.assertEqual(entry["phrase"], "какой заряд")
        self.assertEqual(entry["action"], {"name": "system_info", "args": {"what": "battery"}})
        self.assertEqual((entry["level"], entry["ok"], entry["confirmed"]), ("SAFE", True, None))

    def test_failed_quick_command_is_journaled(self):
        with patch("core.mac_actions.subprocess.run", return_value=completed("", 1, "boom")):
            self.mac.execute("заблокируй экран")
        [entry] = self.entries()
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["action"]["name"], "display")

    def test_old_commands_are_journaled(self):
        self.mac.execute("сделай громче")
        self.mac.execute("выключи звук")
        self.mac.execute("открой калькулятор")
        self.mac.execute("открой браузер")
        actions = [(e["action"]["name"], e["ok"]) for e in self.entries()]
        self.assertEqual(actions, [("quick_volume", True), ("quick_volume", True), ("open_app", True),
                                   ("open_url", True)])
        self.assertEqual(self.entries()[2]["action"]["args"], {"app": "Calculator"})

    def test_not_intercepted_not_journaled(self):
        self.mac.execute("закрой Safari")
        self.mac.execute("какой заряд у телефона")
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
