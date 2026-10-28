"""Планировщик таймеров и будильников: подменённые часы, хранение 600, восстановление, пропущенные, срабатывание."""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core import journal, scheduler
from core.errors import ActionError
from core.scheduler import MAX_ACTIVE, Scheduler, fire_text, make_fire_handler, make_missed_handler, missed_phrase


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "data" / "timers.json"
        self.clock = Clock()

    def make(self):
        return Scheduler(self.path, clock=self.clock)


class TestFiring(SchedulerCase):
    def test_timer_fires_when_due_not_before(self):
        s = self.make()
        fired = []
        s.on_fire = fired.append
        s.add("timer", self.clock.t + 60, "чай")
        s.tick()
        self.assertEqual(fired, [])
        self.clock.t += 59
        s.tick()
        self.assertEqual(fired, [])
        self.clock.t += 1
        s.tick()
        self.assertEqual([i["label"] for i in fired], ["чай"])
        self.assertEqual(s.items(), [])
        s.tick()
        self.assertEqual(len(fired), 1)   # второй раз не срабатывает

    def test_several_due_fire_in_order_and_failure_does_not_stop_others(self):
        s = self.make()
        order = []

        def on_fire(item):
            order.append(item["label"])
            if item["label"] == "bad":
                raise RuntimeError("сбой озвучки")
        s.on_fire = on_fire
        s.add("timer", self.clock.t + 30, "second")
        s.add("timer", self.clock.t + 10, "bad")
        s.add("alarm", self.clock.t + 20, "middle")
        self.clock.t += 100
        with self.assertLogs("core.scheduler", level="ERROR"):
            s.tick()
        self.assertEqual(order, ["bad", "middle", "second"])
        self.assertEqual(s.items(), [])

    def test_remaining_and_cancel(self):
        s = self.make()
        item = s.add("timer", self.clock.t + 300, "")
        self.clock.t += 100
        self.assertEqual(s.remaining(item), 200)
        s.remove(item["id"])
        self.assertEqual(s.items(), [])

    def test_max_20_active(self):
        s = self.make()
        for i in range(MAX_ACTIVE):
            s.add("timer", self.clock.t + 100 + i, str(i))
        with self.assertRaises(ActionError) as ctx:
            s.add("alarm", self.clock.t + 500, "лишний")
        self.assertIn("20", ctx.exception.short)
        self.assertEqual(len(s.items()), 20)
        s.remove(s.items()[0]["id"])
        s.add("alarm", self.clock.t + 500, "теперь можно")


class TestPersistence(SchedulerCase):
    def test_file_is_private_and_atomic(self):
        s = self.make()
        s.add("timer", self.clock.t + 60, "чай")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertFalse(self.path.with_name("timers.json.tmp").exists())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["items"][0]["label"], "чай")

    def test_restart_restores_active(self):
        s = self.make()
        s.add("timer", self.clock.t + 600, "чай")
        s.add("alarm", self.clock.t + 3600, "подъём")
        again = self.make()
        self.assertEqual([(i["kind"], i["label"]) for i in again.items()], [("timer", "чай"), ("alarm", "подъём")])
        self.assertEqual(again.missed, [])
        fired = []
        again.on_fire = fired.append
        self.clock.t += 601
        again.tick()
        self.assertEqual([i["label"] for i in fired], ["чай"])

    def test_missed_during_downtime_announced_once(self):
        s = self.make()
        s.add("alarm", self.clock.t + 100, "подъём")
        s.add("timer", self.clock.t + 200, "чай")
        s.add("timer", self.clock.t + 99999, "потом")
        self.clock.t += 5000      # Jarvis был выключен
        again = self.make()
        self.assertEqual([i["label"] for i in again.missed], ["подъём", "чай"])
        self.assertEqual([i["label"] for i in again.items()], ["потом"])
        heard = []
        again.on_missed = heard.append
        again.on_fire = lambda item: self.fail("пропущенные не должны срабатывать по одному")
        self.assertEqual(len(again.announce_missed()), 2)
        self.assertEqual(len(heard), 1)            # одна фраза на всё
        self.assertEqual(again.announce_missed(), [])
        self.assertEqual(self.make().missed, [])   # и после следующего запуска не возвращаются

    def test_broken_file_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.make().items(), [])
        self.path.write_text(json.dumps({"items": [{"kind": "bomb", "id": "x", "due": 1}, "мусор",
                                                   {"kind": "timer", "id": "ok", "due": self.clock.t + 5, "label": "a"}]}))
        s = self.make()
        self.assertEqual([i["id"] for i in s.items()], ["ok"])

    def test_memory_only_without_path(self):
        s = Scheduler(clock=self.clock)
        s.add("timer", self.clock.t + 5, "")
        self.assertFalse(self.path.exists())


class TestPhrases(unittest.TestCase):
    def test_fire_text(self):
        self.assertEqual(fire_text({"kind": "timer", "label": "чай"}), "Таймер «чай» сработал.")
        self.assertEqual(fire_text({"kind": "timer", "label": ""}), "Таймер сработал.")
        self.assertEqual(fire_text({"kind": "alarm", "label": "подъём"}), "Будильник: подъём.")
        self.assertEqual(fire_text({"kind": "alarm", "label": ""}), "Будильник.")

    def test_missed_phrase_one_sentence(self):
        alarm_time = datetime(2026, 10, 28, 7, 0).timestamp()
        self.assertEqual(missed_phrase([{"kind": "alarm", "label": "", "due": alarm_time},
                                        {"kind": "timer", "label": "чай", "due": 1}]),
                         "Пока Jarvis был выключен, сработали: будильник на 7:00, таймер «чай».")
        self.assertEqual(missed_phrase([{"kind": "timer", "label": "", "due": 1}]),
                         "Пока Jarvis был выключен, сработал таймер.")


class TestFireHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = journal.JOURNAL_FILE
        journal.setup_journal(Path(self.tmp.name) / "actions.jsonl")
        self.addCleanup(setattr, journal, "JOURNAL_FILE", old)
        self.popen, self.run, self.speak = MagicMock(), MagicMock(), MagicMock()
        self.handler = make_fire_handler(self.speak, popen=self.popen, run=self.run)

    def test_sound_notification_speech_journal(self):
        order = []
        self.popen.side_effect = lambda *a, **k: order.append("sound")
        self.run.side_effect = lambda *a, **k: order.append("notify")
        self.speak.side_effect = lambda text: order.append("speak")
        self.handler({"kind": "timer", "label": "чай", "id": "a1", "due": 1})
        self.assertEqual(order, ["sound", "notify", "speak"])
        self.popen.assert_called_once_with(["afplay", "/System/Library/Sounds/Glass.aiff"])
        args = self.run.call_args.args[0]
        self.assertEqual(args[0], "osascript")
        self.assertEqual(args[-2:], ["Таймер «чай» сработал.", "Jarvis"])   # текст — через argv
        self.assertNotIn("чай", " ".join(args[:-2]))
        self.speak.assert_called_once_with("Таймер «чай» сработал.")
        entry = json.loads((Path(self.tmp.name) / "actions.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(entry["action"], {"name": "timer", "args": {"action": "fire", "label": "чай"}})
        self.assertTrue(entry["ok"])

    def test_sound_failure_does_not_block_speech(self):
        self.popen.side_effect = OSError("нет afplay")
        self.run.side_effect = OSError("нет osascript")
        self.handler({"kind": "alarm", "label": "", "id": "b", "due": 1})
        self.speak.assert_called_once_with("Будильник.")

    def test_speech_goes_through_engine_lock_without_overlap(self):
        """Срабатывание говорит через SpeechEngine.speak: пока идёт другая фраза, ждёт очереди (общий замок)."""
        from core.engine import SpeechEngine
        engine = SpeechEngine.__new__(SpeechEngine)
        engine._speak_lock = threading.Lock()
        engine._stop_generation = 0
        engine._proc = None
        engine.speaking = threading.Event()
        engine._last_speech_end = 0.0
        engine.recent_spoken = __import__("collections").deque(maxlen=3)
        engine.voice = None
        active, overlaps = [], []

        class Proc:
            def wait(self_inner):
                active.append(1)
                if len(active) > 1:
                    overlaps.append(1)
                time.sleep(0.05)
                active.pop()

        with patch("core.engine.subprocess.Popen", return_value=Proc()):
            handler = make_fire_handler(engine.speak, popen=MagicMock(), run=MagicMock())
            talker = threading.Thread(target=engine.speak, args=("Длинная фраза ассистента",))
            talker.start()
            time.sleep(0.01)
            handler({"kind": "timer", "label": "", "id": "c", "due": 1})
            talker.join()
        self.assertEqual(overlaps, [])

    def test_missed_handler_speaks_once_and_journals(self):
        items = [{"kind": "timer", "label": "чай", "id": "1", "due": 1}, {"kind": "alarm", "label": "", "id": "2",
                                                                       "due": datetime(2026, 1, 1, 7, 0).timestamp()}]
        make_missed_handler(self.speak)(items)
        self.speak.assert_called_once()
        lines = (Path(self.tmp.name) / "actions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["action"]["args"]["action"], "missed")


class TestThread(unittest.TestCase):
    def test_background_thread_fires_and_stops(self):
        s = Scheduler()
        fired = threading.Event()
        s.on_fire = lambda item: fired.set()
        s.add("timer", time.time() + 0.1, "быстрый")
        s.start()
        self.addCleanup(s.stop)
        self.assertTrue(fired.wait(3))
        s.stop()

    def test_default_scheduler_is_memory_only(self):
        scheduler.set_scheduler(None)
        self.addCleanup(scheduler.set_scheduler, None)
        s = scheduler.get_scheduler()
        self.assertIsNone(s.path)
        self.assertIs(scheduler.get_scheduler(), s)


if __name__ == "__main__":
    unittest.main()
