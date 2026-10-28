"""Тест самой защиты (tests_base): настоящий запуск падает, мок работает, запись в домашние папки падает."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from tests_base import FORBIDDEN_DIRS, TEMP_ROOT, VIOLATIONS, ForbiddenInTests

ROOT = Path(__file__).resolve().parent
MARK = "jarvis-tests-base-must-not-exist"


class BaseCase(unittest.TestCase):
    def setUp(self):
        before = len(VIOLATIONS)
        # нарушения, которые тест вызывает нарочно, не должны оставаться в общем списке
        self.addCleanup(lambda: VIOLATIONS.__delitem__(slice(before, None)))


class TestProcessesAreForbidden(BaseCase):
    def test_real_subprocess_run_raises(self):
        with self.assertRaises(ForbiddenInTests) as ctx:
            subprocess.run(["echo", "не должно запуститься"])
        self.assertIn("настоящий запуск запрещён в тестах", str(ctx.exception))

    def test_popen_and_wrappers_raise(self):
        for call in (lambda: subprocess.Popen(["echo", "x"]), lambda: subprocess.call(["echo", "x"]),
                     lambda: subprocess.check_call(["echo", "x"]), lambda: subprocess.check_output(["echo", "x"]),
                     lambda: os.system("echo x"), lambda: os.popen("echo x"),
                     lambda: os.posix_spawn("/bin/echo", ["echo"], {})):
            with self.subTest(call=call), self.assertRaises(ForbiddenInTests):
                call()

    def test_guard_is_not_swallowed_by_application_error_handling(self):
        """Приложение ловит OSError и Exception — защита от этого не прячется."""
        swallowed = []
        try:
            try:
                subprocess.run(["echo", "x"])
            except OSError:
                swallowed.append("OSError")
            except Exception:
                swallowed.append("Exception")
        except ForbiddenInTests:
            pass
        self.assertEqual(swallowed, [])
        self.assertTrue(issubclass(ForbiddenInTests, BaseException))
        self.assertFalse(issubclass(ForbiddenInTests, Exception))

    def test_violations_are_recorded(self):
        before = len(VIOLATIONS)
        with self.assertRaises(ForbiddenInTests):
            subprocess.run(["echo", "x"])
        self.assertEqual(len(VIOLATIONS), before + 1)

    def test_mock_works(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertEqual(subprocess.run(["echo", "x"]).returncode, 0)
            run.assert_called_once_with(["echo", "x"])
        with patch("subprocess.Popen") as popen:
            subprocess.Popen(["say", "привет"])
            popen.assert_called_once()
        with patch("os.system", return_value=0) as system:
            self.assertEqual(os.system("echo x"), 0)
            system.assert_called_once()

    def test_module_level_patches_used_by_the_app_work(self):
        from core import ai
        with patch("core.ai.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            ai._run(["open", "-a", "Safari"])
            run.assert_called_once()

    def test_guard_returns_after_the_mock_is_gone(self):
        with patch("subprocess.run"):
            pass
        with self.assertRaises(ForbiddenInTests):
            subprocess.run(["echo", "x"])


class TestHomeFoldersAreForbidden(BaseCase):
    def test_protected_folders(self):
        names = {p.name for p in FORBIDDEN_DIRS}
        self.assertEqual(names, {"Jarvis", "Desktop", "Documents", "Downloads"})
        home = Path.home()
        self.assertIn(home / "Library" / "Application Support" / "Jarvis", FORBIDDEN_DIRS)
        self.assertIn(home / "Library" / "Logs" / "Jarvis", FORBIDDEN_DIRS)

    def test_writes_in_every_protected_folder_raise_and_create_nothing(self):
        for folder in FORBIDDEN_DIRS:
            target = folder / f"{MARK}.txt"
            sub = folder / MARK
            attempts = {
                "open w": lambda: open(target, "w"),
                "open a": lambda: open(target, "a"),
                "open x": lambda: open(target, "x"),
                "open w+": lambda: open(target, "w+"),
                "write_text": lambda: target.write_text("x"),
                "write_bytes": lambda: target.write_bytes(b"x"),
                "Path.open": lambda: target.open("w"),
                "os.open": lambda: os.open(target, os.O_WRONLY | os.O_CREAT),
                "mkdir": lambda: sub.mkdir(),
                "makedirs": lambda: os.makedirs(sub / "a" / "b"),
                "replace": lambda: os.replace(tempfile.gettempdir(), target),
                "rename": lambda: os.rename(target, sub),
                "remove": lambda: os.remove(target),
                "unlink": lambda: target.unlink(),
                "rmdir": lambda: os.rmdir(sub),
                "chmod": lambda: os.chmod(target, 0o600),
                "rmtree": lambda: shutil.rmtree(sub),
                "shutil.copyfile": lambda: shutil.copyfile(__file__, target),
                "shutil.move": lambda: shutil.move(__file__, target),
            }
            for name, attempt in attempts.items():
                with self.subTest(folder=folder.name, attempt=name), self.assertRaises(ForbiddenInTests):
                    attempt()
            self.assertFalse(target.exists())
            self.assertFalse(sub.exists())

    def test_via_symlink_and_dotdot(self):
        target = Path.home() / "Desktop" / "x" / ".." / f"{MARK}.txt"
        with self.assertRaises(ForbiddenInTests):
            open(target, "w")
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "desktop-link"
            link.symlink_to(Path.home() / "Desktop")
            with self.assertRaises(ForbiddenInTests):
                (link / f"{MARK}.txt").write_text("x")
        self.assertFalse((Path.home() / "Desktop" / f"{MARK}.txt").exists())

    def test_folder_name_case_does_not_help(self):
        with self.assertRaises(ForbiddenInTests):
            open(Path.home() / "DESKTOP" / f"{MARK}.txt", "w")

    def test_application_code_writing_there_is_caught(self):
        """Реальный путь кода приложения: atomic_write_json в папку данных пользователя."""
        from core.storage import atomic_write_json
        real = Path.home() / "Library" / "Application Support" / "Jarvis" / f"{MARK}.json"
        with self.assertRaises(ForbiddenInTests):
            atomic_write_json(real, {"a": 1})
        self.assertFalse(real.exists())

    def test_reading_is_allowed(self):
        with self.assertRaises(FileNotFoundError):
            open(Path.home() / "Desktop" / f"{MARK}-read.txt")
        self.assertIsInstance(os.listdir(Path.home()), list)
        self.assertTrue(Path(__file__).read_text(encoding="utf-8"))

    def test_temp_folders_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "b.txt"
            path.parent.mkdir()
            path.write_text("ок")
            os.chmod(path, 0o600)
            os.replace(path, path.with_name("c.txt"))
            self.assertEqual(path.with_name("c.txt").read_text(), "ок")
        self.assertTrue(TEMP_ROOT.exists())
        (TEMP_ROOT / MARK).write_text("ок")
        (TEMP_ROOT / MARK).unlink()

    def test_project_folder_is_inside_documents_but_not_writable_either(self):
        if "Documents" in ROOT.parts:   # проект лежит в ~/Documents: писать в него тесты тоже не должны
            with self.assertRaises(ForbiddenInTests):
                (ROOT / f"{MARK}.txt").write_text("x")
            self.assertFalse((ROOT / f"{MARK}.txt").exists())


class TestAppPathsRedirected(BaseCase):
    def test_constants_point_into_temp(self):
        from core import cache, logger, memory, scheduler, storage
        from core.apps import shortcuts
        for path in (storage.DATA_DIR, cache.CACHE_FILE, memory.MEMORY_FILE, scheduler.TIMERS_FILE,
                     shortcuts.SHORTCUTS_FILE, logger.LOG_DIR, logger.LOG_FILE):
            self.assertTrue(TEMP_ROOT in Path(path).parents or Path(path) == TEMP_ROOT, path)
            self.assertFalse(tests_base.is_forbidden_path(path), path)

    def test_default_writes_land_in_temp_and_real_files_are_untouched(self):
        from core import storage
        from core.apps import shortcuts
        real = Path.home() / "Library" / "Application Support" / "Jarvis" / "shortcuts_allowed.json"
        before = real.stat().st_mtime_ns if real.exists() else None
        with patch.object(shortcuts, "SHORTCUTS_FILE", storage.DATA_DIR / "fresh" / "shortcuts_allowed.json"):
            self.assertEqual(shortcuts.load_allowed()["Jarvis Alarm"], "SAFE")      # создаётся по умолчанию
            self.assertTrue(shortcuts.SHORTCUTS_FILE.exists())
        self.assertEqual(real.stat().st_mtime_ns if real.exists() else None, before)


class TestSpeechRecognitionIsGuarded(BaseCase):
    """Этап 8.1: настоящая модель Whisper и звук в Google в тестах запрещены."""

    def test_loading_a_real_model_raises(self):
        from core.stt import faster, whisper_cpp
        for module in (whisper_cpp, faster):
            with self.subTest(module=module.__name__), self.assertRaises(ForbiddenInTests) as ctx:
                module._import_backend()
            self.assertIn("загрузка модели Whisper", str(ctx.exception))

    def test_recognize_google_raises(self):
        import speech_recognition as sr
        with self.assertRaises(ForbiddenInTests) as ctx:
            sr.Recognizer().recognize_google(None, language="ru-RU")
        self.assertIn("звук ушёл бы в интернет", str(ctx.exception))

    def test_load_error_in_engine_is_not_swallowed(self):
        """Загрузчик модели ловит Exception, но не BaseException: нарушение защиты не превращается в «недоступно»."""
        from core.stt.whisper_cpp import WhisperCppEngine
        engine = WhisperCppEngine(models_dir=TEMP_ROOT, unload_minutes=0)
        with self.assertRaises(ForbiddenInTests):
            engine.ensure_loaded()

    def test_patched_backend_is_allowed(self):
        from core.stt import whisper_cpp
        with patch("core.stt.whisper_cpp._import_backend", return_value="фальшивка"):
            self.assertEqual(whisper_cpp._import_backend(), "фальшивка")


class TestEveryTestFileIsProtected(unittest.TestCase):
    def test_all_test_files_import_tests_base(self):
        files = sorted(ROOT.glob("test_*.py")) + [ROOT / "verify_local_ai.py"]
        self.assertGreater(len(files), 25)
        missing = [f.name for f in files if not re.search(r"^import tests_base\b", f.read_text(encoding="utf-8"), re.M)]
        self.assertEqual(missing, [], "эти файлы не подключают tests_base")

    def test_no_exception_for_compilers_is_needed(self):
        """osacompile и py_compile тестам сейчас не нужны; если понадобятся — отдельное явное разрешение в tests_base."""
        text = "".join(f.read_text(encoding="utf-8") for f in ROOT.glob("test_*.py") if f.name != Path(__file__).name)
        self.assertNotIn("osacompile", text)
        self.assertNotIn("py_compile", text)


if __name__ == "__main__":
    unittest.main()
