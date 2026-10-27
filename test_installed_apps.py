import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import installed_apps
from core.ai import AIClient
from core.safety import SAFE, classify
from fixtures_apps import patch_installed


class FakeAppsDirMixin:
    """Временные папки с пустыми каталогами *.app вместо /Applications."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        apps, utils = root / "Applications", root / "Utilities"
        for name in ["Safari", "Telegram", "Google Chrome", "Visual Studio Code", "Microsoft Word",
                     "Microsoft Excel", "VLC"]:
            (apps / f"{name}.app").mkdir(parents=True)
        (apps / "Vendor" / "Tool.app").mkdir(parents=True)          # один уровень глубже — да
        (apps / "Vendor" / "Sub" / "Deep.app").mkdir(parents=True)  # два уровня — нет
        (apps / ".Hidden.app").mkdir()
        (apps / "notes.txt").write_text("не приложение", encoding="utf-8")
        (apps / "Fake.app").write_text("файл, а не папка", encoding="utf-8")
        (utils / "Terminal.app").mkdir(parents=True)

        self.patches = [
            patch.object(installed_apps, "APP_DIRS", [str(apps), str(utils), str(root / "missing")]),
            patch.object(installed_apps, "VENDOR_DIRS", [str(apps)]),
            patch.object(installed_apps, "EXTRA_APPS", []),
        ]
        for p in self.patches:
            p.start()
        installed_apps.clear_cache()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        installed_apps.clear_cache()
        self._tmp.cleanup()


class TestScan(FakeAppsDirMixin, unittest.TestCase):
    def test_lists_app_bundles(self):
        self.assertEqual(installed_apps.list_installed_apps(), [
            "Google Chrome", "Microsoft Excel", "Microsoft Word", "Safari", "Telegram", "Terminal", "Tool",
            "Visual Studio Code", "VLC",
        ])

    def test_cache_ten_minutes(self):
        with patch.object(installed_apps, "scan_apps", wraps=installed_apps.scan_apps) as scan, \
                patch.object(installed_apps.time, "monotonic", return_value=1000.0) as clock:
            installed_apps.list_installed_apps()
            installed_apps.list_installed_apps()
            self.assertEqual(scan.call_count, 1)
            clock.return_value = 1000.0 + 599
            installed_apps.list_installed_apps()
            self.assertEqual(scan.call_count, 1)
            clock.return_value = 1000.0 + 601
            installed_apps.list_installed_apps()
            self.assertEqual(scan.call_count, 2)
            installed_apps.clear_cache()
            installed_apps.list_installed_apps()
            self.assertEqual(scan.call_count, 3)


class TestResolve(FakeAppsDirMixin, unittest.TestCase):
    def test_exact_and_case(self):
        self.assertEqual(installed_apps.resolve_app_name("Safari"), "Safari")
        self.assertEqual(installed_apps.resolve_app_name("SAFARI"), "Safari")
        self.assertEqual(installed_apps.resolve_app_name("visual studio code"), "Visual Studio Code")

    def test_alias(self):
        self.assertEqual(installed_apps.resolve_app_name("сафари"), "Safari")
        self.assertEqual(installed_apps.resolve_app_name("хром"), "Google Chrome")
        self.assertEqual(installed_apps.resolve_app_name("телеграма"), "Telegram")
        # В словаре есть, но не установлено
        self.assertIsNone(installed_apps.resolve_app_name("спотифай"))

    def test_by_word(self):
        self.assertEqual(installed_apps.resolve_app_name("Chrome"), "Google Chrome")
        self.assertEqual(installed_apps.resolve_app_name("word"), "Microsoft Word")
        self.assertEqual(installed_apps.resolve_app_name("code"), "Visual Studio Code")

    def test_fuzzy(self):
        self.assertEqual(installed_apps.resolve_app_name("Telegarm"), "Telegram")
        self.assertEqual(installed_apps.resolve_app_name("Terminl"), "Terminal")
        self.assertEqual(installed_apps.resolve_app_name("Safary"), "Safari")

    def test_unknown_or_ambiguous(self):
        for name in ["Spotify", "xyzzy", "Photoshop", "microsoft", "", "   ", None, 42]:
            with self.subTest(name=name):
                self.assertIsNone(installed_apps.resolve_app_name(name))

    def test_ambiguous_word_not_guessed(self):
        # Найдено на реальном Mac: «Code» есть в двух названиях, а нечётко ближе всего Xcode
        apps = ["Visual Studio Code", "Claude Code URL Handler", "Xcode", "Safari"]
        with patch.object(installed_apps, "list_installed_apps", return_value=apps):
            self.assertIsNone(installed_apps.resolve_app_name("Code"))
            self.assertEqual(installed_apps.resolve_app_name("Xcode"), "Xcode")
            self.assertEqual(installed_apps.resolve_app_name("visual studio code"), "Visual Studio Code")

    def test_empty_list_passes_name(self):
        with patch.object(installed_apps, "list_installed_apps", return_value=[]):
            self.assertEqual(installed_apps.resolve_app_name("сафари"), "Safari")
            self.assertEqual(installed_apps.resolve_app_name("Whatever"), "Whatever")
        print("✅ resolve_app_name: OK")


class TestFindAndList(FakeAppsDirMixin, unittest.TestCase):
    def test_find(self):
        self.assertEqual(installed_apps.find_apps("telegram")[0], "Telegram")
        self.assertEqual(set(installed_apps.find_apps("microsoft")), {"Microsoft Word", "Microsoft Excel"})
        self.assertIn("Google Chrome", installed_apps.find_apps("хром"))
        self.assertEqual(installed_apps.find_apps("qwertyuiop"), [])

    def test_find_limit(self):
        many = [f"App {i}" for i in range(25)]
        with patch.object(installed_apps, "list_installed_apps", return_value=many):
            self.assertEqual(len(installed_apps.find_apps("app")), 10)

    def test_popular_first(self):
        apps = ["Zed", "Abc", "Telegram", "Safari", "VLC"]
        self.assertEqual(installed_apps.popular_first(apps), ["Safari", "Telegram", "VLC", "Zed", "Abc"])
        self.assertEqual(len(installed_apps.popular_first([f"App {i}" for i in range(50)])), 30)
        print("✅ find_apps / list_apps: OK")


class TestAppActions(unittest.TestCase):
    def setUp(self):
        patcher = patch_installed()
        patcher.start()
        self.addCleanup(patcher.stop)
        run = patch('core.ai.subprocess.run')
        self.mock_run = run.start()
        self.addCleanup(run.stop)
        self.mock_run.return_value.returncode = 0
        with patch('openai.OpenAI'):
            self.client = AIClient(llm=MagicMock())

    def test_read_actions_are_safe(self):
        self.assertEqual(classify({"name": "find_apps", "args": {"query": "tele"}})[0], SAFE)
        self.assertEqual(classify({"name": "list_apps", "args": {}})[0], SAFE)

    def test_find_apps_action(self):
        [result] = self.client.run_actions([{"name": "find_apps", "args": {"query": "microsoft"}}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["speech"], "Нашёл: Microsoft Excel, Microsoft Word.")
        [result] = self.client.run_actions([{"name": "find_apps", "args": {"query": "qwertyuiop"}}])
        self.assertFalse(result["ok"])  # «не найдено» — неудача (4.4)
        self.assertEqual(result["short"], "не нашёл")
        [result] = self.client.run_actions([{"name": "find_apps", "args": {"query": ""}}])
        self.assertFalse(result["ok"])
        self.mock_run.assert_not_called()  # только чтение

    def test_list_apps_action(self):
        [result] = self.client.run_actions([{"name": "list_apps", "args": {}}])
        self.assertTrue(result["ok"])
        self.assertTrue(result["speech"].startswith("Установлены: Safari, Telegram, Google Chrome"))
        self.assertIn("и ещё", result["speech"])  # вслух — не больше 10 названий
        self.mock_run.assert_not_called()

    def test_open_unknown_app_rejected(self):
        [result] = self.client.run_actions([{"name": "open_app", "args": {"app": "Spotify"}}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["short"], "приложение Spotify не найдено")
        self.mock_run.assert_not_called()

    def test_open_by_word_and_fuzzy(self):
        self.client.run_actions([{"name": "open_app", "args": {"app": "Chrome"}}])
        self.mock_run.assert_called_with(["open", "-a", "Google Chrome"], capture_output=True, text=True, timeout=10)
        self.client.run_actions([{"name": "open_app", "args": {"app": "Telegarm"}}])
        self.mock_run.assert_called_with(["open", "-a", "Telegram"], capture_output=True, text=True, timeout=10)

    def test_quit_fuzzy_careful_app_needs_confirm(self):
        # «Terminl» распознаётся как Terminal — и уровень, и выполнение видят одно и то же приложение
        [result] = self.client.run_actions([{"name": "quit_app", "args": {"app": "Terminl"}}])
        self.assertEqual(result["short"], "нужно подтверждение")
        self.mock_run.assert_not_called()

    def test_prompt_points_to_find_apps(self):
        # С шага 4.2 списка приложений в промпте нет: его дают find_apps / list_apps
        self.assertNotIn("Установленные приложения", self.client.system_prompt)
        self.assertNotIn("Telegram, Google Chrome", self.client.system_prompt)
        self.assertIn("find_apps", self.client.system_prompt)
        print("✅ Действия с приложениями: OK")


if __name__ == '__main__':
    unittest.main()
