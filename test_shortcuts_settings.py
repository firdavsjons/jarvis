"""Быстрые команды (allowlist, запуск, права) и панели Системных настроек. subprocess замокан."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core import safety
from core.apps import registry, settings, shortcuts
from core.errors import ActionError
from fixtures_run import completed, isolated_apps, scripted_run

LIST = "Jarvis Alarm\nJarvis Timer\nJarvis Focus\nВыключить свет\nОтправить отчёт\nLock Launcher\n"


def shortcut(**args):
    return registry.get("shortcuts").run(phrase="", **args)


def level(**args):
    return safety.classify({"name": "shortcuts", "args": args})[0]


class ShortcutsCase(unittest.TestCase):
    def setUp(self):
        _, self.path = isolated_apps(self, {"Jarvis Alarm": "SAFE", "Jarvis Timer": "SAFE", "Jarvis Focus": "SAFE",
                                            "Выключить свет": "CONFIRM"})


class TestAllowlistLevels(ShortcutsCase):
    def test_levels(self):
        self.assertEqual(level(action="run", name="Jarvis Alarm", input=""), safety.SAFE)
        self.assertEqual(level(action="run", name="Выключить свет", input=""), safety.CONFIRM)
        self.assertEqual(level(action="run", name="Отправить отчёт", input=""), safety.BLOCKED)   # не из списка
        self.assertEqual(level(action="run", name="jarvis alarm", input=""), safety.BLOCKED)      # точное имя
        self.assertEqual(level(action="run", name="", input=""), safety.BLOCKED)
        self.assertEqual(level(action="run", name=None, input=""), safety.BLOCKED)
        self.assertEqual(level(action="list", name="", input=""), safety.SAFE)
        self.assertEqual(level(action="delete", name="Jarvis Alarm", input=""), safety.BLOCKED)

    def test_list_of_names_means_confirm(self):
        self.path.write_text(json.dumps(["Выключить свет", "Другая"]), encoding="utf-8")
        self.assertEqual(level(action="run", name="Другая", input=""), safety.CONFIRM)
        self.assertEqual(level(action="run", name="Jarvis Alarm", input=""), safety.BLOCKED)

    def test_explicit_blocked_and_bad_values(self):
        self.path.write_text(json.dumps({"Опасная": "BLOCKED", "Опечатка": "SAF", "Малые": "safe"}), encoding="utf-8")
        self.assertEqual(level(action="run", name="Опасная", input=""), safety.BLOCKED)
        self.assertEqual(level(action="run", name="Опечатка", input=""), safety.CONFIRM)   # неизвестное значение — спросить
        self.assertEqual(level(action="run", name="Малые", input=""), safety.SAFE)

    def test_broken_file_allows_nothing(self):
        for text in ("{не json", '"строка"', "42"):
            self.path.write_text(text, encoding="utf-8")
            self.assertEqual(level(action="run", name="Jarvis Alarm", input=""), safety.BLOCKED, text)

    def test_file_created_with_defaults_private(self):
        self.path.unlink()
        self.assertEqual(level(action="run", name="Jarvis Timer", input=""), safety.SAFE)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"Jarvis Alarm": "SAFE", "Jarvis Timer": "SAFE", "Jarvis Focus": "SAFE"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(level(action="run", name="Любая другая", input=""), safety.BLOCKED)


class TestRun(ShortcutsCase):
    def test_run_exists_list_and_argv(self):
        mock = scripted_run(LIST, completed(""))
        with patch("core.apps.shortcuts.subprocess.run", mock):
            done = shortcut(action="run", name="Jarvis Timer", input="300")
        args = mock.call_args.args[0]
        self.assertEqual(args[:3], ["shortcuts", "run", "Jarvis Timer"])
        self.assertIn("--input-path", args)
        self.assertEqual(mock.call_args.kwargs["timeout"], 30)
        self.assertTrue(done.web)   # результат — чужие данные
        self.assertEqual(done.speech, "Команда «Jarvis Timer» выполнена.")

    def test_input_goes_through_private_temp_file_not_argv(self):
        seen = {}

        def fake(args, **kwargs):
            if args[:2] == ["shortcuts", "list"]:
                return completed(LIST)
            source = Path(args[args.index("--input-path") + 1])
            seen["text"] = source.read_text(encoding="utf-8")
            seen["mode"] = stat.S_IMODE(source.stat().st_mode)
            seen["dir_mode"] = stat.S_IMODE(source.parent.stat().st_mode)
            seen["path"] = source
            return completed("")
        with patch("core.apps.shortcuts.subprocess.run", side_effect=fake) as run:
            shortcut(action="run", name="Jarvis Alarm", input="07:30 Подъём; rm -rf ~")
        self.assertEqual(seen["text"], "07:30 Подъём; rm -rf ~")
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["dir_mode"], 0o700)
        self.assertNotIn("rm -rf", " ".join(run.call_args.args[0]))
        self.assertFalse(seen["path"].exists())          # временный файл удалён

    def test_nonexistent_command(self):
        with patch("core.apps.shortcuts.subprocess.run", scripted_run("Lock Launcher\nЗвонок\n")):
            with self.assertRaises(ActionError) as ctx:
                shortcut(action="run", name="Jarvis Alarm", input="")
        self.assertEqual(ctx.exception.short, "команды «Jarvis Alarm» нет в приложении")

    def test_name_not_in_allowlist_never_runs(self):
        mock = scripted_run(LIST, completed(""))
        with patch("core.apps.shortcuts.subprocess.run", mock):
            with self.assertRaises(ActionError) as ctx:
                shortcut(action="run", name="Отправить отчёт", input="")
        self.assertIn("списке разрешённых", ctx.exception.short)
        mock.assert_not_called()

    def test_validation(self):
        mock = scripted_run(LIST)
        with patch("core.apps.shortcuts.subprocess.run", mock):
            for name, text in [("", ""), ("a" * 101, ""), ("Jarvis Alarm\x00", ""), ("Jarvis Alarm", "x" * 201),
                               ("Jarvis Alarm", "строка\x07")]:
                with self.subTest(name=name[:10], text=text[:10]), self.assertRaises(ActionError):
                    shortcut(action="run", name=name, input=text)
        mock.assert_not_called()

    def test_output_limited_to_300(self):
        long = "слово " * 200

        def fake(args, **kwargs):
            if args[:2] == ["shortcuts", "list"]:
                return completed(LIST)
            Path(args[args.index("--output-path") + 1]).write_text(long, encoding="utf-8")
            return completed("")
        with patch("core.apps.shortcuts.subprocess.run", side_effect=fake):
            done = shortcut(action="run", name="Jarvis Focus", input="on")
        self.assertLessEqual(len(done.speech), 302)
        self.assertTrue(done.speech.rstrip(".").endswith("…"))

    def test_timeout_and_failure(self):
        with patch("core.apps.shortcuts.subprocess.run", scripted_run(LIST, subprocess.TimeoutExpired("shortcuts", 30))):
            with self.assertRaises(ActionError) as ctx:
                shortcut(action="run", name="Jarvis Timer", input="")
        self.assertEqual(ctx.exception.short, "команда «Jarvis Timer» не ответила вовремя")
        with patch("core.apps.shortcuts.subprocess.run", scripted_run(LIST, completed("", 1, "Error: что-то"))):
            with self.assertRaises(ActionError) as ctx:
                shortcut(action="run", name="Jarvis Timer", input="")
        self.assertEqual(ctx.exception.short, "команда «Jarvis Timer» не выполнилась")

    def test_permission_error_names_shortcuts_app(self):
        with patch("core.apps.shortcuts.subprocess.run", scripted_run(LIST, completed("", 1, "(-1743)"))):
            with self.assertRaises(ActionError) as ctx:
                shortcut(action="run", name="Jarvis Timer", input="")
        self.assertIn("Автоматизация → Быстрые команды", str(ctx.exception))

    def test_list_shows_only_allowed_existing(self):
        with patch("core.apps.shortcuts.subprocess.run", scripted_run(LIST)):
            done = shortcut(action="list", name="", input="")
        self.assertEqual(done.speech, "Доступны команды: Jarvis Alarm, Jarvis Timer, Jarvis Focus, Выключить свет.")
        self.assertNotIn("Lock Launcher", done.speech)
        self.assertNotIn("Отправить отчёт", done.speech)

    def test_preview_for_confirm(self):
        text = registry.get("shortcuts").preview(action="run", name="Выключить свет", input="гостиная")
        self.assertEqual(text, "запустить быструю команду «Выключить свет» с входом «гостиная»")

    def test_run_is_external_and_destructive(self):
        tool = registry.get("shortcuts")
        self.assertTrue(tool.is_external({"action": "run"}))
        self.assertTrue(tool.is_destructive({"action": "run"}))
        self.assertFalse(tool.is_external({"action": "list"}))


class TestSettingsPanes(unittest.TestCase):
    EXPECTED = {"wifi", "bluetooth", "sound", "display", "battery", "notifications", "focus", "keyboard", "privacy",
                "accessibility", "storage", "general", "network"}

    def test_table_is_exactly_the_agreed_panes(self):
        self.assertEqual(set(settings.PANES), self.EXPECTED)
        self.assertNotIn("passwords", settings.PANES)
        tool = registry.get("settings")
        self.assertEqual(set(tool.actions), self.EXPECTED)
        self.assertEqual(set(tool.schema()["function"]["parameters"]["properties"]["pane"]["enum"]), self.EXPECTED)

    def test_urls_only_from_table(self):
        for pane, (ident, _, _) in settings.PANES.items():
            self.assertEqual(settings.pane_url(pane), "x-apple.systempreferences:" + ident)
            self.assertRegex(ident, r"^com\.apple\.[A-Za-z0-9.\-]+$")
        for bad in ("passwords", "../x", "wifi; open /", "", None, "WIFI"):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                settings.pane_url(bad)

    def test_open_uses_list_args_without_model_string(self):
        mock = scripted_run("")
        with patch("core.apps.settings.subprocess.run", mock):
            done = settings.run("sound", phrase="")
        self.assertEqual(mock.call_args.args[0], ["open", "x-apple.systempreferences:com.apple.Sound-Settings.extension"])
        self.assertEqual(done.speech, "Открыл настройки звука.")
        mock = scripted_run("")
        with patch("core.apps.settings.subprocess.run", mock), self.assertRaises(ActionError):
            settings.run("https://evil.example", phrase="")
        mock.assert_not_called()

    def test_failure(self):
        with patch("core.apps.settings.subprocess.run", scripted_run(completed("", 1, "boom"))):
            with self.assertRaises(ActionError) as ctx:
                settings.run("wifi", phrase="")
        self.assertEqual(ctx.exception.short, "не удалось открыть настройки")

    def test_levels(self):
        for pane in settings.PANES:
            self.assertEqual(safety.classify({"name": "settings", "args": {"pane": pane}})[0], safety.SAFE)
        self.assertEqual(safety.classify({"name": "settings", "args": {"pane": "passwords"}})[0], safety.BLOCKED)

    def test_phrases(self):
        cases = {"открой настройки Wi-Fi": "wifi", "открой настройки вайфай": "wifi", "Открой настройки звука": "sound",
                 "открой настройки bluetooth": "bluetooth", "открой настройки экрана": "display",
                 "открой настройки батареи": "battery", "открой, пожалуйста, настройки уведомлений": "notifications",
                 "покажи настройки сети": "network", "открой настройки универсального доступа": "accessibility"}
        for phrase, pane in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(settings.pane_from_phrase(phrase), pane)
        for phrase in ["открой настройки", "открой настройки паролей", "открой настройки звука и экрана",
                       "расскажи про настройки звука", "настройки wifi", "открой звук"]:
            with self.subTest(phrase=phrase):
                self.assertIsNone(settings.pane_from_phrase(phrase))


if __name__ == "__main__":
    unittest.main()
