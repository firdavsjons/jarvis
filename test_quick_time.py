"""Быстрые правила без модели (Этап 6.2): таймер, «сколько осталось», «отмени таймер», «что у меня сегодня»,
настройки; журнал; кэш (инструменты времени не кэшируются, панели настроек — по шаблону)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import journal, scheduler
from core.ai import AIClient
from core.cache import CommandCache, cacheable_actions
from fixtures_apps import patch_installed
from fixtures_run import Clock, completed, isolated_apps, scripted_run
from modules.mac_control import MacControl
from test_tools import ScriptedLLM, call, final, tools_reply


class QuickCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sched, _ = isolated_apps(self, {}, self.clock)
        installed = patch_installed()
        installed.start()
        self.addCleanup(installed.stop)
        self.mac = MacControl()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"
        old = journal.JOURNAL_FILE
        journal.setup_journal(self.journal_path)
        self.addCleanup(setattr, journal, "JOURNAL_FILE", old)
        # любые внешние команды — мок; настоящих запусков нет
        self.run = scripted_run(completed(""))
        for target in ("core.apps.common.subprocess.run", "core.apps.settings.subprocess.run"):
            patcher = patch(target, self.run)
            patcher.start()
            self.addCleanup(patcher.stop)

    def entries(self):
        if not self.journal_path.exists():
            return []
        return [json.loads(line) for line in self.journal_path.read_text(encoding="utf-8").splitlines()]


class TestTimerPhrases(QuickCase):
    def test_start_phrases(self):
        cases = {"поставь таймер на 5 минут": 300, "поставь таймер на пять минут": 300, "таймер на пять минут": 300,
                 "Поставь, пожалуйста, таймер на полчаса": 1800, "поставь таймер на час": 3600,
                 "поставь таймер на полтора часа": 5400, "поставь таймер на 1 час 30 минут": 5400,
                 "таймер на 90 секунд": 90, "запусти таймер на две минуты": 120, "поставь мне таймер на 10 секунд": 10}
        for phrase, seconds in cases.items():
            with self.subTest(phrase=phrase):
                known = {i["id"] for i in self.sched.items("timer")}
                reply = self.mac.execute(phrase)
                self.assertRegex(reply, r"^Таймер на .+ запущен\.$")
                [new] = [i for i in self.sched.items("timer") if i["id"] not in known]
                self.assertEqual(round(new["due"] - self.clock.t), seconds)
                self.assertEqual(new["label"], "")

    def test_status(self):
        self.mac.execute("поставь таймер на 5 минут")
        self.clock.t += 60
        self.assertEqual(self.mac.execute("сколько осталось"), "Осталось 4 минуты.")
        self.assertEqual(self.mac.execute("Сколько времени осталось?"), "Осталось 4 минуты.")

    def test_status_without_timers(self):
        self.assertEqual(self.mac.execute("сколько осталось"), "Активных таймеров нет.")

    def test_cancel(self):
        self.mac.execute("поставь таймер на 5 минут")
        self.assertEqual(self.mac.execute("отмени таймер"), "Таймер отменён.")
        self.assertEqual(self.sched.items(), [])
        self.assertIn("нет", self.mac.execute("отмени таймер").lower())

    def test_cancel_with_several_timers_goes_to_model(self):
        self.mac.execute("поставь таймер на 5 минут")
        self.mac.execute("поставь таймер на 10 минут")
        self.assertIsNone(self.mac.execute("отмени таймер"))
        self.assertEqual(len(self.sched.items("timer")), 2)

    def test_not_intercepted(self):
        for phrase in ["поставь таймер", "поставь таймер на чай", "таймер на пять минут с названием чай",
                       "поставь таймер на пять минут и открой сафари", "через пять минут напомни позвонить маме",
                       "поставь будильник на семь", "что такое таймер", "таймер на 25 часов",
                       "поставь таймер на ноль минут", "поставь таймер в приложении Часы на 5 минут"]:
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.mac.execute(phrase))
                self.assertEqual(self.sched.items(), [])
        self.assertEqual(self.entries(), [])

    def test_journal_for_quick_timer(self):
        self.mac.execute("поставь таймер на 5 минут")
        self.mac.execute("сколько осталось")
        entries = self.entries()
        self.assertEqual([e["action"]["name"] for e in entries], ["timer", "timer"])
        self.assertEqual(entries[0]["action"]["args"]["action"], "start")
        self.assertEqual(entries[0]["phrase"], "поставь таймер на 5 минут")
        self.assertEqual((entries[0]["level"], entries[0]["ok"]), ("SAFE", True))


class TestOtherQuickPhrases(QuickCase):
    def test_what_do_i_have_today_goes_to_calendar_without_model(self):
        today = datetime.now()
        self.run.side_effect = scripted_run(f"Планёрка\t{today.year}-{today.month}-{today.day}-10-0\tfalse").side_effect
        self.assertEqual(self.mac.execute("что у меня сегодня"), "Сегодня 1 событие: Планёрка в 10:00.")
        self.assertEqual(self.mac.execute("Что у меня сегодня?"), "Сегодня 1 событие: Планёрка в 10:00.")
        entry = self.entries()[-1]
        self.assertEqual(entry["action"]["name"], "calendar")
        self.assertEqual(entry["action"]["args"]["action"], "list_today")

    def test_calendar_permission_error_is_spoken(self):
        self.run.side_effect = scripted_run(completed("", 1, "Not authorized (-1743)")).side_effect
        reply = self.mac.execute("что у меня сегодня")
        self.assertIn("Автоматизация, Календарь", reply)
        self.assertFalse(self.entries()[-1]["ok"])

    def test_settings_panes(self):
        cases = {"открой настройки Wi-Fi": "com.apple.wifi-settings-extension",
                 "открой настройки звука": "com.apple.Sound-Settings.extension",
                 "открой настройки экрана": "com.apple.Displays-Settings.extension",
                 "открой настройки bluetooth": "com.apple.BluetoothSettings",
                 "покажи настройки батареи": "com.apple.Battery-Settings.extension"}
        for phrase, ident in cases.items():
            with self.subTest(phrase=phrase):
                reply = self.mac.execute(phrase)
                self.assertTrue(reply.startswith("Открыл настройки"), reply)
                self.assertEqual(self.run.call_args.args[0], ["open", "x-apple.systempreferences:" + ident])
        self.assertEqual(self.entries()[0]["action"], {"name": "settings", "args": {"pane": "wifi"}})

    def test_settings_not_intercepted(self):
        before = self.run.call_count
        for phrase in ["открой настройки", "открой настройки паролей", "открой настройки звука и экрана"]:
            self.assertIsNone(self.mac.execute(phrase), phrase)
        self.assertEqual(self.run.call_count, before)


class TestFifteenPhrasesWithoutModel(QuickCase):
    """Не меньше 15 фраз: каждая либо выполняется без модели, либо уходит модели (None). Журнал — у выполненных."""

    def test_table(self):
        executed = ["поставь таймер на 5 минут", "поставь таймер на пять минут", "таймер на пять минут",
                    "поставь таймер на полчаса", "таймер на час", "таймер на полтора часа", "таймер на 90 секунд",
                    "сколько осталось", "что у меня сегодня", "открой настройки Wi-Fi",
                    "открой настройки звука", "открой настройки экрана", "открой настройки батареи"]
        to_model = ["через 5 минут напомни позвонить", "поставь таймер", "поставь будильник на 7", "закрой Safari",
                    "поставь таймер на пять минут и открой сафари", "что у меня завтра", "открой настройки пароли"]
        self.assertGreaterEqual(len(executed) + len(to_model), 15)
        for phrase in executed:
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(self.mac.execute(phrase))
        self.assertEqual(len(self.entries()), len(executed))        # все записаны в журнал
        for phrase in to_model:
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.mac.execute(phrase))
        self.assertEqual(len(self.entries()), len(executed))        # а переданные модели — нет


class TestCache(unittest.TestCase):
    def ok(self, name, args):
        return {"action": {"name": name, "args": args}, "ok": True}

    def setUp(self):
        isolated_apps(self, {})

    def test_time_and_personal_tools_never_cached(self):
        results = {
            "timer": ("поставь таймер на 5 минут", {"action": "start", "seconds": 300, "label": ""}),
            "alarm": ("поставь будильник на 7", {"action": "set", "when": "2099-01-01 07:00", "label": ""}),
            "reminders": ("напомни молоко", {"action": "add", "title": "молоко", "when": "", "list_name": ""}),
            "calendar": ("что у меня сегодня", {"action": "list_today", "title": "", "start": "", "minutes": 0}),
            "notes": ("создай заметку молоко", {"action": "create", "title": "молоко", "text": ""}),
            "shortcuts": ("запусти jarvis timer", {"action": "run", "name": "Jarvis Timer", "input": ""}),
        }
        for name, (phrase, args) in results.items():
            with self.subTest(name=name):
                self.assertIsNone(cacheable_actions([self.ok(name, args)], phrase))

    def test_settings_pane_cached_by_template_only(self):
        wifi = [self.ok("settings", {"pane": "wifi"})]
        self.assertEqual(cacheable_actions(wifi, "открой настройки вайфай"), [{"name": "settings", "args": {"pane": "wifi"}}])
        self.assertIsNotNone(cacheable_actions(wifi, "открой настройки Wi-Fi"))
        self.assertIsNone(cacheable_actions(wifi, "открой настройки звука"))                 # другая панель
        self.assertIsNone(cacheable_actions(wifi, "покажи что-нибудь про интернет"))          # не шаблон
        self.assertIsNone(cacheable_actions([*wifi, self.ok("settings", {"pane": "sound"})], "открой настройки вайфай"))

    def test_cached_settings_replays_without_model(self):
        cache = CommandCache()
        cache.store("открой настройки вайфай", [{"name": "settings", "args": {"pane": "wifi"}}])
        run = scripted_run(completed(""))
        with patch("core.apps.settings.subprocess.run", run):
            llm = ScriptedLLM()
            turn = AIClient(llm=llm, cache=cache).respond("открой настройки вайфай")
        self.assertEqual(llm.requests, [])
        self.assertEqual(run.call_args.args[0][0], "open")
        self.assertIn("Открыл настройки", turn.speech)

    def test_model_run_of_timer_is_not_cached(self):
        cache = CommandCache()
        client = AIClient(llm=ScriptedLLM(tools_reply(call("timer", {"action": "start", "seconds": 300, "label": ""}))),
                          cache=cache)
        client.respond("поставь таймер на 5 минут с названием чай")
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
