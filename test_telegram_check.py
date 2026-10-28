"""python -m core.telegram_check: диагностика вставки черновика. Тест на моках: Telegram не запускается, клавиши не
нажимаются, скрипт спрашивает ответы у «пользователя» (подменённый input)."""
import io
import os
import subprocess
import sys
import unittest

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

import json
from unittest.mock import patch

from core import telegram_check as check
from core.apps import telegram as tg
from test_telegram import CLIENT, FakeMac, TelegramCase


class Answers:
    """input(): ответы по очереди; запоминает вопросы."""

    def __init__(self, *answers):
        self.answers, self.questions = list(answers), []

    def __call__(self, prompt=""):
        self.questions.append(prompt)
        return self.answers.pop(0) if self.answers else ""


class CheckCase(TelegramCase):
    def run_check(self, *answers):
        out, ask = io.StringIO(), Answers(*answers)
        code = check.main(input_fn=ask, out=out)
        return code, out.getvalue(), ask


class TestHappyPath(CheckCase):
    def test_runs_two_variants_and_concludes(self):
        code, text, ask = self.run_check("", "д", "", "н")
        self.assertEqual(code, 0)
        # разрешения → чат → ожидание → два варианта
        self.assertEqual(self.mac.names()[:4], ["front", "probe_access", "open", "wait"])   # права → чат → ожидание
        for expected in ("Автоматизация → System Events", "Универсальный доступ", "контакт «себе»", "Telegram впереди",
                         "Вариант А", "Вариант Б", "РЕЗУЛЬТАТ", "время ожидания"):
            self.assertIn(expected, text)
        self.assertEqual(text.count("[ok]") >= 8, True)
        self.assertNotIn("[ошибка]", text)
        self.assertEqual(sum("Появился ли текст" in q for q in ask.questions), 2)     # про каждый вариант
        self.assertEqual(sum("сотрите его вручную" in q for q in ask.questions), 2)    # стирает пользователь
        self.assertIn("сотрите его вручную", text.splitlines()[-1])                    # напоминание в конце
        self.assertIn("не отправлен", text)

    def test_enter_is_never_pressed_and_only_the_fixed_text_is_pasted(self):
        self.run_check("", "д", "", "д")
        self.assertNotIn("enter", self.mac.names())
        self.assertEqual(self.mac.names().count("paste"), 2)
        texts = [e[1] for e in self.mac.events if e[0] == "pbcopy"]
        self.assertEqual(texts.count("проверка Jarvis (черновик)"), 2)
        self.assertEqual(set(texts), {"проверка Jarvis (черновик)", "старый буфер"})      # остальное — возврат буфера
        opened = {e[1] for e in self.mac.events if e[0] == "open"}
        self.assertEqual(opened, {"tg://resolve?domain=my_self_user"})                  # только чат «себе»

    def test_variant_timings_a_immediately_b_one_second(self):
        self.run_check("", "д", "", "д")
        sleeps = [e[1] for e in self.mac.events if e[0] == "sleep"]
        waits = [s for s in sleeps if s != tg.PASTE_DELAY]
        self.assertEqual(waits, [0.0, 1.0])
        # возврат буфера — после соответствующей паузы
        names = [e[0] for e in self.mac.events]
        paste_positions = [i for i, n in enumerate(names) if n == "paste"]
        restore_positions = [i for i, e in enumerate(self.mac.events) if e == ("pbcopy", "старый буфер")]
        self.assertEqual(len(restore_positions), 2)
        self.assertEqual(self.mac.events[paste_positions[1] + 1], ("sleep", 1.0))
        self.assertGreater(restore_positions[1], paste_positions[1] + 1)
        self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_conclusions(self):
        cases = {("н", "д"): ("Гонка", "TELEGRAM_PASTE_WAIT=1.0"), ("д", "д"): ("оба варианта", "TELEGRAM_PASTE_WAIT=1.0"),
                 ("н", "н"): ("ни один вариант", None), ("д", "н"): ("Нестабильно", "TELEGRAM_PASTE_WAIT=1.0")}
        for (a, b), (phrase, env) in cases.items():
            with self.subTest(a=a, b=b):
                self.mac = FakeMac()
                code, text, _ = self.run_check("", a, "", b)
                self.assertEqual(code, 0)
                self.assertIn(phrase, text)
                if env:
                    self.assertIn(f"Строка для .env: {env}", text)
                else:
                    self.assertNotIn("Строка для .env", text)
                self.assertIn("Вариант А (сразу): " + ("текст появился" if a == "д" else "текста нет"), text)
                self.assertIn("Вариант Б (через 1,0 с): " + ("текст появился" if b == "д" else "текста нет"), text)

    def test_conclude_function(self):
        self.assertEqual(check.conclude(False, True)[1], "TELEGRAM_PASTE_WAIT=1.0")
        self.assertIsNone(check.conclude(False, False)[1])

    def test_answers_parsed_strictly_and_asked_again(self):
        code, text, ask = self.run_check("", "может быть", "д", "", "нет")
        self.assertIn("Ответьте «д» или «н».", text)
        self.assertIn("Вариант А (сразу): текст появился", text)
        self.assertIn("Вариант Б (через 1,0 с): текста нет", text)

    def test_clipboard_text_restored_and_nothing_else_touched(self):
        self.mac.pasteboard = "важный буфер"
        self.run_check("", "д", "", "д")
        self.assertEqual(self.mac.pasteboard, "важный буфер")

    def test_no_secret_values_in_output(self):
        code, text, _ = self.run_check("", "д", "", "д")
        for secret in ("my_self_user", "mama_user", "anya_user", "999"):
            self.assertNotIn(secret, text)


class TestFailures(CheckCase):
    def test_no_automation_permission(self):
        self.mac.fail_on = {"front": "Not authorized to send Apple events to System Events. (-1743)"}
        code, text, ask = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("[ошибка] Автоматизация → System Events — Нужно разрешение", text)
        self.assertIn("Автоматизация, System Events", text)
        self.assertEqual(ask.questions, [])
        self.assertNotIn("paste", self.mac.names())
        self.assertNotIn("open", self.mac.names())

    def test_no_accessibility_permission(self):
        self.mac.fail_on = {"probe_access": "osascript is not allowed assistive access. (-25211)"}
        code, text, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Универсальный доступ", text)
        self.assertIn("[ошибка] Универсальный доступ", text)
        self.assertNotIn("paste", self.mac.names())

    def test_no_self_contact(self):
        book = {k: v for k, v in json.loads(self.contacts_file.read_text(encoding="utf-8")).items() if k != "себе"}
        self.contacts_file.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        code, text, ask = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Добавьте «себе» в contacts.json", text)
        self.assertNotIn("open", self.mac.names())

    def test_telegram_does_not_come_forward(self):
        self.mac.window = "timeout"
        code, text, ask = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("[ошибка] Telegram впереди, окно есть", text)
        self.assertNotIn("paste", self.mac.names())

    def test_focus_lost_before_paste_prints_error_and_restores_buffer(self):
        self.mac.front = "com.apple.Safari"       # Telegram «впереди» по ожиданию окна, но по проверке — нет
        code, text, ask = self.run_check("", "", "")
        self.assertEqual(code, 1)
        self.assertNotIn("paste", self.mac.names())          # клавиши не нажаты
        self.assertEqual(self.mac.pasteboard, "старый буфер")
        self.assertIn("Telegram не в фокусе", text)

    def test_non_text_clipboard_stops_variant(self):
        self.mac.clipboard_info = "«class PNGf», 1000"
        code, text, _ = self.run_check("", "")
        self.assertEqual(code, 1)
        self.assertIn("В буфере обмена не текст", text)
        self.assertNotIn("paste", self.mac.names())


class TestScripts(unittest.TestCase):
    def test_probe_scripts_compile(self):
        """Скрипты проверки разрешений только читают: настоящий запуск под tests_base запрещён, поэтому здесь
        проверяется форма скриптов (компиляцию проверяют вручную при разработке)."""
        for script in (check._PROBE_AUTOMATION, check._PROBE_ACCESSIBILITY):
            text = " ".join(script)
            self.assertIn("System Events", text)
            self.assertNotIn("keystroke", text)
            self.assertNotIn("key code", text)

    def test_module_never_references_enter(self):
        source = open(check.__file__, encoding="utf-8").read()
        for forbidden in ("_ENTER", "key code 36", "_send(", "key code"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
