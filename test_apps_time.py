"""Время и личные данные (Этап 6.2): timer, alarm, reminders, calendar, notes. subprocess замокан."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import safety
from core.apps import calendar as calendar_app
from core.apps import clock, common, notes, registry, reminders
from core.errors import ActionError
from fixtures_run import Clock, completed, isolated_apps, scripted_run

NOW = datetime(2026, 10, 28, 14, 30)


def tool(name):
    return registry.get(name)


def run(name, phrase="", **args):
    return tool(name).run(phrase=phrase, **args)


class AppCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sched, self.shortcuts_file = isolated_apps(self, {"Jarvis Alarm": "SAFE", "Jarvis Timer": "SAFE"},
                                                        self.clock)

    def osascript(self, *outputs):
        mock = scripted_run(*outputs)
        patcher = patch("core.apps.common.subprocess.run", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock


class TestTimer(AppCase):
    def test_start_status_cancel(self):
        done = run("timer", action="start", seconds=300, label="чай")
        self.assertEqual(done.speech, "Таймер «чай» на 5 минут запущен.")
        self.assertEqual(run("timer", action="status", seconds=0, label="").speech, "Осталось 5 минут, таймер «чай».")
        self.clock.t += 90
        self.assertEqual(run("timer", action="status", seconds=0, label="").speech,
                         "Осталось 3 минуты 30 секунд, таймер «чай».")
        self.assertEqual(run("timer", action="cancel", seconds=0, label="").speech, "Таймер отменён: чай.")
        self.assertEqual(run("timer", action="status", seconds=0, label="").speech, "Активных таймеров нет.")

    def test_seconds_validation(self):
        for bad in (0, -5, 86401, "abc", None, True, 3.5 * 0 + 0):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                run("timer", action="start", seconds=bad, label="")
        self.assertEqual(run("timer", action="start", seconds=86400, label="").speech, "Таймер на 24 часа запущен.")
        self.assertEqual(run("timer", action="start", seconds="90", label="").speech, "Таймер на 1 минута 30 секунд запущен.")

    def test_label_checked(self):
        for bad in ("a" * 101, "строка\x00ноль"):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                run("timer", action="start", seconds=10, label=bad)

    def test_several_timers(self):
        run("timer", action="start", seconds=300, label="чай")
        run("timer", action="start", seconds=60, label="")
        text = run("timer", action="list", seconds=0, label="").speech
        self.assertIn("Таймеров 2", text)
        with self.assertRaises(ActionError) as ctx:
            run("timer", action="cancel", seconds=0, label="")
        self.assertIn("назовите название", ctx.exception.short)
        self.assertEqual(run("timer", action="cancel", seconds=0, label="чай").speech, "Таймер отменён: чай.")
        self.assertEqual(len(self.sched.items("timer")), 1)
        with self.assertRaises(ActionError) as ctx:
            run("timer", action="cancel", seconds=0, label="нет такого")
        self.assertEqual(ctx.exception.short, "не нашёл такой таймер")

    def test_no_timers_to_cancel(self):
        with self.assertRaises(ActionError) as ctx:
            run("timer", action="cancel", seconds=0, label="")
        self.assertEqual(ctx.exception.short, "активных таймеров нет")

    def test_clock_app_goes_through_shortcut(self):
        mock = scripted_run("Jarvis Timer\nДругая", completed(""))
        with patch("core.apps.shortcuts.subprocess.run", mock):
            done = run("timer", phrase="поставь таймер в приложении Часы на 5 минут", action="start", seconds=300, label="")
        self.assertIn("в приложении Часы", done.speech)
        self.assertEqual(self.sched.items(), [])   # внутренний не заводится
        run_args = mock.call_args.args[0]
        self.assertEqual(run_args[:3], ["shortcuts", "run", "Jarvis Timer"])

    def test_clock_app_without_shortcut_is_honest(self):
        self.shortcuts_file.write_text("{}", encoding="utf-8")
        with self.assertRaises(ActionError) as ctx:
            run("timer", phrase="таймер в часах на минуту", action="start", seconds=60, label="")
        reply = ctx.exception.reply
        self.assertIn("В приложении Часы это делается через Быстрые команды", reply)
        self.assertIn("Создайте команду Jarvis Timer, инструкция в файле docs/shortcuts.md", reply)
        self.assertIn("внутренний таймер", reply)
        self.assertEqual(self.sched.items(), [])

    def test_clock_app_shortcut_missing_in_app(self):
        with patch("core.apps.shortcuts.subprocess.run", scripted_run("Lock Launcher\nЗвонок")):
            with self.assertRaises(ActionError) as ctx:
                run("timer", phrase="в приложении часы таймер", action="start", seconds=60, label="")
        self.assertIn("Jarvis Timer", ctx.exception.reply)


class TestAlarm(AppCase):
    def setUp(self):
        super().setUp()
        self.now = datetime.now().replace(second=0, microsecond=0)
        self.clock.t = datetime.now().timestamp()

    def when(self, **delta):
        return (self.now + timedelta(**delta)).strftime("%Y-%m-%d %H:%M")

    def test_set_list_cancel(self):
        done = run("alarm", action="set", when=self.when(hours=3), label="подъём")
        self.assertIn("Он сработает, только если Jarvis запущен.", done.speech)
        self.assertEqual(len(self.sched.items("alarm")), 1)
        self.assertIn("Будильников 1", run("alarm", action="list", when="", label="").speech)
        self.assertEqual(run("alarm", action="cancel", when="", label="").speech, "Будильник отменён.")
        self.assertEqual(run("alarm", action="list", when="", label="").speech, "Активных будильников нет.")

    def test_when_validation(self):
        for bad, expected in [("", "формате"), ("завтра в 7", "формате"), ("2026-13-45 99:99", "не существует"),
                              ("2026-02-30 10:00", "не существует"), (self.when(hours=-2), "прошло"),
                              (self.when(days=366), "на год")]:
            with self.subTest(bad=bad), self.assertRaises(ActionError) as ctx:
                run("alarm", action="set", when=bad, label="")
            self.assertIn(expected, ctx.exception.short)
        self.assertEqual(self.sched.items(), [])

    def test_boundaries_ok(self):
        run("alarm", action="set", when=self.when(days=364), label="")
        run("alarm", action="set", when=self.when(minutes=5), label="")

    def test_wake_me_creates_reminder_in_reminders_app(self):
        mock = self.osascript("")
        when = self.when(days=1)
        done = run("alarm", phrase="разбуди меня завтра", action="set", when=when, label="Подъём")
        self.assertIn("Создал напоминание на", done.speech)
        self.assertIn("оно сработает и без меня", done.speech)
        self.assertEqual(self.sched.items(), [])      # внутренний не заводится
        args = mock.call_args.args[0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("Подъём", args[-8:])                    # название — аргументом argv
        self.assertNotIn("Подъём", " ".join(args[:-8]))       # и не в тексте скрипта

    def test_wake_me_spoken_exactly(self):
        self.osascript("")
        when = (self.now.replace(hour=7, minute=0) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        done = run("alarm", phrase="разбуди меня в 7", action="set", when=when, label="")
        self.assertEqual(done.speech, "Создал напоминание на 7:00, оно сработает и без меня.")

    def test_clock_app_alarm_via_shortcut(self):
        mock = scripted_run("Jarvis Alarm", completed(""))
        with patch("core.apps.shortcuts.subprocess.run", mock):
            done = run("alarm", phrase="поставь будильник в приложении Часы", action="set",
                       when=self.when(hours=5), label="")
        self.assertIn("в приложении Часы", done.speech)
        self.assertEqual(mock.call_args.args[0][:3], ["shortcuts", "run", "Jarvis Alarm"])
        self.assertEqual(self.sched.items(), [])

    def test_clock_app_alarm_over_24_hours_refused(self):
        with self.assertRaises(ActionError) as ctx:
            run("alarm", phrase="будильник в часах", action="set", when=self.when(days=3), label="")
        self.assertIn("ближайшие сутки", ctx.exception.short)

    def test_clock_app_alarm_without_shortcut(self):
        self.shortcuts_file.write_text('{"Jarvis Timer": "SAFE"}', encoding="utf-8")
        with self.assertRaises(ActionError) as ctx:
            run("alarm", phrase="будильник в приложении Часы", action="set", when=self.when(hours=2), label="")
        self.assertIn("Создайте команду Jarvis Alarm, инструкция в файле docs/shortcuts.md", ctx.exception.reply)
        self.assertIn("внутренний будильник", ctx.exception.reply)


class TestReminders(AppCase):
    def test_add_with_date_passes_argv_only(self):
        mock = self.osascript("")
        when = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=5, second=0, microsecond=0)
        done = run("reminders", action="add", title="Позвонить маме \"срочно\"; do shell script", when=when.strftime(
            "%Y-%m-%d %H:%M"), list_name="Дом")
        self.assertIn("создано на завтра в 9:05", done.speech)
        args = mock.call_args.args[0]
        self.assertEqual(args[-8:], ["Позвонить маме \"срочно\"; do shell script", "1", str(when.year), str(when.month),
                                      str(when.day), "9", "5", "Дом"])
        script = " ".join(a for a in args[:-8] if a != "-e")
        self.assertNotIn("срочно", script)
        self.assertNotIn("Дом", script)

    def test_add_without_date_and_default_list(self):
        mock = self.osascript("")
        run("reminders", action="add", title="Молоко", when="", list_name="")
        self.assertEqual(mock.call_args.args[0][-8:], ["Молоко", "0", "0", "0", "0", "0", "0", ""])

    def test_add_validation(self):
        mock = self.osascript("")
        for kwargs in ({"title": "", "when": "", "list_name": ""}, {"title": "x" * 201, "when": "", "list_name": ""},
                       {"title": "ок", "when": "2020-01-01 10:00", "list_name": ""},
                       {"title": "ок", "when": "вчера", "list_name": ""},
                       {"title": "ок\x07", "when": "", "list_name": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ActionError):
                run("reminders", action="add", **kwargs)
        mock.assert_not_called()

    def test_list_up_to_10(self):
        lines = "\n".join(f"Дело {i}\t2026-10-29-9-0" for i in range(12)) + "\nБез даты\t"
        mock = self.osascript(lines)
        done = run("reminders", action="list", title="", when="", list_name="")
        self.assertEqual(mock.call_args.args[0][-1], "10")   # лимит передан скрипту
        self.assertTrue(done.web)                              # чужой текст
        self.assertIn("Дело 0", done.speech)
        self.assertIn("Невыполненных напоминаний 13", done.speech)   # скрипт вернул, сколько вернул

    def test_list_empty(self):
        self.osascript("")
        self.assertEqual(run("reminders", action="list", title="", when="", list_name="").speech,
                         "Невыполненных напоминаний нет.")

    def test_complete_and_delete_unique(self):
        mock = self.osascript("1")
        self.assertEqual(run("reminders", action="complete", title="Молоко", when="", list_name="").speech,
                         "Напоминание «Молоко» выполнено.")
        self.assertEqual(mock.call_args.args[0][-2:], ["Молоко", "complete"])
        self.assertEqual(run("reminders", action="delete", title="Молоко", when="", list_name="").speech,
                         "Напоминание «Молоко» удалено.")
        self.assertEqual(mock.call_args.args[0][-2:], ["Молоко", "delete"])

    def test_not_found_and_ambiguous(self):
        self.osascript("0")
        with self.assertRaises(ActionError) as ctx:
            run("reminders", action="complete", title="Нет такого", when="", list_name="")
        self.assertEqual(ctx.exception.short, "не нашёл")
        self.osascript("2")
        with self.assertRaises(ActionError) as ctx:
            run("reminders", action="delete", title="Дубль", when="", list_name="")
        self.assertIn("несколько", ctx.exception.short)

    def test_delete_is_confirm_and_preview_names_target(self):
        self.assertEqual(safety.classify({"name": "reminders", "args": {"action": "delete"}})[0], safety.CONFIRM)
        self.assertEqual(safety.classify({"name": "reminders", "args": {"action": "complete"}})[0], safety.SAFE)
        mock = self.osascript("1")
        preview = tool("reminders").preview(action="delete", title="Молоко", when="", list_name="")
        self.assertEqual(preview, "удалить напоминание «Молоко»")
        self.assertEqual(mock.call_args.args[0][-1], "check")   # поиск без изменений


class TestCalendar(AppCase):
    def test_add(self):
        mock = self.osascript("")
        start = (datetime.now() + timedelta(days=2)).replace(hour=15, minute=0, second=0, microsecond=0)
        done = run("calendar", action="add", title="Планёрка", start=start.strftime("%Y-%m-%d %H:%M"), minutes=45)
        self.assertIn("Событие «Планёрка» создано", done.speech)
        args = mock.call_args.args[0]
        self.assertEqual(args[-7:], ["Планёрка", str(start.year), str(start.month), str(start.day), "15", "0", "45"])
        script = " ".join(a for a in args if a != "-e")
        self.assertNotIn("attendee", script.lower())      # приглашений других людей нет

    def test_minutes_range(self):
        start = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        mock = self.osascript("")
        for bad in (0, 4, 1441, "abc", None, True):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                run("calendar", action="add", title="Встреча", start=start, minutes=bad)
        mock.assert_not_called()
        run("calendar", action="add", title="Встреча", start=start, minutes=5)
        run("calendar", action="add", title="Встреча", start=start, minutes=1440)

    def test_no_writable_calendar(self):
        self.osascript(completed("", 1, "execution error: нет календаря для записи (9001)"))
        start = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        with self.assertRaises(ActionError) as ctx:
            run("calendar", action="add", title="Встреча", start=start, minutes=30)
        self.assertEqual(ctx.exception.short, "нет календаря, в который можно записывать")

    def test_list_today_sorted_and_limited(self):
        today = datetime.now()
        rows = [f"Обед\t{today.year}-{today.month}-{today.day}-13-0\tfalse",
                f"Планёрка\t{today.year}-{today.month}-{today.day}-10-0\tfalse",
                f"День рождения\t{today.year}-{today.month}-{today.day}-0-0\ttrue"]
        self.osascript("\n".join(rows))
        done = run("calendar", action="list_today", title="", start="", minutes=0)
        self.assertEqual(done.speech, "Сегодня 3 события: День рождения весь день; Планёрка в 10:00; Обед в 13:00.")
        self.assertTrue(done.web)

    def test_list_empty_and_many(self):
        self.osascript("")
        self.assertEqual(run("calendar", action="list_tomorrow", title="", start="", minutes=0).speech,
                         "Завтра событий нет.")
        day = datetime.now()
        rows = [f"Событие {i}\t{day.year}-{day.month}-{day.day}-{i}-0\tfalse" for i in range(15)]
        mock = self.osascript("\n".join(rows))
        done = run("calendar", action="list_week", title="", start="", minutes=0)
        self.assertEqual(done.speech.count("Событие"), 10)    # до 10 событий
        self.assertEqual(mock.call_args.args[0][-1], str(calendar_app.MAX_SCAN))

    def test_list_week_range_in_argv(self):
        mock = self.osascript("")
        run("calendar", action="list_week", title="", start="", minutes=0)
        args = mock.call_args.args[0]
        first = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(args[-11:-6], [str(first.year), str(first.month), str(first.day), "0", "0"])
        end = first + timedelta(days=7)
        self.assertEqual(args[-6:-1], [str(end.year), str(end.month), str(end.day), "0", "0"])

    def test_delete_preview_names_event(self):
        today = datetime.now()
        self.osascript(f"1\t{today.year}-{today.month}-{today.day}-15-0")
        preview = tool("calendar").preview(action="delete", title="Планёрка", start="", minutes=0)
        self.assertEqual(preview, "удалить событие «Планёрка» сегодня в 15:00")

    def test_delete_not_found_and_several(self):
        self.osascript("0\t")
        with self.assertRaises(ActionError) as ctx:
            tool("calendar").preview(action="delete", title="Нет", start="", minutes=0)
        self.assertEqual(ctx.exception.short, "не нашёл")
        self.osascript("3\t2026-10-30-9-0")
        with self.assertRaises(ActionError) as ctx:
            run("calendar", action="delete", title="Планёрка", start="", minutes=0)
        self.assertIn("несколько", ctx.exception.short)

    def test_levels(self):
        for action, level in [("add", "SAFE"), ("list_today", "SAFE"), ("list_tomorrow", "SAFE"),
                              ("list_week", "SAFE"), ("delete", "CONFIRM")]:
            self.assertEqual(safety.classify({"name": "calendar", "args": {"action": action}})[0], level, action)


class TestNotes(AppCase):
    def test_create_escapes_html_and_uses_argv(self):
        mock = self.osascript("")
        done = run("notes", action="create", title="Покупки <b>", text="молоко & хлеб\nсыр")
        self.assertEqual(done.speech, "Заметка «Покупки <b>» создана.")
        body = mock.call_args.args[0][-1]
        self.assertIn("&lt;b&gt;", body)
        self.assertIn("молоко &amp; хлеб", body)
        self.assertNotIn("<b>", body)
        script = " ".join(a for a in mock.call_args.args[0][:-1] if a != "-e")
        self.assertNotIn("молоко", script)

    def test_create_validation(self):
        mock = self.osascript("")
        for kwargs in ({"title": "", "text": "x"}, {"title": "ок", "text": "x" * 2001}, {"title": "a\x00", "text": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ActionError):
                run("notes", action="create", **kwargs)
        mock.assert_not_called()

    def test_append_found_and_not_found(self):
        self.osascript("1")
        self.assertEqual(run("notes", action="append", title="Покупки", text="яйца").speech,
                         "Добавил в заметку «Покупки».")
        self.osascript("0")
        with self.assertRaises(ActionError) as ctx:
            run("notes", action="append", title="Нет такой", text="x")
        self.assertEqual(ctx.exception.short, "не нашёл такую заметку")

    def test_list_titles_newest_first_max_10(self):
        rows = "\n".join(f"{age}\tЗаметка {age}" for age in (500, 20, 9000, 3, 77, 1, 2, 4, 5, 6, 8, 9))
        self.osascript(rows)
        done = run("notes", action="list_titles", title="", text="")
        titles = done.speech.removeprefix("Заметки: ").rstrip(".").split("; ")
        self.assertEqual(titles[:3], ["Заметка 1", "Заметка 2", "Заметка 3"])
        self.assertEqual(len(titles), 10)
        self.assertTrue(done.web)

    def test_read_last_first_300_chars(self):
        self.osascript("Идеи\n" + "слово " * 200)
        done = run("notes", action="read_last", title="", text="")
        content = done.speech.split("»: ", 1)[1]
        self.assertLessEqual(len(content), 301)
        self.assertTrue(done.speech.startswith("Последняя заметка «Идеи»: слово слово"))
        self.assertTrue(done.web)
        self.osascript("")
        with self.assertRaises(ActionError):
            run("notes", action="read_last", title="", text="")

    def test_all_actions_safe_and_external_flags(self):
        for action in ("create", "append", "list_titles", "read_last"):
            self.assertEqual(safety.classify({"name": "notes", "args": {"action": action}})[0], safety.SAFE)
        self.assertEqual(tool("notes").external, frozenset({"list_titles", "read_last"}))


class TestPermissionsWithAppNames(AppCase):
    def test_codes_to_phrases_with_app_name(self):
        cases = [("Reminders", "Not authorized to send Apple events to Reminders. (-1743)", "Автоматизация → Напоминания",
                  "доступа к разделу «Напоминания»"),
                 ("Calendar", "Not authorized to send Apple events to Calendar. (-1743)", "Автоматизация → Календарь",
                  "доступа к разделу «Календари»"),
                 ("Notes", "execution error: (-1743)", "Автоматизация → Заметки", None),
                 ("Shortcuts", "(-1743)", "Автоматизация → Быстрые команды", None),
                 ("System Events", "osascript is not allowed assistive access. (-25211)", "Универсальный доступ", None),
                 ("System Events", "Invalid index. (-1719)", "Универсальный доступ", None),
                 ("System Events", "not allowed to send keystrokes. (1002)", "Универсальный доступ", None)]
        for app, stderr, section, extra in cases:
            with self.subTest(app=app, stderr=stderr):
                error = common.permission_error(app, stderr)
                self.assertIn(section, str(error))
                self.assertIn("для приложения, из которого запущен Jarvis", str(error))
                self.assertNotIn("→", error.reply)
                if extra:
                    self.assertIn(extra.split("«")[1].rstrip("»"), str(error))

    def test_not_found_names_app(self):
        error = common.permission_error("Reminders", "execution error: Can't get list \"X\". (-1728)")
        self.assertIn("в приложении Напоминания", error.reply)

    def test_osascript_raises_it(self):
        self.osascript(completed("", 1, "(-1743)"))
        with self.assertRaises(ActionError) as ctx:
            run("reminders", action="list", title="", when="", list_name="")
        self.assertIn("Напоминания", ctx.exception.reply)

    def test_timeout_names_app(self):
        import subprocess
        self.osascript(subprocess.TimeoutExpired("osascript", 30))
        with self.assertRaises(ActionError) as ctx:
            run("calendar", action="list_today", title="", start="", minutes=0)
        self.assertEqual(ctx.exception.short, "Календарь не ответило вовремя")


if __name__ == "__main__":
    unittest.main()
