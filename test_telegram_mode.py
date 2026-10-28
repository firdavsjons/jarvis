"""Этап 7.1.4: боевая отправка Telegram с повторным открытием чата (TELEGRAM_FOCUS_MODE=reopen|direct). Только моки:
Telegram не запускается, клавиши не нажимаются, Enter — только в фазе 2 после «да»."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

from core import telegram_check as check
from core.ai import AIClient
from core.apps import telegram as tg
from test_telegram import FakeMac, TelegramCase
from test_telegram_check import CheckCase, User
from test_tools import ScriptedLLM, final, tools_reply

URL = ("open", "tg://resolve?domain=mama_user")
OLD = "старый буфер"


def env_file(text):
    path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / ".env"
    path.write_text(text, encoding="utf-8")
    return path


class ModeCase(TelegramCase):
    PHRASE = "напиши маме в телеграм привет"

    def ask(self):
        client = self.client(tools_reply(self.send_call(text="привет")))
        return client, client.respond(self.PHRASE)


class TestSettings(unittest.TestCase):
    def mode(self, text):
        with patch("core.settings.ENV_FILE", env_file(text)):
            return tg.focus_mode_setting()

    def test_mode_values(self):
        self.assertEqual(self.mode(""), ("reopen", None))
        self.assertEqual(self.mode("TELEGRAM_FOCUS_MODE=reopen\n"), ("reopen", None))
        self.assertEqual(self.mode("TELEGRAM_FOCUS_MODE=direct\n"), ("direct", None))
        self.assertEqual(self.mode("TELEGRAM_FOCUS_MODE= Direct \n"), ("direct", None))
        self.assertEqual(self.mode("TELEGRAM_FOCUS_MODE=REOPEN\n"), ("reopen", None))
        self.assertEqual(self.mode("TELEGRAM_FOCUS_MODE=\n"), ("reopen", None))          # пусто — по умолчанию
        for unsupported in ("type", "click", "мусор", "reopen2", "1"):
            self.assertEqual(self.mode(f"TELEGRAM_FOCUS_MODE={unsupported}\n"), ("reopen", unsupported), unsupported)

    def test_unsupported_mode_uses_reopen_and_warns(self):
        for unsupported in ("type", "click"):
            with patch("core.settings.ENV_FILE", env_file(f"TELEGRAM_FOCUS_MODE={unsupported}\n")):
                with self.assertLogs("core.apps.telegram", level="WARNING") as logs:
                    self.assertEqual(tg.focus_mode(), "reopen")
            self.assertIn(f"TELEGRAM_FOCUS_MODE={unsupported} не поддерживается", logs.output[0])
            self.assertIn("использую reopen", logs.output[0])

    def test_supported_modes_do_not_warn(self):
        for mode in ("reopen", "direct"):
            with patch("core.settings.ENV_FILE", env_file(f"TELEGRAM_FOCUS_MODE={mode}\n")):
                with self.assertNoLogs("core.apps.telegram", level="WARNING"):
                    self.assertEqual(tg.focus_mode(), mode)

    def test_reopen_wait_values(self):
        for raw, expected in [(None, 1.0), ("0.3", 0.3), ("0", 0.0), ("abc", 1.0), ("-2", 0.0), ("99", 5.0)]:
            with patch("core.settings.ENV_FILE", env_file("" if raw is None else f"TELEGRAM_REOPEN_WAIT={raw}\n")):
                self.assertEqual(tg.reopen_wait(), expected, raw)

    def test_env_example_has_the_three_settings(self):
        text = Path(__file__).with_name(".env.example").read_text(encoding="utf-8")
        for line in ("TELEGRAM_FOCUS_MODE=reopen", "TELEGRAM_REOPEN_WAIT=1.0", "TELEGRAM_PASTE_WAIT=1.0"):
            self.assertIn(line, text)
        self.assertEqual(tg.DEFAULT_FOCUS_MODE, "reopen")
        self.assertEqual((tg.DEFAULT_REOPEN_WAIT, tg.DEFAULT_PASTE_WAIT), (1.0, 1.0))


class TestPhase1Reopen(ModeCase):
    def test_exact_order_of_calls(self):
        self.ask()
        self.assertEqual(self.mac.events, [
            ("clipboard_info",), ("pbpaste",),
            URL, ("wait",),                      # открыть по ссылке, дождаться Telegram впереди (1/2)
            URL, ("sleep", 1.0),                 # открыть ту же ссылку ещё раз (2/2) и подождать TELEGRAM_REOPEN_WAIT
            ("pbcopy", "привет"), ("front",),    # текст в буфер, Telegram впереди
            ("pbpaste",),                        # буфер принял текст (длина), без вывода текста
            ("paste",), ("sleep", 1.0),          # Cmd+V, подождать TELEGRAM_PASTE_WAIT
            ("pbcopy", OLD),                     # вернуть прежний буфер
        ])

    def test_waits_follow_settings(self):
        with patch("core.apps.telegram.reopen_wait", return_value=0.3), \
                patch("core.apps.telegram.paste_wait", return_value=2.5):
            self.ask()
        sleeps = [e[1] for e in self.mac.events if e[0] == "sleep"]
        self.assertEqual(sleeps, [0.3, 2.5])

    def test_enter_never_before_yes_no_delete_no_select_all(self):
        client, turn = self.ask()
        self.assertEqual(len(turn.pending), 1)
        names = self.mac.names()
        self.assertNotIn("enter", names)
        for forbidden in ("delete", "space", "click", "paste_keycode", "menu_click"):
            self.assertNotIn(forbidden, names)
        self.assertEqual(names.count("paste"), 1)                           # единственное нажатие клавиш — Cmd+V

    def test_second_open_failure_is_reported_and_buffer_restored(self):
        self.mac.fail_open_number = 2
        client, turn = self.ask()
        self.assertEqual(turn.pending, [])
        self.assertEqual(turn.speech, "Не получилось: не удалось повторно открыть чат.")
        self.assertNotIn("paste", self.mac.names())                         # Cmd+V не нажат
        self.assertNotIn("enter", self.mac.names())
        self.assertEqual(self.mac.events[-1], ("pbcopy", OLD))              # буфер возвращён
        self.assertEqual(self.mac.pasteboard, OLD)
        self.assertEqual(self.mac.names().count("open"), 2)                 # без повторных попыток
        self.assertIsNone(tg._state)

    def test_first_open_failure_also_restores_buffer(self):
        self.mac.fail_open_number = 1
        client, turn = self.ask()
        self.assertEqual(turn.pending, [])
        self.assertEqual(self.mac.pasteboard, OLD)
        self.assertEqual(self.mac.names().count("open"), 1)

    def test_window_never_comes_forward_skips_second_open(self):
        self.mac.window = "timeout"
        client, turn = self.ask()
        self.assertEqual(self.mac.names().count("open"), 1)
        self.assertIn("Telegram не открылся", turn.speech)
        self.assertEqual(self.mac.pasteboard, OLD)

    def test_not_in_front_after_second_open_presses_nothing(self):
        self.mac.front = "com.apple.Safari"
        client, turn = self.ask()
        self.assertEqual(turn.speech, "Не получилось: Telegram не в фокусе.")
        self.assertNotIn("paste", self.mac.names())
        self.assertEqual(self.mac.pasteboard, OLD)

    def test_log_steps_without_text(self):
        with self.assertLogs("core.apps.telegram", level="INFO") as logs:
            self.ask()
        joined = "\n".join(logs.output)
        order = [joined.index(x) for x in ("чат «мама» открыт по ссылке (1/2)", "на переднем плане с окном — да, ждали",
                                           "повторное открытие (2/2)", "после повторного открытия ждали 1.0 с",
                                           "буфер обмена принял текст, длина 6", "вставка нажата (способ keystroke), длина текста 6",
                                           "после вставки ждали 1.0 с",
                                           "прежний буфер обмена возвращён")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("привет", joined)
        self.assertNotIn("mama_user", joined)


class TestPhase1Direct(ModeCase):
    def setUp(self):
        super().setUp()
        patcher = patch("core.apps.telegram.focus_mode", return_value="direct")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_open_and_old_pause(self):
        self.ask()
        self.assertEqual(self.mac.events, [
            ("clipboard_info",), ("pbpaste",), URL, ("wait",), ("sleep", tg.PASTE_DELAY), ("pbcopy", "привет"),
            ("front",), ("pbpaste",), ("paste",), ("sleep", 1.0), ("pbcopy", OLD)])
        self.assertEqual(self.mac.names().count("open"), 1)

    def test_log_says_one_of_one(self):
        with self.assertLogs("core.apps.telegram", level="INFO") as logs:
            self.ask()
        joined = "\n".join(logs.output)
        self.assertIn("открыт по ссылке (1/1)", joined)
        self.assertNotIn("повторное открытие", joined)


class TestPhase2(ModeCase):
    def confirm(self):
        client, turn = self.ask()
        before = len(self.mac.events)
        results = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        return results, self.mac.events[before:]

    def test_reopen_order_open_wait_open_pause_front_enter(self):
        results, events = self.confirm()
        self.assertTrue(results[0]["ok"], results[0])
        self.assertEqual(results[0]["speech"], "Нажал Enter в чате с Мама. Если сообщения нет, поле было пустым.")
        self.assertEqual(events, [URL, ("wait",), URL, ("sleep", 1.0), ("front",), ("enter",)])
        self.assertEqual(self.mac.names().count("enter"), 1)

    def test_enter_only_after_yes(self):
        client, turn = self.ask()
        self.assertNotIn("enter", self.mac.names())
        client.reject_actions(turn.pending, self.PHRASE, "отменено пользователем", confirmed=False)
        self.assertNotIn("enter", self.mac.names())
        [late] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertFalse(late["ok"])
        self.assertNotIn("enter", self.mac.names())

    def test_pause_before_enter_is_reopen_wait(self):
        with patch("core.apps.telegram.reopen_wait", return_value=0.4):
            client, turn = self.ask()
            before = len(self.mac.events)
            client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertEqual([e for e in self.mac.events[before:] if e[0] == "sleep"], [("sleep", 0.4)])

    def test_second_open_failure_means_no_enter(self):
        client, turn = self.ask()
        self.mac.fail_open_number = self.mac.open_count + 2          # второе открытие фазы 2
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["short"], "не удалось повторно открыть чат")
        self.assertNotIn("enter", self.mac.names())
        self.assertEqual(self.mac.names().count("open"), 4)          # две в фазе 1 и две в фазе 2, без повторов

    def test_not_in_front_before_enter(self):
        client, turn = self.ask()
        self.mac.front = "com.apple.Safari"
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertEqual(result["short"], "Telegram не в фокусе")
        self.assertNotIn("enter", self.mac.names())

    def test_window_does_not_come_forward(self):
        client, turn = self.ask()
        self.mac.window = "timeout"
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertEqual(result["short"], "Telegram не в фокусе")
        self.assertNotIn("enter", self.mac.names())
        self.assertEqual(self.mac.names()[-1], "wait")                  # дальше не пошли

    def test_enter_failure_no_retry(self):
        client, turn = self.ask()
        self.mac.fail_on = {"enter": "not allowed to send keystrokes (1002)"}
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.mac.names().count("enter"), 1)

    def test_phase2_log_has_both_opens_without_text(self):
        client, turn = self.ask()
        with self.assertLogs("core.apps.telegram", level="INFO") as logs:
            client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        joined = "\n".join(logs.output)
        self.assertIn("(1/2)", joined)
        self.assertIn("повторное открытие (2/2)", joined)
        self.assertNotIn("привет", joined)


class TestPhase2Direct(ModeCase):
    def test_direct_phase2_is_unchanged(self):
        with patch("core.apps.telegram.focus_mode", return_value="direct"):
            client, turn = self.ask()
            before = len(self.mac.events)
            [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self.mac.events[before:], [URL, ("wait",), ("front",), ("sleep", tg.BEFORE_ENTER_DELAY),
                                                    ("enter",)])
        self.assertEqual(self.mac.names().count("open"), 2)           # по одному в каждой фазе


class TestWithRealEnvFunctions(ModeCase):
    """focus_mode / reopen_wait не подменены: значения берутся из (подменённого) .env."""

    def test_default_without_settings_is_reopen_with_one_second(self):
        self.ask()
        self.assertEqual(self.mac.names().count("open"), 2)
        self.assertEqual([e[1] for e in self.mac.events if e[0] == "sleep"], [1.0, 1.0])

    def test_direct_from_env(self):
        with patch("core.settings.ENV_FILE", env_file("TELEGRAM_FOCUS_MODE=direct\n")):
            self.ask()
        self.assertEqual(self.mac.names().count("open"), 1)

    def test_unsupported_from_env_falls_back_to_reopen(self):
        with patch("core.settings.ENV_FILE", env_file("TELEGRAM_FOCUS_MODE=click\nTELEGRAM_REOPEN_WAIT=0.2\n")):
            with self.assertLogs("core.apps.telegram", level="WARNING"):
                self.ask()
        self.assertEqual(self.mac.names().count("open"), 2)
        self.assertIn(("sleep", 0.2), self.mac.events)


class TestDiagnosticsUseProductionValues(CheckCase):
    def test_header_prints_env_values(self):
        with patch("core.settings.ENV_FILE", env_file("TELEGRAM_FOCUS_MODE=direct\nTELEGRAM_REOPEN_WAIT=0.7\n")):
            code, text, _ = self.run_check(User(verdicts=["н"]), "--only", "Б")
        self.assertIn("Боевой режим из .env: TELEGRAM_FOCUS_MODE=direct, TELEGRAM_REOPEN_WAIT=0.7, "
                      "TELEGRAM_PASTE_WAIT=1.0", text)

    def test_header_warns_about_unsupported_mode(self):
        with patch("core.settings.ENV_FILE", env_file("TELEGRAM_FOCUS_MODE=click\n")):
            code, text, _ = self.run_check(User(verdicts=["н"]), "--only", "Б")
        self.assertIn("TELEGRAM_FOCUS_MODE=click не поддерживается", text)
        self.assertIn("reopen", text.splitlines()[3])

    def test_variant_b_repeats_the_production_sequence(self):
        self.run_check(User(verdicts=["н"]), "--only", "Б")
        names = self.mac.names()
        start = names.index("clipboard_info")
        self.assertEqual(names[start:], ["clipboard_info", "pbpaste", "open", "wait", "open", "pbcopy", "front", "pbpaste",
                                         "paste", "pbcopy"])
        self.mac = FakeMac()
        with patch("core.settings.ENV_FILE", env_file("TELEGRAM_FOCUS_MODE=direct\n")):
            self.run_check(User(verdicts=["н"]), "--only", "Б")
        self.assertEqual(self.mac.names().count("open"), 2)          # открытие шага 3 диагностики + один в варианте Б

    def test_variant_a_is_the_same_path_with_zero_wait(self):
        self.run_check(User(verdicts=["н"]), "--only", "А")
        paste = self.mac.events.index(("paste",))
        self.assertEqual(self.mac.events[paste + 1:paste + 3], [("sleep", 0.0), ("pbcopy", OLD)])     # пауза 0, сразу
        self.assertEqual(self.mac.names().count("open"), 3)                # шаг 3 диагностики + 2 открытия боевого пути

    def test_variant_d_stays_fixed(self):
        self.run_check(User(verdicts=["н"]), "--only", "Д")
        self.assertIn(("sleep", check.REOPEN_WAIT), self.mac.events)

    def test_type_and_click_lines_are_marked_unsupported(self):
        code, text, _ = self.run_check(User(verdicts=["н", "д", "н"], confirms=["д", "д"]), "--only", "Д,Г,В")
        self.assertIn("боевой код пока поддерживает только reopen и direct", text)


if __name__ == "__main__":
    unittest.main()
