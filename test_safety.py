import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import journal, safety
from core.ai import ACTIONS, AIClient
from core.safety import BLOCKED, CONFIRM, SAFE, classify, classify_path, classify_url
from core.llm import LLMResponse
from fixtures_apps import patch_installed
import main

_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()


def trash(path):
    return {"name": "trash_file", "args": {"path": path}}


class FakeHomeMixin:
    """Временная «домашняя папка» с файлом и симлинками — настоящие пути не трогаем."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._tmp.name)
        desktop = Path(self.home, "Desktop")
        desktop.mkdir()
        self.report = desktop / "отчёт.txt"
        self.report.write_text("x", encoding="utf-8")
        self.link_to_system = desktop / "sys_link"
        self.link_to_system.symlink_to("/System")
        self.link_to_report = desktop / "report_link"
        self.link_to_report.symlink_to(self.report)
        Path(self.home, ".ssh").mkdir()
        Path(self.home, ".ssh", "id_rsa").write_text("key", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()


class TestPathLevels(FakeHomeMixin, unittest.TestCase):
    def test_dangerous_paths_blocked(self):
        dangerous = [
            "/System", "/system/library", "/SYSTEM/Library/CoreServices", "/", "~", "~/", "~/.",
            "~/Library", "~/library/Preferences/x.plist", "~/.ssh", "~/.ssh/id_rsa", "~/.zshrc", "~/.Trash",
            "~/.config/app", "../../etc", "Desktop/отчёт.txt", "./x", "~/Desktop/../../..", "~/Desktop/../..",
            "~/Desktop/../Library/Keychains", "/etc/hosts", "/tmp/x", "/var/log", "/Applications/Safari.app",
            "/usr/bin/python3", "/bin/rm", "/sbin", "/Library/Preferences", "/private/etc", "/Users",
            "/Volumes/USB/file.txt", "/opt/homebrew", "", "   ", None, 42, "~/Desktop/a\nb",
        ]
        for path in dangerous:
            with self.subTest(path=path):
                level, reason, _ = classify_path(path)
                self.assertEqual(level, BLOCKED, f"{path!r}: {reason}")

    def test_reasons(self):
        self.assertIn("/System", classify_path("/system/library")[1])
        self.assertIn("/private", classify_path("/etc/hosts")[1])  # /etc -> /private/etc
        self.assertIn("домашняя папка", classify_path("~")[1])
        self.assertIn("корень", classify_path("/")[1])
        self.assertIn("относительный", classify_path("../../etc")[1])
        self.assertIn("~/Library", classify_path("~/Library")[1])

    def test_symlink_to_system_blocked(self):
        level, reason, resolved = classify_path(str(self.link_to_system), home=self.home)
        self.assertEqual(level, BLOCKED)
        self.assertIn("/System", reason)
        self.assertEqual(resolved, "/System")

    def test_dotdot_inside_fake_home_resolved(self):
        sneaky = os.path.join(self.home, "Desktop", "..", ".ssh", "id_rsa")
        self.assertEqual(classify_path(sneaky, home=self.home)[0], BLOCKED)
        escape = os.path.join(self.home, "Desktop", "..", "..", "..")
        self.assertEqual(classify_path(escape, home=self.home)[0], BLOCKED)
        self.assertEqual(classify_path(self.home + "/", home=self.home)[0], BLOCKED)

    def test_user_files_confirm(self):
        for path in ["~/Desktop/отчёт.txt", "~/Documents/Projects/a.py", "~/Downloads/фото 1.jpg"]:
            with self.subTest(path=path):
                self.assertEqual(classify_path(path)[0], CONFIRM)
        self.assertEqual(classify_path(str(self.report), home=self.home)[0], CONFIRM)
        level, _, resolved = classify_path(str(self.link_to_report), home=self.home)
        self.assertEqual(level, CONFIRM)
        self.assertEqual(resolved, str(self.report))
        print("✅ Пути: OK")


class TestActionLevels(FakeHomeMixin, unittest.TestCase):
    def test_whitelist_matches_ai_actions(self):
        self.assertEqual(set(ACTIONS), safety.ACTION_NAMES)

    def test_unknown_actions_blocked(self):
        for action in [
            {"name": "sudo", "args": {"command": "rm -rf /"}},
            {"name": "run_shell", "args": {"command": "sudo rm -rf /"}},
            {"name": "shell", "args": {"command": "ls"}},
            {"name": "rm", "args": {"path": "/"}},
            {"name": "format_disk", "args": {"disk": "disk0"}},
            {"name": "disable_firewall", "args": {}},
            {"name": "disable_sip", "args": {}},
            {"step": 1, "command": "sudo rm -rf /"},
            "sudo rm -rf /",
            None,
        ]:
            with self.subTest(action=action):
                self.assertEqual(classify(action)[0], BLOCKED)

    def test_trash_levels(self):
        self.assertEqual(classify(trash("/System"))[0], BLOCKED)
        self.assertEqual(classify(trash("~/Desktop/отчёт.txt"))[0], CONFIRM)
        self.assertEqual(classify(trash(str(self.link_to_system)), home=self.home)[0], BLOCKED)
        self.assertEqual(classify({"name": "trash_file", "args": {}})[0], BLOCKED)
        self.assertEqual(classify({"name": "trash_file", "args": "~"})[0], BLOCKED)

    def test_safe_actions(self):
        self.assertEqual(classify({"name": "open_app", "args": {"app": "Safari"}})[0], SAFE)
        self.assertEqual(classify({"name": "set_volume", "args": {"level": 30}})[0], SAFE)

    def test_quit_app_levels(self):
        self.assertEqual(classify({"name": "quit_app", "args": {"app": "Safari"}})[0], SAFE)
        self.assertEqual(classify({"name": "quit_app", "args": {"app": "Spotify"}})[0], SAFE)
        for app in ["Terminal", "терминал", "iTerm", "Visual Studio Code", "visual studio code", "Code",
                    "Xcode", "PyCharm", "Finder", "файндер", "System Settings", "настройки"]:
            with self.subTest(app=app):
                self.assertEqual(classify({"name": "quit_app", "args": {"app": app}})[0], CONFIRM)

    def test_url_levels(self):
        safe = ["https://youtube.com", "https://www.youtube.com/watch?v=1", "https://m.youtube.com",
                "https://YOUTUBE.COM/", "https://ru.wikipedia.org/wiki/X", "http://google.com"]
        confirm = ["https://evil-youtube.com.evil.ru", "https://youtube.com.evil.ru", "https://evil-youtube.com",
                   "https://youtube.com@evil.ru", "https://notyoutube.com", "https://example.com"]
        blocked = ["file:///etc/passwd", "javascript:alert(1)", "ftp://youtube.com", "https://", "youtube.com",
                   None]
        for url in safe:
            self.assertEqual(classify_url(url)[0], SAFE, url)
        for url in confirm:
            self.assertEqual(classify_url(url)[0], CONFIRM, url)
        for url in blocked:
            self.assertEqual(classify_url(url)[0], BLOCKED, url)

    def test_trusted_domains_file(self):
        domains = safety.load_trusted_domains()
        self.assertIn("youtube.com", domains)
        self.assertNotIn("", domains)
        self.assertFalse(any(d.startswith("#") for d in domains))
        print("✅ Уровни действий: OK")


@patch('core.ai.subprocess.run')
class TestExecution(FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with patch('openai.OpenAI'):
            self.client = AIClient(llm=MagicMock())
        self.home_patch = patch('core.safety.home_dir', return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        super().tearDown()

    def test_blocked_never_runs(self, mock_run):
        for action in [trash("/System"), trash("~"), trash(str(self.link_to_system)),
                       {"name": "run_shell", "args": {"command": "sudo rm -rf /"}}]:
            log = self.client.execute_actions([action], confirmed=True)
            self.assertIn("⛔ Заблокировано", log)
        mock_run.assert_not_called()

    def test_confirm_needs_confirmation(self, mock_run):
        log = self.client.execute_actions([trash(str(self.report))])
        self.assertIn("⛔ Нужно подтверждение", log)
        log = self.client.execute_actions([{"name": "open_url", "args": {"url": "https://evil.ru"}}])
        self.assertIn("⛔ Нужно подтверждение", log)
        mock_run.assert_not_called()

    def test_trash_uses_finder_not_rm(self, mock_run):
        mock_run.return_value.returncode = 0
        log = self.client.execute_actions([trash(str(self.report))], confirmed=True)

        self.assertIn("✅", log)
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("Finder", " ".join(args))
        self.assertEqual(args[-1], str(self.report))  # путь — отдельным аргументом argv
        self.assertNotIn(str(self.report), " ".join(args[:-1]))  # и не вставлен в текст скрипта
        self.assertNotIn("rm", args)
        self.assertNotIn("shell", mock_run.call_args.kwargs)

    def test_trash_symlink_trashes_checked_target(self, mock_run):
        mock_run.return_value.returncode = 0
        self.client.execute_actions([trash(str(self.link_to_report))], confirmed=True)
        self.assertEqual(mock_run.call_args.args[0][-1], str(self.report))

    def test_trash_missing_file(self, mock_run):
        log = self.client.execute_actions([trash(os.path.join(self.home, "Desktop", "нет.txt"))], confirmed=True)
        self.assertIn("не найден", log)
        mock_run.assert_not_called()

    def test_safe_runs_without_confirmation(self, mock_run):
        mock_run.return_value.returncode = 0
        self.client.execute_actions([{"name": "quit_app", "args": {"app": "Safari"}},
                                     {"name": "open_url", "args": {"url": "https://youtube.com"}}])
        self.assertEqual(mock_run.call_count, 2)
        print("✅ Выполнение: OK")


class TestJournal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        journal.setup_journal(Path(self._tmp.name) / "actions.jsonl")
        with patch('openai.OpenAI'):
            self.client = AIClient(llm=MagicMock())

    def tearDown(self):
        journal.JOURNAL_FILE = None
        self._tmp.cleanup()

    def entries(self):
        return [json.loads(line) for line in journal.JOURNAL_FILE.read_text(encoding="utf-8").splitlines()]

    @patch('core.ai.subprocess.run')
    def test_records_actions(self, mock_run):
        mock_run.return_value.returncode = 0
        self.client.execute_actions([{"name": "open_app", "args": {"app": "Safari"}}, trash("/System")],
                                    phrase="открой сафари и удали систему")
        self.client.reject_actions([trash("~/Desktop/отчёт.txt")], "удали отчёт", "отменено пользователем",
                                   confirmed=False)
        first, second, third = self.entries()
        self.assertEqual(first["phrase"], "открой сафари и удали систему")
        self.assertEqual((first["level"], first["confirmed"], first["ok"]), (SAFE, None, True))
        self.assertEqual((second["level"], second["ok"]), (BLOCKED, False))
        self.assertIn("/System", second["result"])
        self.assertEqual((third["level"], third["confirmed"], third["ok"]), (CONFIRM, False, False))
        self.assertTrue(all("time" in e for e in (first, second, third)))

    def test_secrets_redacted(self):
        self.client.reject_actions(
            [{"name": "open_url", "args": {"url": "https://user:hunter2@site.com/a?token=abc123&q=1"}}],
            "мой пароль hunter2, api_key=XYZ789", "тест")
        line = journal.JOURNAL_FILE.read_text(encoding="utf-8")
        for secret in ["hunter2", "abc123", "XYZ789", "user:"]:
            self.assertNotIn(secret, line)
        entry = self.entries()[0]
        self.assertEqual(entry["action"]["args"]["url"], "https://site.com/a?token=***&q=1")
        self.assertIn("пароль ***", entry["phrase"])

    def test_disabled_by_default(self):
        journal.JOURNAL_FILE = None
        journal.record("x", {}, SAFE, None, True, "ok")  # не должно падать и ничего не пишет
        print("✅ Журнал: OK")


class TestBlockedReply(unittest.TestCase):
    """VoiceApp.process_command без окна: self — мок, ai — настоящий."""

    def make_app(self, ai_reply):
        # Модель без инструментов (режим JSON): отвечает JSON-текстом
        llm = MagicMock()
        llm.chat.return_value = LLMResponse(ok=True, text=json.dumps(ai_reply, ensure_ascii=False), mode="json")
        client = AIClient(llm=llm)
        app = MagicMock()
        app.ai = client
        app.has_pending.return_value = False
        app.mac.execute.return_value = None
        app.report_result = lambda results: main.VoiceApp.report_result(app, results)
        return app

    def spoken(self, app):
        return [c.args[0] for c in app.engine.speak.call_args_list]

    @patch('core.ai.subprocess.run')
    def test_any_blocked_rejects_whole_reply(self, mock_run):
        app = self.make_app({"response": "Открываю Safari и удаляю System.", "actions": [
            {"name": "open_app", "args": {"app": "Safari"}}, trash("/System")]})

        main.VoiceApp.process_command(app, "открой сафари и удали систему")

        mock_run.assert_not_called()
        app.engine.speak.assert_called_once_with(main.BLOCKED_REPLY)
        app.set_pending.assert_not_called()

    @patch('core.ai.subprocess.run')
    def test_timer_starts_after_question(self, mock_run):
        app = self.make_app({"response": "", "actions": [trash("~/Desktop/отчёт.txt")]})

        main.VoiceApp.process_command(app, "удали отчёт")

        names = [c[0] for c in app.mock_calls if c[0] in ("set_pending", "engine.speak", "start_pending_timer")]
        self.assertEqual(names, ["set_pending", "engine.speak", "start_pending_timer"])
        app.engine.speak.assert_called_once_with("Переместить в Корзину файл отчёт с рабочего стола?")
        mock_run.assert_not_called()
        self.assertEqual(main.CONFIRM_TIMEOUT, 20)
        print("✅ Ответ с BLOCKED и порядок подтверждения: OK")

    @patch('core.ai.subprocess.run')
    def test_model_response_not_spoken_before_confirmation(self, mock_run):
        app = self.make_app({"response": "Перемещаю отчёт в Корзину.", "actions": [trash("~/Desktop/отчёт.txt")]})
        main.VoiceApp.process_command(app, "удали отчёт")
        self.assertEqual(self.spoken(app), ["Переместить в Корзину файл отчёт с рабочего стола?"])


class TestHonestResult(FakeHomeMixin, unittest.TestCase):
    """После выполнения — правда: «Готово» только если всё получилось."""

    def setUp(self):
        super().setUp()
        self.home_patch = patch('core.safety.home_dir', return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        super().tearDown()

    make_app = TestBlockedReply.make_app
    spoken = TestBlockedReply.spoken

    @patch('core.ai.subprocess.run')
    def test_safe_success(self, mock_run):
        mock_run.return_value.returncode = 0
        app = self.make_app({"response": "", "actions": [{"name": "open_app", "args": {"app": "Safari"}}]})
        main.VoiceApp.process_command(app, "открой сафари")
        self.assertEqual(self.spoken(app), ["Готово."])

        app = self.make_app({"response": "Открываю Safari.", "actions": [{"name": "open_app", "args": {"app": "Safari"}}]})
        main.VoiceApp.process_command(app, "открой сафари")
        self.assertEqual(self.spoken(app), ["Открываю Safari."])

    @patch('core.ai.subprocess.run')
    def test_safe_failure(self, mock_run):
        app = self.make_app({"response": "Открываю Spotify.", "actions": [{"name": "open_app", "args": {"app": "Spotify"}}]})
        main.VoiceApp.process_command(app, "открой спотифай")

        self.assertEqual(self.spoken(app), ["Не получилось: приложение Spotify не найдено."])
        self.assertFalse(any("Готово" in s or "Открываю" in s for s in self.spoken(app)))
        mock_run.assert_not_called()
        self.assertIn("Не получилось", app.update_log.call_args.args[0])

    @patch('core.ai.subprocess.run')
    def test_command_error_is_failure(self, mock_run):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "boom"
        app = self.make_app({"response": "Ставлю громкость.", "actions": [{"name": "set_volume", "args": {"level": 30}}]})
        main.VoiceApp.process_command(app, "громкость 30")
        self.assertEqual(self.spoken(app), ["Не получилось: команда завершилась с ошибкой."])

    @patch('core.ai.subprocess.run')
    def test_confirmed_success(self, mock_run):
        mock_run.return_value.returncode = 0
        app = self.make_app({})
        app.take_pending.return_value = ([trash(str(self.report))], "удали отчёт")
        main.VoiceApp.handle_confirmation(app, "да")
        self.assertEqual(self.spoken(app), ["Выполняю.", "Готово."])

    @patch('core.ai.subprocess.run')
    def test_confirmed_failure(self, mock_run):
        app = self.make_app({})
        app.take_pending.return_value = ([trash(os.path.join(self.home, "Desktop", "нет.txt"))], "удали")
        main.VoiceApp.handle_confirmation(app, "да")

        self.assertEqual(self.spoken(app), ["Выполняю.", "Не получилось: файл не найден."])
        mock_run.assert_not_called()
        print("✅ Честный результат: OK")


if __name__ == '__main__':
    unittest.main()
