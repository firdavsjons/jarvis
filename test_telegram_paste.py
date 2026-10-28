"""Этап 7.1.5: способ вставки (TELEGRAM_PASTE_METHOD: keystroke / keycode / menu), меню «Изменить», проверка буфера,
честная фраза после Enter, диагностика --repeat. Только моки: Telegram не запускается, клавиши не нажимаются."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

from core import journal
from core import telegram_check as check
from core.ai import claims_unperformed_action
from core.apps import telegram as tg
from test_telegram import FakeMac, TelegramCase
from test_telegram_check import CheckCase
from test_telegram_mode import env_file

OLD = "старый буфер"
HONEST = "Нажал Enter в чате с Мама. Если сообщения нет, поле было пустым."


def with_env(testcase, text):
    patcher = patch("core.settings.ENV_FILE", env_file(text))
    patcher.start()
    testcase.addCleanup(patcher.stop)


class PasteCase(TelegramCase):
    PHRASE = "напиши маме в телеграм привет"

    def ask(self, method=None):
        if method:
            with_env(self, f"TELEGRAM_PASTE_METHOD={method}\n")
        client = self.client(__import__("test_tools").tools_reply(self.send_call(text="привет")))
        return client, client.respond(self.PHRASE)


class TestSetting(unittest.TestCase):
    def test_values(self):
        def setting(text):
            with patch("core.settings.ENV_FILE", env_file(text)):
                return tg.paste_method_setting()
        self.assertEqual(setting(""), ("keystroke", None))
        self.assertEqual(setting("TELEGRAM_PASTE_METHOD=menu\n"), ("menu", None))
        self.assertEqual(setting("TELEGRAM_PASTE_METHOD= KeyCode \n"), ("keycode", None))
        self.assertEqual(setting("TELEGRAM_PASTE_METHOD=keystroke\n"), ("keystroke", None))
        for bad in ("type", "click", "мусор", "key code"):
            self.assertEqual(setting(f"TELEGRAM_PASTE_METHOD={bad}\n"), ("keystroke", bad))

    def test_unsupported_warns_and_uses_keystroke(self):
        with patch("core.settings.ENV_FILE", env_file("TELEGRAM_PASTE_METHOD=click\n")):
            with self.assertLogs("core.apps.telegram", level="WARNING") as logs:
                self.assertEqual(tg.paste_method(), "keystroke")
        self.assertIn("TELEGRAM_PASTE_METHOD=click не поддерживается", logs.output[0])

    def test_env_example(self):
        text = Path(__file__).with_name(".env.example").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_PASTE_METHOD=keystroke", text)


class TestMethodsInPhase1(PasteCase):
    def test_default_is_keystroke_without_menu_reads(self):
        self.ask()
        names = self.mac.names()
        self.assertEqual(names.count("paste"), 1)
        for forbidden in ("paste_keycode", "menu_bar", "menu_items", "menu_enabled", "menu_click"):
            self.assertNotIn(forbidden, names)

    def test_keycode(self):
        self.ask("keycode")
        names = self.mac.names()
        self.assertEqual(names.count("paste_keycode"), 1)
        self.assertNotIn("paste", names)
        self.assertNotIn("menu_bar", names)          # состояние пункта для keycode не читается

    def test_menu_reads_then_clicks(self):
        client, turn = self.ask("menu")
        names = self.mac.names()
        start = names.index("menu_bar")
        self.assertEqual(names[start:start + 4], ["menu_bar", "menu_items", "menu_enabled", "menu_click"])
        self.assertIn(("menu_click", "Правка", "Вставить"), self.mac.events)
        self.assertNotIn("paste", names)
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(self.mac.pasteboard, OLD)

    def test_menu_named_izmenit_edit_pravka(self):
        cases = {"Apple, Telegram, Изменить, Вид, Окно": "Изменить", "Apple, Telegram, Файл, Правка, Вид": "Правка",
                 "Apple, Telegram, File, Edit, View": "Edit", "Apple, Telegram, ИЗМЕНИТЬ, Вид": "ИЗМЕНИТЬ"}
        for bar, name in cases.items():
            with self.subTest(bar=bar):
                self.mac = FakeMac()
                self.mac.menu_bar = bar
                tg._state = None
                self.ask("menu")
                self.assertIn(("menu_click", name, "Вставить"), self.mac.events)

    def test_english_paste_item_and_exact_match_only(self):
        self.mac.menu_bar, self.mac.menu_items = "Apple, Telegram, Edit", "Undo, Redo, Cut, Copy, Paste, Paste and Match Style"
        self.ask("menu")
        self.assertIn(("menu_click", "Edit", "Paste"), self.mac.events)
        self.mac = FakeMac()
        self.mac.menu_items = "Отменить, Вставить и согласовать стиль"
        client, turn = self.ask("menu")
        self.assertEqual(turn.speech, "Не получилось: пункт меню не найден.")        # «…и согласовать стиль» не подходит

    def test_menu_not_found_is_clear_and_not_retried(self):
        for field, value in (("menu_bar", "Apple, Telegram, Файл, Вид"), ("menu_items", "Отменить, Копировать")):
            with self.subTest(field=field):
                self.mac = FakeMac()
                setattr(self.mac, field, value)
                tg._state = None
                client, turn = self.ask("menu")
                self.assertEqual(turn.speech, "Не получилось: пункт меню не найден.")
                self.assertEqual(turn.pending, [])
                names = self.mac.names()
                self.assertNotIn("menu_click", names)
                self.assertNotIn("paste", names)
                self.assertEqual(names.count("menu_bar"), 1)                          # без повторов
                self.assertEqual(self.mac.pasteboard, OLD)                             # буфер возвращён
                self.assertEqual(self.mac.events[-1], ("pbcopy", OLD))

    def test_disabled_paste_is_rechecked_after_half_a_second_then_refused(self):
        self.mac.paste_reads = [False]
        client, turn = self.ask("menu")
        self.assertEqual(turn.speech, "Не получилось: поле ввода не в фокусе (пункт «Вставить» недоступен).")
        names = self.mac.names()
        self.assertEqual(names.count("menu_enabled"), 2)                              # повторное чтение
        self.assertIn(("sleep", 0.5), self.mac.events)
        self.assertLess(self.mac.events.index(("sleep", 0.5)), len(self.mac.events) - 1)
        self.assertNotIn("menu_click", names)
        self.assertEqual(self.mac.pasteboard, OLD)
        self.assertIsNone(tg._state)

    def test_stale_disabled_state_recovers_on_recheck(self):
        self.mac.paste_reads = [False, True]
        client, turn = self.ask("menu")
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(self.mac.names().count("menu_click"), 1)
        sleeps = [e[1] for e in self.mac.events if e[0] == "sleep"]
        self.assertIn(0.5, sleeps)

    def test_enabled_state_not_read_twice(self):
        self.mac.paste_reads = [True]
        self.ask("menu")
        self.assertEqual(self.mac.names().count("menu_enabled"), 1)               # доступен — повторного чтения нет

    def test_state_unreadable_does_not_block_click(self):
        self.mac.fail_on = {"menu_enabled": "no property"}
        client, turn = self.ask("menu")
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(self.mac.names().count("menu_click"), 1)

    def test_keystroke_and_keycode_ignore_the_menu_state(self):
        self.mac.paste_reads = [False]
        for method in ("keystroke", "keycode"):
            with self.subTest(method=method):
                self.mac = FakeMac()
                self.mac.paste_reads = [False]
                tg._state = None
                client, turn = self.ask(method)
                self.assertEqual(len(turn.pending), 1)
                self.assertNotIn("menu_enabled", self.mac.names())

    def test_menu_click_permission_error_names_section_without_retry(self):
        self.mac.fail_on = {"menu_click": "osascript is not allowed assistive access. (-25211)"}
        client, turn = self.ask("menu")
        self.assertIn("Универсальный доступ", turn.speech)
        self.assertEqual(self.mac.names().count("menu_click"), 1)
        self.assertEqual(self.mac.pasteboard, OLD)

    def test_logs_name_menu_but_no_text(self):
        with_env(self, "TELEGRAM_PASTE_METHOD=menu\n")
        with self.assertLogs("core.apps.telegram", level="INFO") as logs:
            client = self.client(__import__("test_tools").tools_reply(self.send_call(text="приветствие-теста")))
            client.respond("напиши маме в телеграм приветствие-теста")
        joined = "\n".join(logs.output)
        self.assertIn("меню «Правка», пункт «Вставить» найден, доступен: да", joined)
        self.assertIn("вставка нажата (способ menu)", joined)
        self.assertNotIn("приветствие-теста", joined)

    def test_enter_never_before_yes_for_every_method(self):
        for method in tg.PASTE_METHODS:
            with self.subTest(method=method):
                self.mac = FakeMac()
                tg._state = None
                client, turn = self.ask(method)
                self.assertEqual(len(turn.pending), 1)
                self.assertNotIn("enter", self.mac.names())
                for forbidden in ("delete", "space", "click", "menu_open", "escape"):
                    self.assertNotIn(forbidden, self.mac.names())
                client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
                self.assertEqual(self.mac.names().count("enter"), 1)
                self.assertEqual(self.mac.names()[-1], "enter")


class TestBufferCheck(PasteCase):
    def test_mismatch_means_no_paste_for_every_method(self):
        for method in tg.PASTE_METHODS:
            with self.subTest(method=method):
                self.mac = FakeMac()
                self.mac.verify_mangle = True
                tg._state = None
                with self.assertLogs("core.apps.telegram", level="WARNING") as logs:
                    client, turn = self.ask(method)
                self.assertEqual(turn.speech, "Не получилось: буфер обмена не принял текст.")
                self.assertEqual(turn.pending, [])
                names = self.mac.names()
                for pressed in ("paste", "paste_keycode", "menu_click", "menu_bar", "enter"):
                    self.assertNotIn(pressed, names)
                self.assertEqual(self.mac.events[-1], ("pbcopy", OLD))                  # прежний буфер возвращён
                self.assertIn("буфер обмена не принял текст (в буфере 3 симв., нужно 6)", "\n".join(logs.output))
                self.assertNotIn("привет", "\n".join(logs.output))

    def test_match_is_checked_after_focus_and_before_paste(self):
        self.ask()
        names = self.mac.names()
        self.assertEqual(names[names.index("pbcopy"):names.index("pbcopy") + 4], ["pbcopy", "front", "pbpaste", "paste"])

    def test_check_covers_text_difference_not_only_length(self):
        class Altered(FakeMac):
            def __call__(self, args, **kwargs):
                if args[0] == "pbpaste" and self.events and self.events[-1][0] == "front":
                    self.events.append(("pbpaste",))
                    return __import__("test_telegram").completed("привеТ")         # та же длина, другой текст
                return super().__call__(args, **kwargs)
        self.mac = Altered()
        client, turn = self.ask()
        self.assertEqual(turn.speech, "Не получилось: буфер обмена не принял текст.")

    def test_clipboard_text_not_printed_anywhere(self):
        self.mac.verify_mangle = True
        with self.assertLogs("core.apps.telegram", level="WARNING") as logs:
            self.ask()
        self.assertNotIn("???", "\n".join(logs.output))


class TestHonestPhrase(PasteCase):
    def test_after_enter_phrase_in_voice_window_and_journal(self):
        path = Path(tests_base.TEMP_ROOT) / "honest-actions.jsonl"
        path.unlink(missing_ok=True)
        old = journal.JOURNAL_FILE
        journal.setup_journal(path)
        self.addCleanup(setattr, journal, "JOURNAL_FILE", old)
        client, turn = self.ask()
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertEqual(result["speech"], HONEST)
        self.assertEqual(result["message"], "✅ " + HONEST)                              # окно
        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["result"], "✅ " + HONEST)                                # журнал
        self.assertNotIn("отправку", json.dumps(entry, ensure_ascii=False))
        self.assertNotIn("привет", json.dumps(entry, ensure_ascii=False))

    def test_old_phrase_is_gone_and_new_one_is_a_claim_without_a_call(self):
        self.assertTrue(claims_unperformed_action("", "Нажал Enter в чате с Мама."))
        self.assertTrue(claims_unperformed_action("", HONEST))
        self.assertFalse(claims_unperformed_action("до скольки открыт музей", "Музей открыт до 18:00."))
        source = open(tg.__file__, encoding="utf-8").read()
        self.assertNotIn("Нажал отправку", source)

    def test_history_keeps_action_line_not_the_phrase(self):
        client, turn = self.ask()
        client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        from core.history import action_line
        results = client.run_actions([{"name": "telegram", "args": {"action": "open_chat", "to": "мама", "text": ""}}],
                                     phrase="открой чат с мамой")
        self.assertTrue(action_line(results).startswith("[действие: открыл чат с Мама"))


class RepeatUser:
    """input(): стирание черновика — Enter (или «п» по номеру), «Появился ли текст?» — ответы по очереди."""

    def __init__(self, verdicts=(), skip_erase=()):
        self.verdicts, self.skip_erase = list(verdicts), set(skip_erase)
        self.erase_count, self.questions = 0, []

    def __call__(self, prompt=""):
        self.questions.append(prompt)
        if "Сотрите черновик" in prompt:
            self.erase_count += 1
            return "п" if self.erase_count in self.skip_erase else ""
        if "Появился ли текст" in prompt:
            return self.verdicts.pop(0) if self.verdicts else "н"
        return ""


class RepeatCase(CheckCase):
    def run_repeat(self, user=None, *argv):
        user, out = user or RepeatUser(), io.StringIO()
        code = check.main(list(argv), input_fn=user, out=out)
        return code, out.getvalue(), user

    def pastes(self):
        return [n for n in self.mac.names() if n in ("paste", "paste_keycode", "menu_click")]


class TestRepeatMode(RepeatCase):
    def test_default_is_repeat_three_methods_three_attempts(self):
        code, text, user = self.run_repeat(RepeatUser(verdicts=["д"] * 9))
        self.assertEqual(code, 0)
        self.assertEqual(user.erase_count, 9)
        self.assertEqual(sum("Появился ли текст" in q for q in user.questions), 9)
        self.assertEqual(self.pastes(), ["paste"] * 3 + ["paste_keycode"] * 3 + ["menu_click"] * 3)
        for expected in ("Способ keystroke", "Способ keycode", "Способ menu", "Попытка 1/3", "Попытка 3/3"):
            self.assertIn(expected, text)

    def test_attempt_is_production_phase_1_each_time(self):
        self.run_repeat(RepeatUser(verdicts=["д"]), "--methods", "keystroke", "--repeat", "1")
        names = self.mac.names()
        start = names.index("clipboard_info")
        # боевая фаза 1 целиком; чтение состояния «Вставить» (menu_*) — проба диагностики перед вставкой, без клика
        self.assertEqual(names[start:], ["clipboard_info", "pbpaste", "open", "wait", "open", "pbcopy", "front", "pbpaste",
                                         "menu_bar", "menu_items", "menu_enabled", "paste", "pbcopy"])
        self.assertEqual(names.count("open"), 3)          # шаг 3 диагностики + два открытия боевого пути
        self.assertEqual(self.mac.pasteboard, OLD)

    def test_script_erases_nothing_and_never_presses_enter(self):
        user = RepeatUser(verdicts=["д", "н", "д"] * 3)
        self.run_repeat(user, "--open-menu")
        names = self.mac.names()
        for forbidden in ("enter", "delete", "space", "click"):
            self.assertNotIn(forbidden, names)
        self.assertTrue(all("Сотрите черновик в Telegram вручную (скрипт его не стирает)" in q
                            for q in user.questions if "Сотрите" in q))
        texts = {e[1] for e in self.mac.events if e[0] in ("pbcopy", "clip_as")}
        self.assertEqual(texts, {check.TEST_TEXT, OLD})

    def test_answers_yes_no_skip_in_both_alphabets(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["y", "n", "s"]), "--methods", "keystroke")
        self.assertIn("keystroke  1:д  2:н  3:—", text)
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д", "нет", "п"]), "--methods", "keystroke")
        self.assertIn("keystroke  1:д  2:н  3:—", text)

    def test_skip_erase_skips_the_attempt(self):
        user = RepeatUser(verdicts=["д", "д"], skip_erase={2})
        code, text, _ = self.run_repeat(user, "--methods", "keystroke")
        self.assertEqual(self.pastes(), ["paste", "paste"])
        self.assertIn("1:д  2:—  3:д", text)

    def test_repeat_and_methods_options(self):
        code, text, user = self.run_repeat(RepeatUser(verdicts=["д"] * 4), "--repeat", "2", "--methods", "keycode,menu")
        self.assertEqual(self.pastes(), ["paste_keycode"] * 2 + ["menu_click"] * 2)
        self.assertEqual(user.erase_count, 4)
        for bad in (["--repeat", "0"], ["--repeat", "11"], ["--methods", "type"], ["--methods", ","]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                check.main(bad, input_fn=RepeatUser(), out=io.StringIO())

    def test_methods_deduplicated_keep_order(self):
        self.assertEqual(check.parse_methods("menu, keystroke,menu"), ["menu", "keystroke"])
        self.assertEqual(check.parse_methods("KEYCODE"), ["keycode"])

    def test_old_variants_still_available(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["н"]), "--only", "А")
        self.assertIn("Вариант А", text)
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["н"] * 8), "--variants")
        self.assertIn("Вариант Е", text)


class TestRepeatTable(RepeatCase):
    def test_matrix_and_state_column(self):
        self.mac.paste_reads = [True, True, False]
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д", "д", "н"]), "--methods", "keystroke")
        self.assertIn("Способ × попытка", text)
        self.assertIn("keystroke  1:д  2:д  3:н", text)
        self.assertIn("Вставить: доступен/недоступен/не прочитано", text)
        self.assertIn("keystroke  | 1 | д | доступен", text)
        self.assertIn("keystroke  | 3 | н | недоступен", text)

    def test_state_shown_next_to_the_question_in_every_attempt(self):
        self.mac.paste_reads = [True, False]
        user = RepeatUser(verdicts=["д", "н"])
        self.run_repeat(user, "--methods", "keystroke", "--repeat", "2")
        questions = [q for q in user.questions if "Появился ли текст" in q]
        self.assertIn("[Вставить перед Cmd+V: доступен]", questions[0])
        self.assertIn("[Вставить перед Cmd+V: недоступен]", questions[1])

    def test_unreadable_state(self):
        self.mac.menu_bar = "Apple, Telegram, Файл, Вид"
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"]), "--methods", "keystroke", "--repeat", "1")
        self.assertIn("keystroke  | 1 | д | не прочитано", text)

    def test_menu_method_refusal_is_recorded_as_error_without_question(self):
        self.mac.paste_reads = [False]
        user = RepeatUser(verdicts=[])
        code, text, _ = self.run_repeat(user, "--methods", "menu", "--repeat", "2")
        self.assertEqual(sum("Появился ли текст" in q for q in user.questions), 0)
        self.assertIn("menu       1:О  2:О", text)
        self.assertIn("поле ввода не в фокусе (пункт «Вставить» недоступен)", text)
        self.assertIn("Способ menu отказался нажимать 2 раз(а)", text)
        self.assertNotIn("menu_click", self.mac.names())

    def test_menu_items_printed_once_without_click(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["н"] * 3), "--methods", "keystroke")
        self.assertEqual(text.count("пункты (чтение, без клика)"), 1)
        self.assertIn("меню «Правка», пункты (чтение, без клика): Отменить, Вставить, Вставить и согласовать стиль", text)

    def test_izmenit_menu_from_the_real_telegram(self):
        self.mac.menu_bar = "Apple, Telegram, Изменить, Вид, Окно"
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"]), "--methods", "menu", "--repeat", "1")
        self.assertIn("меню «Изменить», пункты (чтение, без клика)", text)
        self.assertIn(("menu_click", "Изменить", "Вставить"), self.mac.events)


class TestOpenMenuFlag(RepeatCase):
    def test_open_close_once_per_attempt_and_both_states_printed(self):
        self.mac.paste_reads = [False, True]           # до открытия — недоступен, после — доступен
        code, text, user = self.run_repeat(RepeatUser(verdicts=["д"]), "--methods", "keystroke", "--repeat", "1",
                                           "--open-menu")
        names = self.mac.names()
        self.assertEqual(names.count("menu_open"), 1)
        self.assertEqual(names.count("escape"), 1)
        self.assertLess(names.index("menu_open"), names.index("escape"))
        self.assertLess(names.index("escape"), names.index("paste"))                 # меню закрыто до Cmd+V
        self.assertEqual(names.count("menu_click"), 0)                               # по пунктам не кликали
        self.assertIn(("menu_open", "Правка"), self.mac.events)                      # клик по строке меню
        self.assertIn("Вставить перед Cmd+V: недоступен; после открытия меню: доступен", text)
        self.assertIn("keystroke  | 1 | д | недоступен | доступен", text)

    def test_per_attempt(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"] * 2), "--methods", "keystroke", "--repeat", "2",
                                        "--open-menu")
        self.assertEqual(self.mac.names().count("menu_open"), 2)
        self.assertEqual(self.mac.names().count("escape"), 2)

    def test_no_escape_when_menu_did_not_open(self):
        self.mac.fail_on = {"menu_open": "can't click"}
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"]), "--methods", "keystroke", "--repeat", "1", "--open-menu")
        self.assertEqual(self.mac.names().count("escape"), 0)          # Escape в Telegram закрыл бы чат
        self.assertIn("после открытия меню: не прочитано", text)

    def test_without_flag_menu_is_never_opened(self):
        self.run_repeat(RepeatUser(verdicts=["д"] * 3), "--methods", "keystroke")
        self.assertNotIn("menu_open", self.mac.names())
        self.assertNotIn("escape", self.mac.names())

    def test_scripts_click_only_the_menu_bar_item(self):
        text = " ".join(check._MENU_OPEN)
        self.assertIn("click menu bar item (item 2 of argv) of menu bar 1", text)
        self.assertNotIn("click menu item", text)
        self.assertIn("key code 53", " ".join(check._ESCAPE))


class TestRepeatVerdict(RepeatCase):
    def test_best_method_and_env_line(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"] * 9))
        self.assertIn("Способ «keystroke» сработал во всех попытках.", text)
        self.assertIn("Также стабильны: keycode, menu.", text)
        self.assertIn("TELEGRAM_PASTE_METHOD=keystroke", text)

    def test_keycode_when_keystroke_fails(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["н"] * 3 + ["д"] * 3 + ["н"] * 3))
        self.assertIn("Способ «keycode» сработал во всех попытках.", text)
        self.assertIn("    TELEGRAM_PASTE_METHOD=keycode", text)
        self.assertNotIn("Также стабильны", text)

    def test_menu_when_only_menu_works(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["н"] * 6 + ["д"] * 3))
        self.assertIn("    TELEGRAM_PASTE_METHOD=menu", text)

    def test_one_failure_disqualifies_a_method(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д", "н", "д"] + ["д"] * 3 + ["н"] * 3))
        self.assertIn("    TELEGRAM_PASTE_METHOD=keycode", text)

    def test_nothing_stable_gives_partial_report_and_help(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д", "н", "н"] + ["н"] * 6))
        self.assertIn("Ни один способ не сработал во всех попытках; частично сработали: keystroke.", text)
        self.assertIn("Опишите, пожалуйста, что видно в окне Telegram", text)
        self.assertNotIn("    TELEGRAM_PASTE_METHOD=", text)

    def test_nothing_at_all(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["н"] * 9))
        self.assertIn("Ни один способ не сработал.", text)
        self.assertIn("1)", text)
        self.assertIn("Правка / Изменить", text)

    def test_skipped_attempts_do_not_count_as_failures(self):
        user = RepeatUser(verdicts=["д", "д"], skip_erase={2})
        code, text, _ = self.run_repeat(user, "--methods", "keystroke")
        self.assertIn("    TELEGRAM_PASTE_METHOD=keystroke", text)

    def test_unreliable_indicator_warning_when_grey_but_text_appeared(self):
        self.mac.paste_reads = [False]
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д", "д"]), "--methods", "keystroke", "--repeat", "2")
        self.assertIn("Состояние пункта «Вставить» не является надёжным индикатором фокуса в вашем Telegram, "
                      "не используйте режим menu как проверку", text)
        self.assertNotIn("режим menu можно использовать как проверку фокуса", text)
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"]), "--methods", "keycode", "--repeat", "1")
        self.assertIn("не является надёжным индикатором", text)

    def test_state_matching_result_is_reported_as_reliable(self):
        self.mac.paste_reads = [True, False]
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д", "н"]), "--methods", "keystroke", "--repeat", "2")
        self.assertIn("Состояние «Вставить» совпало с результатом", text)
        self.assertNotIn("не является надёжным индикатором", text)

    def test_grey_with_menu_method_does_not_trigger_unreliable_note(self):
        self.mac.paste_reads = [False]
        code, text, _ = self.run_repeat(RepeatUser(verdicts=[]), "--methods", "menu", "--repeat", "1")
        self.assertNotIn("не является надёжным индикатором", text)

    def test_header_mentions_paste_method(self):
        with patch("core.settings.ENV_FILE", env_file("TELEGRAM_PASTE_METHOD=menu\n")):
            code, text, _ = self.run_repeat(RepeatUser(verdicts=["н"]), "--methods", "keystroke", "--repeat", "1")
        self.assertIn("TELEGRAM_PASTE_METHOD=menu", text)


class TestLastRunRepeat(RepeatCase):
    def test_summary_saved_without_secrets(self):
        self.run_repeat(RepeatUser(verdicts=["д", "н", "д"] * 3))
        data = json.loads(self.last.read_text(encoding="utf-8"))
        self.assertEqual(data["repeat"]["keystroke"], [True, False, True])
        self.assertEqual(set(data["repeat"]), {"keystroke", "keycode", "menu"})
        raw = self.last.read_text(encoding="utf-8")
        for secret in (check.TEST_TEXT, "my_self_user", "mama_user", "999"):
            self.assertNotIn(secret, raw)

    def test_layout_comparison_for_repeat_runs(self):
        self.mac.layout = 'KeyboardLayout Name = "Russian";'
        self.run_repeat(RepeatUser(verdicts=["н"] * 9))
        self.mac = FakeMac()
        self.mac.layout = 'KeyboardLayout Name = "ABC";'
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"] * 3 + ["н"] * 6))
        self.assertIn("Раскладка ВЛИЯЕТ: keystroke: русской — есть «н», английской — все «д»", text)

    def test_same_layout_and_no_previous(self):
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"] * 9))
        self.assertIn("сравнить не с чем", text)
        code, text, _ = self.run_repeat(RepeatUser(verdicts=["д"] * 9))
        self.assertIn("та же раскладка", text)


class TestScriptsAndDocs(unittest.TestCase):
    def test_menu_scripts_are_shared_with_production(self):
        self.assertIs(check._MENU_BAR, tg._MENU_BAR)
        self.assertIs(check._MENU_CLICK, tg._MENU_CLICK)
        self.assertIs(check._PASTE_KEYCODE, tg._PASTE_KEYCODE)
        self.assertIn("enabled of every menu item", " ".join(tg._MENU_ENABLED))

    def test_edit_and_paste_names(self):
        self.assertEqual(tg.EDIT_MENUS, {"правка", "изменить", "edit"})
        self.assertEqual(tg.PASTE_ITEMS, {"вставить", "paste"})

    def test_docs_warn_about_type_and_click(self):
        text = Path(__file__).with_name("docs").joinpath("telegram.md").read_text(encoding="utf-8")
        for expected in ("TELEGRAM_PASTE_METHOD", "Изменить", "type", "click", "START", "диалог удаления",
                         "Нажал Enter в чате", "--repeat", "--methods", "--open-menu", "не подключены"):
            self.assertIn(expected, text, expected)
        self.assertNotIn("Нажал отправку", text)


if __name__ == "__main__":
    unittest.main()
