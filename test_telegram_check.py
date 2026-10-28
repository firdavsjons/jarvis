"""python -m core.telegram_check: диагностика вставки черновика (Этап 7.1.3). Только моки: Telegram не запускается,
клавиши и клики не выполняются, «пользователь» отвечает через подменённый input."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

from core import telegram_check as check
from core.apps import telegram as tg
from test_telegram import CLIENT, FakeMac, TelegramCase

SAFE_ALL_NO = ["н"] * 6          # З1, А, Б, Ж, З2, Д


class User:
    """input(): отличает вопросы по тексту. verdicts — ответы «Появился ли текст?» по порядку; confirms — «Продолжить?»;
    manual — ручная проверка; skip_erase — номера (с 1) просьб стереть черновик, на которые отвечаем «п»."""

    def __init__(self, verdicts=(), confirms=(), manual=(), skip_erase=()):
        self.verdicts, self.confirms, self.manual = list(verdicts), list(confirms), list(manual)
        self.skip_erase, self.erase_count, self.questions = set(skip_erase), 0, []

    def __call__(self, prompt=""):
        self.questions.append(prompt)
        if "сотрите его вручную" in prompt:
            self.erase_count += 1
            return "п" if self.erase_count in self.skip_erase else ""
        if "Продолжить?" in prompt:
            return self.confirms.pop(0) if self.confirms else "д"
        if "Кликните в поле ввода и нажмите Cmd+V руками" in prompt:
            return self.manual.pop(0) if self.manual else "п"
        if "Появился ли текст" in prompt:
            return self.verdicts.pop(0) if self.verdicts else "н"
        return ""


class CheckCase(TelegramCase):
    def setUp(self):
        super().setUp()
        self.last = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / "data" / "telegram_check_last.json"
        patcher = patch.object(check, "LAST_FILE", self.last)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_check(self, user=None, *argv):
        user, out = user or User(), io.StringIO()
        if not any(a in ("--only", "--repeat", "--methods", "--open-menu", "--variants") for a in argv):
            argv = ("--variants", *argv)          # эти тесты — про прежний набор вариантов (Этап 7.1.3)
        code = check.main(list(argv), input_fn=user, out=out)
        return code, out.getvalue(), user

    def pastes(self):
        return [n for n in self.mac.names() if n in ("paste", "paste_keycode", "menu_click")]


class TestOrderAndLayout(CheckCase):
    def test_layout_first_with_warning_then_permissions(self):
        code, text, _ = self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"], manual=["п"]))
        lines = text.splitlines()
        self.assertIn("Раскладка сейчас: ABC", lines[1])
        self.assertEqual(lines[2], "Для сравнения запустите диагностику дважды: с английской и с русской раскладкой.")
        self.assertLess(text.index("Раскладка сейчас"), text.index("1. Разрешения macOS"))
        self.assertEqual(self.mac.names()[0], "layout")

    def test_layout_kinds(self):
        for output, kind in {'"KeyboardLayout Name" = "U.S.";': "en", 'KeyboardLayout Name = "ABC";': "en",
                             'KeyboardLayout Name = "Russian";': "ru", 'KeyboardLayout Name = "Russian - PC";': "ru",
                             'KeyboardLayout Name = "German";': "other", "что-то другое": "?"}.items():
            with self.subTest(output=output):
                self.mac.layout = output
                self.assertEqual(check.current_layout()[1], kind)
        self.mac.fail_on = {"layout": "no domain"}
        self.assertEqual(check.current_layout(), ("не удалось определить", "?"))

    def test_variants_run_in_the_agreed_order(self):
        code, text, _ = self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"]))
        self.assertEqual(code, 0)
        positions = [text.index(f"Вариант {v}:") for v in ("Е", "З1", "А", "Б", "Ж", "З2", "Д", "Г", "В", "И")]
        self.assertEqual(positions, sorted(positions))
        # Cmd+V по порядку вариантов: З1 (key code 9), А, Б, Ж (keystroke), З2 (меню), Д, Г, В
        self.assertEqual(self.pastes(), ["paste_keycode", "paste", "paste", "paste", "menu_click", "paste", "paste", "paste"])

    def test_clipboard_check_runs_before_any_paste_and_asks_nothing(self):
        user = User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"])
        self.run_check(user)
        names = self.mac.names()
        self.assertLess(names.index("clip_as"), names.index("paste_keycode"))
        first_erase = next(i for i, q in enumerate(user.questions) if "сотрите его вручную" in q)
        self.assertEqual(user.questions[first_erase].count("сотрите"), 1)
        self.assertEqual(sum("в поле ввода? (д/н/п)" in q for q in user.questions), 8)   # Е — без вопроса

    def test_enter_is_never_pressed_and_only_fixed_texts_are_used(self):
        self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"], manual=["н"]))
        self.assertNotIn("enter", self.mac.names())
        texts = {e[1] for e in self.mac.events if e[0] in ("pbcopy", "clip_as")}
        self.assertEqual(texts, {check.TEST_TEXT, check.ASCII_TEXT, "старый буфер"})
        self.assertEqual(self.mac.pasteboard, "старый буфер")


class TestSafeFirstRiskySecond(CheckCase):
    def test_risky_variants_skipped_when_a_safe_one_worked(self):
        for worked in range(6):
            with self.subTest(worked=check.SAFE_VARIANTS[worked]):
                self.mac = FakeMac()
                verdicts = ["д" if i == worked else "н" for i in range(6)]
                code, text, user = self.run_check(User(verdicts=verdicts))
                self.assertNotIn("space", self.mac.names())            # пробел не набирался
                self.assertNotIn("delete", self.mac.names())
                self.assertNotIn("click", self.mac.names())            # кликов не было
                for vid in ("Г", "В"):
                    self.assertIn(f"Вариант {vid}: пропущен — не нужно, безопасный вариант сработал", text)
                self.assertNotIn("Вариант И", text)                    # ручная проверка не нужна
                self.assertFalse(any("Продолжить?" in q for q in user.questions))

    def test_risky_run_when_no_safe_variant_worked_after_confirmation(self):
        code, text, user = self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"]))
        self.assertEqual(sum("Продолжить?" in q for q in user.questions), 2)
        self.assertIn("ВНИМАНИЕ: будет набран один пробел", text)
        self.assertIn("ВНИМАНИЕ: будет ОДИН клик мышью", text)
        self.assertIn("START", text)
        self.assertEqual(self.mac.names().count("space"), 1)
        self.assertEqual(self.mac.names().count("click"), 1)

    def test_declined_confirmation_runs_nothing(self):
        code, text, _ = self.run_check(User(verdicts=SAFE_ALL_NO, confirms=["н", "п"]))
        self.assertNotIn("space", self.mac.names())
        self.assertNotIn("click", self.mac.names())
        self.assertIn("отказ пользователя", text)

    def test_skipped_safe_variants_do_not_count_as_worked(self):
        user = User(verdicts=[], confirms=["д", "д"], skip_erase={1, 2, 3, 4, 5, 6})     # все безопасные пропущены
        code, text, user = self.run_check(user)
        self.assertEqual(self.mac.names().count("space"), 1)                  # безопасных «д» нет → рискованные идут


class TestTypeVariant(CheckCase):
    def test_exactly_one_space_one_delete_then_paste(self):
        self.run_check(User(verdicts=SAFE_ALL_NO + ["н"], confirms=["д", "н"]), "--only", "Г")
        names = self.mac.names()
        self.assertEqual(names.count("space"), 1)
        self.assertEqual(names.count("delete"), 1)
        self.assertEqual(names[names.index("space"):names.index("space") + 3], ["space", "delete", "paste"])
        self.assertLess(names.index("front"), names.index("space"))          # фокус проверен до набора

    def test_no_delete_if_space_was_not_typed(self):
        self.mac.fail_on = {"space": "not allowed to send keystrokes (1002)"}
        code, text, _ = self.run_check(User(confirms=["д"]), "--only", "Г")
        self.assertNotIn("delete", self.mac.names())
        self.assertNotIn("paste", self.mac.names())
        self.assertIn("Универсальный доступ", text)
        self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_explicit_only_runs_risky_even_after_safe(self):
        self.run_check(User(verdicts=["д", "д", "н"], confirms=["д", "д"]), "--only", "З1,Г,В")
        self.assertEqual(self.mac.names().count("space"), 1)
        self.assertEqual(self.mac.names().count("click"), 1)


class TestClickVariant(CheckCase):
    def test_one_click_at_computed_point(self):
        self.mac.geometry = "100 50 1200 800"
        code, text, _ = self.run_check(User(verdicts=["д"], confirms=["д"]), "--only", "В")
        clicks = [e for e in self.mac.events if e[0] == "click"]
        self.assertEqual(clicks, [("click", "844", "812")])                   # 100 + 0.62·1200, 50 + 800 − 38
        self.assertIn("x = левый край + 0.62 · ширина = 844", text)
        self.assertIn("y = нижний край − 38 = 812", text)
        names = self.mac.names()
        self.assertLess(names.index("geometry"), names.index("click"))
        self.assertLess(names.index("click"), names.index("paste"))

    def test_narrow_window_skips_with_reason(self):
        self.mac.geometry = "0 0 700 900"
        code, text, _ = self.run_check(User(confirms=["д"]), "--only", "В")
        self.assertNotIn("click", self.mac.names())
        self.assertIn("пропущен: окно уже 700 пикселей (ширина 700)", text)
        self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_just_over_the_limit_clicks(self):
        self.mac.geometry = "0 0 701 900"
        self.run_check(User(verdicts=["н"], confirms=["д"]), "--only", "В")
        self.assertEqual(self.mac.names().count("click"), 1)

    def test_window_not_found(self):
        for value in ("no-window", "no-process", "ерунда"):
            with self.subTest(value=value):
                self.mac = FakeMac()
                self.mac.geometry = value
                code, text, _ = self.run_check(User(confirms=["д"]), "--only", "В")
                self.assertNotIn("click", self.mac.names())
                self.assertIn("пропущен", text)

    def test_declined_click_is_not_made(self):
        self.run_check(User(confirms=["н"]), "--only", "В")
        self.assertNotIn("click", self.mac.names())
        self.assertNotIn("geometry", self.mac.names())


class TestReopenVariant(CheckCase):
    def test_second_open_then_wait_then_paste(self):
        self.run_check(User(verdicts=["н"]), "--only", "Д")
        names = self.mac.names()
        start = names.index("wait")                                   # шаг 3 диагностики (первое открытие уже было)
        sequence = names[names.index("clipboard_info"):]
        self.assertEqual(sequence[:9], ["clipboard_info", "pbpaste", "open", "wait", "open", "wait", "pbcopy", "front",
                                        "paste"])
        sleeps = [e[1] for e in self.mac.events if e[0] == "sleep"]
        self.assertIn(check.REOPEN_WAIT, sleeps)
        events = self.mac.events
        second_open = [i for i, e in enumerate(events) if e[0] == "open"][-1]
        self.assertEqual(events[second_open + 1], ("sleep", 1.0))
        self.assertEqual(events[events.index(("paste",)) + 1], ("sleep", 1.0))


class TestClipboardCheck(CheckCase):
    def test_match_prints_lengths_not_text(self):
        code, text, user = self.run_check(User(), "--only", "Е")
        self.assertIn("AppleScript: set the clipboard to (item 1 of argv)", text)
        self.assertIn("совпадает: в буфере 26 симв., нужно 26", text)
        self.assertEqual(text.count("совпадает: в буфере 26 симв., нужно 26"), 2)      # AppleScript и pbcopy
        section = text[text.index("Вариант Е"):text.index("РЕЗУЛЬТАТ")]
        self.assertNotIn(check.TEST_TEXT, section)                     # в шагах Е текста нет, только длины
        self.assertEqual(user.questions, [])
        self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_applescript_mismatch(self):
        self.mac.clip_as_ok = False
        code, text, _ = self.run_check(User(), "--only", "Е")
        self.assertIn("не совпадает", text)
        self.assertIn("AppleScript — не совпадает", text)
        self.assertIn("даже через AppleScript текст не совпадает", text)

    def test_pbcopy_mismatch_recommends_applescript(self):
        self.mac.pbpaste_mangle = True
        code, text, _ = self.run_check(User(), "--only", "Е")
        self.assertIn("через pbcopy текст не совпадает, через AppleScript совпадает", text)
        self.assertIn("TELEGRAM_CLIPBOARD_METHOD=applescript", text)
        self.assertIn("отдельным шагом", text)

    def test_non_text_clipboard_stops_clipboard_check(self):
        self.mac.clipboard_info = "«class PNGf», 1000"
        code, text, _ = self.run_check(User(), "--only", "Е")
        self.assertIn("В буфере обмена не текст", text)
        self.assertEqual(self.mac.names().count("clip_as"), 0)


class TestAsciiAndMenuAndKeycode(CheckCase):
    def test_ascii_text_is_pasted_in_variant_zh(self):
        self.run_check(User(verdicts=["н"]), "--only", "Ж")
        self.assertIn(("pbcopy", "jarvis test"), self.mac.events)
        self.assertNotIn(("pbcopy", check.TEST_TEXT), self.mac.events)

    def test_keycode_variant_uses_key_code_9_not_keystroke(self):
        self.run_check(User(verdicts=["д"]), "--only", "З1")
        self.assertEqual(self.pastes(), ["paste_keycode"])

    def test_menu_is_read_before_click_and_localised_names_found(self):
        self.run_check(User(verdicts=["д"]), "--only", "З2")
        names = self.mac.names()
        self.assertEqual(names[names.index("menu_bar"):names.index("menu_bar") + 4],
                         ["menu_bar", "menu_items", "menu_enabled", "menu_click"])        # сначала чтение, потом клик
        self.assertIn(("menu_click", "Правка", "Вставить"), self.mac.events)             # точный пункт, не «…стиль»

    def test_english_menu(self):
        self.mac.menu_bar, self.mac.menu_items = "Apple, Telegram, File, Edit", "Undo, Redo, Cut, Copy, Paste, Select All"
        self.run_check(User(verdicts=["д"]), "--only", "З2")
        self.assertIn(("menu_click", "Edit", "Paste"), self.mac.events)

    def test_menu_without_paste_or_edit_is_skipped(self):
        self.mac.menu_items = "Отменить, Копировать"
        code, text, _ = self.run_check(User(), "--only", "З2")
        self.assertNotIn("menu_click", self.mac.names())
        self.assertIn("пункт меню не найден", text)
        self.assertIn("нет пункта Вставить/Paste", text)
        self.mac = FakeMac()
        self.mac.menu_bar = "Apple, Telegram, Файл"
        code, text, _ = self.run_check(User(), "--only", "З2")
        self.assertNotIn("menu_click", self.mac.names())
        self.assertIn("пункт меню не найден", text)
        self.assertIn("нет Правка/Изменить/Edit", text)
        self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_menu_names_are_printed(self):
        code, text, _ = self.run_check(User(verdicts=["н"]), "--only", "З2")
        self.assertIn("пункты «Правка»: Отменить, Вставить, Вставить и согласовать стиль", text)
        self.assertIn("«Вставить»: доступен", text)


class TestManualCheck(CheckCase):
    def test_manual_only_when_nothing_worked(self):
        user = User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"], manual=["д"])
        code, text, _ = self.run_check(user)
        self.assertIn("Кликните в поле ввода и нажмите Cmd+V руками. Появился ли текст? (д/н/п):",
                      "".join(user.questions))
        self.assertIn("Руками текст вставляется", text)
        self.assertIn(("clip_as", check.TEST_TEXT), self.mac.events)                # буфер с тестом, способом из Е
        self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_manual_uses_pbcopy_when_applescript_mismatches(self):
        self.mac.clip_as_ok = False
        self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"], manual=["н"]))
        self.assertIn(("pbcopy", check.TEST_TEXT), self.mac.events)

    def test_manual_no_means_problem_is_in_field_or_buffer(self):
        code, text, _ = self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"], manual=["н"]))
        self.assertIn("Руками Cmd+V тоже не вставляет", text)

    def test_no_manual_when_something_worked(self):
        user = User(verdicts=["н", "д", "н", "н", "н", "н"])
        code, text, _ = self.run_check(user)
        self.assertNotIn("Вариант И", text)
        self.assertFalse(any("руками" in q for q in user.questions))


class TestConclusions(CheckCase):
    def test_keycode_works_keystroke_does_not_means_layout_dependence(self):
        code, text, _ = self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        self.assertIn("TELEGRAM_PASTE_METHOD=keycode", text)
        self.assertIn("вероятна зависимость от раскладки", text)
        self.assertIn("будут подключены отдельным шагом, боевой код их пока не читает", text)

    def test_plain_keystroke_works(self):
        code, text, _ = self.run_check(User(verdicts=["н", "н", "д", "н", "н", "н"]))
        self.assertIn("TELEGRAM_PASTE_METHOD=keystroke", text)
        self.assertIn("Штатная вставка сработала (вариант Б)", text)

    def test_menu_only(self):
        code, text, _ = self.run_check(User(verdicts=["н", "н", "н", "н", "д", "н"]))
        self.assertIn("TELEGRAM_PASTE_METHOD=menu", text)

    def test_latin_works_cyrillic_does_not(self):
        code, text, _ = self.run_check(User(verdicts=["н", "н", "н", "д", "н", "н"]))
        self.assertIn("Латиница вставилась, кириллица нет", text)

    def test_focus_mode_preference_type_then_reopen_then_click(self):
        # порядок запуска: Д, Г, В; предпочтение при выводе: Г, затем Д, затем В
        for answers, expected in ((["д", "д", "д"], "type"), (["д", "н", "д"], "reopen"), (["н", "н", "д"], "click")):
            with self.subTest(expected=expected):
                self.mac = FakeMac()
                code, text, _ = self.run_check(User(verdicts=answers, confirms=["д", "д"]), "--only", "Г,Д,В")
                self.assertIn(f"    TELEGRAM_FOCUS_MODE={expected}", text)               # строка в списке для .env
                self.assertEqual(text.count("    TELEGRAM_FOCUS_MODE="), 1)

    def test_nothing_worked_gives_request_and_three_checks(self):
        code, text, _ = self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"], manual=["н"]))
        self.assertIn("Опишите, пожалуйста, что видно в окне Telegram", text)
        self.assertIn("поле ввода сообщения", text)
        self.assertIn("открыт чат или профиль", text)
        self.assertIn("курсор", text)
        for number in ("1)", "2)", "3)"):
            self.assertIn(number, text)
        self.assertNotIn("    TELEGRAM_FOCUS_MODE=", text)

    def test_errors_do_not_count_as_no(self):
        self.mac.fail_on = {"paste": "not allowed to send keystrokes (1002)"}
        code, text, user = self.run_check(User(), "--only", "А,Б")
        self.assertEqual(sum("Появился ли текст" in q for q in user.questions), 0)
        self.assertNotIn("Опишите, пожалуйста", text)                        # ошибка прав — не «ничего не сработало»


class TestLastRunFile(CheckCase):
    def test_summary_saved_private_without_secrets(self):
        self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        self.assertEqual(stat.S_IMODE(self.last.stat().st_mode), 0o600)
        data = json.loads(self.last.read_text(encoding="utf-8"))
        self.assertEqual(data["layout_kind"], "en")
        self.assertEqual(data["results"]["З1"], True)
        self.assertEqual(data["results"]["Г"], None)
        raw = self.last.read_text(encoding="utf-8")
        for secret in (check.TEST_TEXT, check.ASCII_TEXT, "my_self_user", "mama_user", "999"):
            self.assertNotIn(secret, raw)

    def test_first_run_has_nothing_to_compare(self):
        code, text, _ = self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        self.assertIn("сравнить не с чем", text)
        self.assertIn("Для сравнения запустите диагностику дважды", text)

    def test_layout_influence_detected_between_two_runs(self):
        self.mac.layout = 'KeyboardLayout Name = "Russian";'
        self.run_check(User(verdicts=["н", "н", "н", "н", "н", "н"], confirms=["д", "д"], manual=["п"]))
        self.mac = FakeMac()
        self.mac.layout = 'KeyboardLayout Name = "ABC";'
        code, text, _ = self.run_check(User(verdicts=["н", "д", "д", "д", "н", "н"]))
        self.assertIn("Раскладка ВЛИЯЕТ", text)
        self.assertIn("А: русской — н, английской — д", text)

    def test_layout_does_not_influence(self):
        self.mac.layout = 'KeyboardLayout Name = "Russian";'
        self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        self.mac = FakeMac()
        code, text, _ = self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        self.assertIn("Раскладка не влияет: результаты при русской и английской одинаковы", text)

    def test_same_layout_asks_to_switch(self):
        self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        code, text, _ = self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        self.assertIn("та же раскладка, для сравнения переключите раскладку", text)

    def test_only_last_run_is_kept(self):
        self.run_check(User(verdicts=["д", "н", "н", "н", "н", "н"]))
        self.mac = FakeMac()
        self.mac.layout = 'KeyboardLayout Name = "Russian";'
        self.run_check(User(verdicts=["н", "н", "н", "н", "н", "н"], confirms=["д", "д"], manual=["п"]))
        self.assertEqual(json.loads(self.last.read_text(encoding="utf-8"))["layout_kind"], "ru")

    def test_compare_function_directly(self):
        previous = {"layout_kind": "ru", "results": {"А": False, "Б": False}}
        self.assertIn("ВЛИЯЕТ", check.compare_with_last(previous, "en", {"А": True, "Б": False}))
        self.assertIn("не влияет", check.compare_with_last(previous, "en", {"А": False, "Б": False}))
        self.assertIn("сравнить не с чем", check.compare_with_last(None, "en", {}))
        self.assertIn("не определена", check.compare_with_last(previous, "?", {}))


class TestArguments(CheckCase):
    def test_to_contact_other_than_self(self):
        book = json.loads(self.contacts_file.read_text(encoding="utf-8"))
        book["тест"] = {"username": "test_bot_user", "title": "Тестовый бот"}
        self.contacts_file.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        code, text, _ = self.run_check(User(verdicts=["н"]), "--to", "тест", "--only", "А")
        self.assertIn(("open", "tg://resolve?domain=test_bot_user"), self.mac.events)
        self.assertNotIn("my_self_user", str(self.mac.events))
        self.assertIn("В чат «тест» вставляется только фиксированный тест", text)
        self.assertNotIn("test_bot_user", text)

    def test_unknown_contact_stops(self):
        code, text, _ = self.run_check(User(), "--to", "незнакомец")
        self.assertEqual(code, 1)
        self.assertIn("Не знаю такого контакта", text)
        self.assertNotIn("paste", self.mac.names())

    def test_default_is_self(self):
        self.run_check(User(verdicts=["н"]), "--only", "А")
        self.assertIn(("open", "tg://resolve?domain=my_self_user"), self.mac.events)

    def test_only_parsing(self):
        self.assertEqual(check.parse_only("Е,З1"), {"Е", "З1"})
        self.assertEqual(check.parse_only("з"), {"З1", "З2"})
        self.assertEqual(check.parse_only("г в"), {"Г", "В"})
        self.assertIsNone(check.parse_only(None))
        with self.assertRaises(SystemExit):
            check.parse_only("Я")

    def test_only_runs_only_the_named_variants(self):
        self.run_check(User(verdicts=["н"]), "--only", "Ж")
        self.assertEqual(self.pastes(), ["paste"])
        self.assertNotIn("clip_as", self.mac.names())

    def test_skip_answer_skips_variant(self):
        self.run_check(User(skip_erase={1}), "--only", "А")
        self.assertEqual(self.pastes(), [])

    def test_skip_verdict_is_recorded_as_skipped(self):
        code, text, _ = self.run_check(User(verdicts=["п"]), "--only", "А")
        self.assertIn("А буфер сразу: пропущен", text)


class TestPermissionsAndSetup(CheckCase):
    def test_no_automation_permission(self):
        self.mac.fail_on = {"front": "Not authorized to send Apple events to System Events. (-1743)"}
        code, text, user = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Автоматизация, System Events", text)
        self.assertEqual(user.questions, [])
        self.assertNotIn("paste", self.mac.names())

    def test_no_accessibility_permission(self):
        self.mac.fail_on = {"probe_access": "osascript is not allowed assistive access. (-25211)"}
        code, text, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Универсальный доступ", text)

    def test_no_self_contact(self):
        book = {k: v for k, v in json.loads(self.contacts_file.read_text(encoding="utf-8")).items() if k != "себе"}
        self.contacts_file.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        code, text, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Добавьте «себе» в contacts.json", text)

    def test_telegram_does_not_come_forward(self):
        self.mac.window = "timeout"
        code, text, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("[ошибка] Telegram впереди, окно есть", text)

    def test_focus_lost_before_paste(self):
        self.mac.front = "com.apple.Safari"
        code, text, _ = self.run_check(User(verdicts=[]), "--only", "З1")
        self.assertNotIn("paste_keycode", self.mac.names())
        self.assertIn("Telegram не в фокусе", text)
        self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_no_secret_values_in_output(self):
        code, text, _ = self.run_check(User(verdicts=SAFE_ALL_NO + ["н", "н"], confirms=["д", "д"], manual=["н"]))
        for secret in ("my_self_user", "mama_user", "anya_user", "999"):
            self.assertNotIn(secret, text)

    def test_reminder_at_the_end(self):
        code, text, _ = self.run_check(User(verdicts=["н"]), "--only", "А")
        self.assertIn("сотрите его вручную", text.splitlines()[-1])
        self.assertIn("не отправлен", text)


class TestScripts(unittest.TestCase):
    def test_no_script_presses_enter_or_sends(self):
        for script in check.ALL_SCRIPTS:
            text = " ".join(script)
            self.assertNotIn("key code 36", text)
            self.assertNotIn("return key", text)
        source = open(check.__file__, encoding="utf-8").read()
        for forbidden in ("_ENTER", "key code 36", "_send(", "tg._take_state"):
            self.assertNotIn(forbidden, source)

    def test_risky_scripts_are_exactly_what_was_agreed(self):
        self.assertIn('keystroke " "', " ".join(check._TYPE_SPACE))
        self.assertEqual(" ".join(check._DELETE).count("key code 51"), 1)
        self.assertEqual(" ".join(check._CLICK).count("click at"), 1)
        self.assertIn("key code 9 using command down", " ".join(check._PASTE_KEYCODE))
        self.assertIn("set the clipboard to (item 1 of argv)", " ".join(check._CLIPBOARD_AS))
        self.assertIn("the clipboard as text", " ".join(check._CLIPBOARD_AS))
        self.assertEqual(check.CLICK_X_FRACTION, 0.62)
        self.assertEqual(check.CLICK_BOTTOM_OFFSET, 38)
        self.assertEqual(check.CLICK_MIN_WIDTH, 700)

    def test_texts_are_passed_by_argv_not_inserted_into_scripts(self):
        for script in check.ALL_SCRIPTS:
            text = " ".join(script)
            self.assertNotIn(check.TEST_TEXT, text)
            self.assertNotIn(check.ASCII_TEXT, text)

    def test_combined_order_constants(self):
        self.assertEqual(check.ORDER, ("Е", "З1", "А", "Б", "Ж", "З2", "Д", "Г", "В", "И"))
        self.assertEqual(check.SAFE_VARIANTS, ("З1", "А", "Б", "Ж", "З2", "Д"))
        self.assertEqual(check.RISKY_VARIANTS, ("Г", "В"))


class TestProductionUntouched(unittest.TestCase):
    def test_production_telegram_code_does_not_use_diagnostic_modes(self):
        source = open(tg.__file__, encoding="utf-8").read()
        # режимы reopen и direct боевые; type, click, key code 9, меню и клик из диагностики в боевой код не подключены
        self.assertEqual(tg.FOCUS_MODES, ("reopen", "direct"))
        self.assertEqual(tg.PASTE_METHODS, ("keystroke", "keycode", "menu"))
        for name in ("TELEGRAM_CLIPBOARD_METHOD", "click at", "key code 51", "key code 53",
                     "import core.telegram_check", "from core import telegram_check"):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
