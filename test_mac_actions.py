"""Этап 6.1: system_info, display, media — разбор вывода, уровни, разрешения, argv, бюджет токенов.
Команды и экран не запускаются: subprocess, socket и disk_usage подменены."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import mac_actions, prompts, safety
from core.ai import ACTIONS, AIClient
from core.errors import ActionError
from core.mac_actions import display, media, system_info
from core.speech_text import prepare_for_speech
from core.tools import TOOLS
from fixtures_apps import FAKE_INSTALLED, patch_installed
from test_tools import ScriptedLLM, call, final, tools_reply

TOKEN_BUDGET_CHARS = 9000

PMSET_DISCHARGING = ("Now drawing from 'Battery Power'\n"
                     " -InternalBattery-0 (id=24838243)\t62%; discharging; 3:05 remaining present: true\n")
PMSET_CHARGING = ("Now drawing from 'AC Power'\n"
                  " -InternalBattery-0 (id=24838243)\t45%; charging; 1:12 remaining present: true\n")
PMSET_NO_ESTIMATE = " -InternalBattery-0 (id=1)\t80%; discharging; (no estimate) present: true\n"
PMSET_NOT_CHARGING = " -InternalBattery-0 (id=1)\t80%; AC attached; not charging present: true\n"
PMSET_CHARGED = " -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n"
PMSET_DESKTOP = "Now drawing from 'AC Power'\n"

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               13973.
Pages active:                            300000.
Pages inactive:                          301617.
Pages wired down:                        100000.
Pages occupied by compressor:            100000.
"""


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def scripted(*outputs):
    """subprocess.run, отдающий выводы по очереди; Exception — выбросить, completed — как есть."""
    queue = list(outputs)

    def fake(args, **kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, SimpleNamespace) else completed(item)
    return MagicMock(side_effect=fake)


class MacActionsCase(unittest.TestCase):
    def setUp(self):
        installed = patch_installed(FAKE_INSTALLED)
        installed.start()
        self.addCleanup(installed.stop)

    def run_with(self, *outputs):
        mock = scripted(*outputs)
        patcher = patch("core.mac_actions.subprocess.run", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock


class TestParsers(unittest.TestCase):
    def test_battery_states(self):
        self.assertEqual(mac_actions.parse_battery(PMSET_DISCHARGING), (62, "discharging", 185))
        self.assertEqual(mac_actions.parse_battery(PMSET_CHARGING), (45, "charging", 72))
        self.assertEqual(mac_actions.parse_battery(PMSET_NO_ESTIMATE), (80, "discharging", None))
        self.assertEqual(mac_actions.parse_battery(PMSET_NOT_CHARGING), (80, "ac", None))
        self.assertEqual(mac_actions.parse_battery(PMSET_CHARGED), (100, "charged", None))

    def test_no_battery(self):
        with self.assertRaises(ActionError) as ctx:
            mac_actions.parse_battery(PMSET_DESKTOP)
        self.assertEqual(ctx.exception.short, "у этого Mac нет батареи")

    def test_battery_phrases(self):
        self.assertEqual(mac_actions.battery_text(PMSET_DISCHARGING),
                         "Заряд 62 процента, не заряжается, хватит на 3 часа 5 минут")
        self.assertEqual(mac_actions.battery_text(PMSET_CHARGING),
                         "Заряд 45 процентов, заряжается, до полной зарядки 1 час 12 минут")
        self.assertEqual(mac_actions.battery_text(PMSET_NOT_CHARGING), "Заряд 80 процентов, не заряжается")
        self.assertEqual(mac_actions.battery_text(PMSET_CHARGED), "Заряд 100 процентов, заряжен")

    def test_vm_stat_and_memory(self):
        page, pages = mac_actions.parse_vm_stat(VM_STAT)
        self.assertEqual((page, pages["Pages active"], pages["Pages wired down"]), (16384, 300000, 100000))
        # (300000 + 100000 + 100000) * 16384 = 8,2 ГБ (1 ГБ = 2^30 -> 7,6)
        text = mac_actions.memory_text(VM_STAT, 16 * 1024 ** 3)
        self.assertEqual(text, "Занято 7,6 гигабайта памяти из 16 гигабайт")
        with self.assertRaises(ActionError):
            mac_actions.parse_vm_stat("ничего похожего")

    def test_boottime_and_wifi(self):
        self.assertEqual(mac_actions.parse_boottime("{ sec = 1792678249, usec = 199627 } Thu Oct 22 19:10:49 2026"),
                         1792678249)
        self.assertEqual(mac_actions.parse_wifi_name("Current Wi-Fi Network: Home 5G\n"), "Home 5G")
        self.assertIsNone(mac_actions.parse_wifi_name("You are not associated with an AirPort network.\n"))
        self.assertIsNone(mac_actions.parse_wifi_name("Current Wi-Fi Network: <redacted>"))
        ports = "Hardware Port: Ethernet\nDevice: en3\n\nHardware Port: Wi-Fi\nDevice: en0\n"
        self.assertEqual(mac_actions.wifi_device(ports), "en0")
        self.assertIsNone(mac_actions.wifi_device("Hardware Port: Ethernet\nDevice: en3\n"))

    def test_duration_and_plurals(self):
        self.assertEqual(mac_actions._duration(5 * 86400 + 14 * 3600 + 10 * 60), "5 дней 14 часов")
        self.assertEqual(mac_actions._duration(3 * 3600 + 5 * 60), "3 часа 5 минут")
        self.assertEqual(mac_actions._duration(20), "меньше минуты")
        self.assertEqual(mac_actions._gigabytes(120), "120 гигабайт")
        self.assertEqual(mac_actions._gigabytes(11.2), "11,2 гигабайта")


class TestSystemInfo(MacActionsCase):
    def test_battery_command_is_list_with_timeout(self):
        mock = self.run_with(PMSET_DISCHARGING)
        done = system_info("battery")
        self.assertEqual(done.speech, "Заряд 62 процента, не заряжается, хватит на 3 часа 5 минут.")
        mock.assert_called_once_with(["pmset", "-g", "batt"], capture_output=True, text=True,
                                     timeout=mac_actions.TIMEOUT)

    def test_storage(self):
        usage = SimpleNamespace(total=500_000_000_000, used=380_000_000_000, free=120_000_000_000)
        with patch("core.mac_actions.shutil.disk_usage", return_value=usage) as disk:
            self.assertEqual(system_info("storage").speech, "Свободно 120 гигабайт из 500.")
        disk.assert_called_once_with("/")

    def test_memory_cpu_uptime(self):
        self.run_with("17179869184\n", VM_STAT)
        self.assertEqual(system_info("memory").speech, "Занято 7,6 гигабайта памяти из 16 гигабайт.")
        with patch("core.mac_actions.os.getloadavg", return_value=(2.0, 1.0, 1.0)), \
                patch("core.mac_actions.os.cpu_count", return_value=8):
            self.assertEqual(system_info("cpu").speech, "Нагрузка процессора около 25 процентов.")
        with patch("core.mac_actions.os.getloadavg", return_value=(1.7, 1.0, 1.0)), \
                patch("core.mac_actions.os.cpu_count", return_value=8):
            self.assertEqual(system_info("cpu").speech, "Нагрузка процессора около 21 процента.")
        self.run_with("{ sec = 1000, usec = 0 } Thu\n")
        with patch("core.mac_actions.time.time", return_value=1000 + 2 * 86400 + 3 * 3600 + 60):
            self.assertEqual(system_info("uptime").speech, "Mac работает 2 дня 3 часа.")

    def test_wifi_name_hidden(self):
        ports = "Hardware Port: Wi-Fi\nDevice: en0\n"
        self.run_with(ports, "You are not associated with an AirPort network.\n", "192.168.1.5\n")
        self.assertEqual(system_info("wifi").speech, "Wi-Fi подключён, имя сети недоступно.")

    def test_wifi_not_connected_and_named(self):
        ports = "Hardware Port: Wi-Fi\nDevice: en0\n"
        self.run_with(ports, "You are not associated with an AirPort network.\n", completed("", 1))
        self.assertEqual(system_info("wifi").speech, "Wi-Fi не подключён.")
        self.run_with(ports, "Current Wi-Fi Network: Home\n")
        self.assertEqual(system_info("wifi").speech, "Wi-Fi подключён к сети Home.")

    def test_ip_without_network_traffic(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = ("192.168.1.5", 54321)
        with patch("core.mac_actions.socket.socket", return_value=sock):
            done = system_info("ip")
        sock.connect.assert_called_once()
        sock.send.assert_not_called()
        sock.sendto.assert_not_called()
        self.assertEqual(done.message, "Локальный адрес 192.168.1.5")
        self.assertIn("192 точка 168 точка 1 точка 5", done.speech)

    def test_macos_and_apps(self):
        with patch("core.mac_actions.platform.mac_ver", return_value=("26.4.1", ("", "", ""), "arm64")):
            self.assertEqual(system_info("macos").message, "macOS 26.4.1")
        self.run_with("Safari\nFinder\n\nTelegram\n")
        self.assertEqual(system_info("running_apps").speech, "Открыты: Safari, Finder, Telegram.")
        names = "\n".join(f"App{i}" for i in range(20))
        self.run_with(names)
        self.assertIn("и ещё 5", system_info("running_apps").speech)
        self.assertEqual(system_info("running_apps").message.count("App"), 20)  # в окне — всё
        self.run_with("Code\n")
        self.assertEqual(system_info("frontmost_app").speech, "Сейчас активно приложение Code.")

    def test_unknown_what(self):
        for bad in ("hack", "", None, 5, ["battery"]):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                system_info(bad)


class TestPermissions(MacActionsCase):
    def test_automation_error_is_understandable(self):
        self.run_with(completed("", 1, "execution error: Not authorized to send Apple events to System Events. (-1743)"))
        with self.assertRaises(ActionError) as ctx:
            system_info("frontmost_app")
        self.assertIn("Автоматизация", ctx.exception.short)
        self.assertIn("из которого запущен Jarvis", ctx.exception.reply)
        self.assertIn("→", str(ctx.exception))   # в окне и журнале — со стрелками, для голоса — запятыми
        self.assertNotIn("→", ctx.exception.reply)

    def test_accessibility_errors(self):
        for stderr in ("osascript is not allowed assistive access. (-25211)", "Invalid index. (-1719)",
                       "osascript is not allowed to send keystrokes. (1002)"):
            with self.subTest(stderr=stderr):
                self.run_with(completed("", 1, stderr))
                with self.assertRaises(ActionError) as ctx:
                    display("lock_screen", "")
                self.assertIn("Универсальный доступ", ctx.exception.short)

    def test_other_errors_and_timeout(self):
        self.run_with(completed("", 1, "что-то другое"))
        with self.assertRaises(ActionError) as ctx:
            system_info("battery")
        self.assertEqual(str(ctx.exception), "что-то другое")
        self.run_with(subprocess.TimeoutExpired("pmset", 10))
        with self.assertRaises(ActionError) as ctx:
            system_info("battery")
        self.assertEqual(ctx.exception.short, "команда не ответила вовремя")


class TestDisplay(MacActionsCase):
    def test_dark_mode_scripts(self):
        mock = self.run_with("")
        self.assertEqual(display("dark_mode_on", "").speech, "Тёмная тема включена.")
        self.assertIn("set dark mode to true", mock.call_args.args[0][2])
        display("dark_mode_off", "")
        self.assertIn("set dark mode to false", mock.call_args.args[0][2])
        display("dark_mode_toggle", "")
        self.assertIn("not dark mode", mock.call_args.args[0][2])
        self.assertEqual(mock.call_args.args[0][:2], ["osascript", "-e"])

    def test_lock_and_sleep(self):
        mock = self.run_with("")
        display("lock_screen", "")
        self.assertIn('keystroke "q" using {control down, command down}', mock.call_args.args[0][2])
        self.assertEqual(display("sleep_display", "").speech, "Экран выключен.")
        self.assertEqual(mock.call_args.args[0], ["pmset", "displaysleepnow"])

    def test_brightness_goes_through_isolated_process(self):
        mock = self.run_with("90\n")
        self.assertEqual(display("brightness_down", "").speech, "Яркость 90 процентов.")
        args = mock.call_args.args[0]
        self.assertEqual(args[:3], [sys.executable, "-I", "-c"])
        self.assertEqual(args[4:], ["delta", "-10"])
        display("brightness_up", "")
        self.assertEqual(mock.call_args.args[0][4:], ["delta", "10"])
        display("brightness_set", "35%")
        self.assertEqual(mock.call_args.args[0][4:], ["set", "35"])

    def test_brightness_value_checked_before_running(self):
        mock = self.run_with("50")
        for bad in ("", "abc", "101", "-1", True, None):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                display("brightness_set", bad)
        mock.assert_not_called()

    def test_brightness_unavailable(self):
        self.run_with(completed("", 3))
        with self.assertRaises(ActionError) as ctx:
            display("brightness_up", "")
        self.assertEqual(ctx.exception.short, "регулировка яркости недоступна")

    def shoot(self, home, value, *, now=(2026, 10, 28, 9, 5, 7)):
        """Скриншот в замоканном screencapture: создаёт файл по пути из аргументов."""
        def fake(args, **kwargs):
            Path(args[-1]).write_bytes(b"png")
            return completed()
        with patch("core.mac_actions.subprocess.run", side_effect=fake) as run, \
                patch("core.mac_actions.datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(*now)
            return mac_actions._screenshot(value, home), run

    def make_home(self, tmp):
        for name in ("Desktop", "Documents", "Downloads", "Pictures"):
            (Path(tmp) / name).mkdir()
        return Path(tmp)

    def test_screenshot_name_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            first, run = self.shoot(home, "")
            second, _ = self.shoot(home, "")
            self.assertEqual(first.speech, "Скриншот сохранён на рабочем столе.")
            names = sorted(p.name for p in (home / "Desktop").iterdir())
            self.assertEqual(names, ["Снимок экрана 2026-10-28 09.05.07 (2).png", "Снимок экрана 2026-10-28 09.05.07.png"])
            self.assertEqual(run.call_args.args[0][:2], ["screencapture", "-x"])
            self.assertIn("Снимок экрана", second.message)

    def test_screenshot_folders(self):
        expected = {"": ("Desktop", "на рабочем столе"), "desktop": ("Desktop", "на рабочем столе"),
                    "documents": ("Documents", "в документах"), "downloads": ("Downloads", "в загрузках"),
                    " Documents ": ("Documents", "в документах")}
        for value, (folder, where) in expected.items():
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                home = self.make_home(tmp)
                done, run = self.shoot(home, value)
                self.assertEqual(done.speech, f"Скриншот сохранён {where}.")
                saved = Path(run.call_args.args[0][-1])
                self.assertEqual(saved.parent, home / folder)
                self.assertIn(str(home / folder), done.message)   # в окне и журнале — полный путь

    def test_screenshot_other_folders_refused(self):
        hint = "Могу сохранить на рабочий стол, в документы или в загрузки"
        for value in ("pictures", "~/Pictures", "/tmp", "../etc", "documents/..", "Library", None, 5):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                home = self.make_home(tmp)
                mock = self.run_with("")
                with self.assertRaises(ActionError) as ctx:
                    mac_actions._screenshot(value, home)
                self.assertEqual(ctx.exception.short, hint)
                self.assertEqual(ctx.exception.reply, hint + ".")
                mock.assert_not_called()

    def test_screenshot_through_display_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)

            def fake(args, **kwargs):
                Path(args[-1]).write_bytes(b"png")
                return completed()
            with patch("core.mac_actions.subprocess.run", side_effect=fake), \
                    patch("core.mac_actions.Path.home", return_value=home):
                self.assertEqual(display("screenshot", "downloads").speech, "Скриншот сохранён в загрузках.")
                self.assertEqual(len(list((home / "Downloads").iterdir())), 1)

    def test_screenshot_retries_once_after_display_error(self):
        err = completed("", 1, "screencapture: could not create image from display 0x1")
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            calls = []

            def fake(args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    return err
                Path(args[-1]).write_bytes(b"png")
                return completed()
            with patch("core.mac_actions.subprocess.run", side_effect=fake), \
                    patch("core.mac_actions.time.sleep") as sleep:
                done = mac_actions._screenshot("", home)
            self.assertEqual(len(calls), 2)
            sleep.assert_called_once_with(1.0)
            self.assertEqual(done.speech, "Скриншот сохранён на рабочем столе.")

    def test_screenshot_permission_after_second_failure(self):
        err = completed("", 1, "screencapture: could not create image from display 0x1")
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            mock = self.run_with(err)
            with patch("core.mac_actions.time.sleep") as sleep, self.assertRaises(ActionError) as ctx:
                mac_actions._screenshot("", home)
            self.assertEqual(mock.call_count, 2)    # ровно один повтор
            sleep.assert_called_once_with(1.0)
            self.assertEqual(str(ctx.exception), "Нужно разрешение: Системные настройки → Конфиденциальность и "
                                                 "безопасность → Запись экрана для приложения, из которого запущен Jarvis.")
            self.assertIn("Запись экрана", ctx.exception.reply)
            self.assertNotIn("→", ctx.exception.reply)

    def test_screenshot_other_errors_not_retried(self):
        mock = self.run_with(completed("", 1, "что-то другое"))
        with tempfile.TemporaryDirectory() as tmp, patch("core.mac_actions.time.sleep") as sleep, \
                self.assertRaises(ActionError) as ctx:
            mac_actions._screenshot("", self.make_home(tmp))
        self.assertEqual(mock.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(str(ctx.exception), "что-то другое")

    def test_screenshot_without_file_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_with("")
            with self.assertRaises(ActionError) as ctx:
                mac_actions._screenshot("", self.make_home(tmp))
            self.assertIn("Запись экрана", ctx.exception.short)

    def test_unknown_action(self):
        for bad in ("format_disk", "", None):
            with self.subTest(bad=bad), self.assertRaises(ActionError):
                display(bad, "")


class TestMedia(MacActionsCase):
    def test_app_name_goes_through_argv(self):
        mock = self.run_with("true", "")
        self.assertEqual(media("pause", "Music").speech, "Пауза.")
        args = mock.call_args.args[0]
        self.assertEqual(args[-1], "Music")
        script = " ".join(a for a in args if a != "-e")
        self.assertIn("(item 1 of argv)", script)
        self.assertIn("pause", script)
        self.assertNotIn("shell", script)

    def test_spotify_passed_as_argv_not_script(self):
        with patch_installed(FAKE_INSTALLED + ["Spotify"]):
            mock = self.run_with("true", "")
            media("next", "Spotify")
        args = mock.call_args.args[0]
        self.assertEqual(args[-1], "Spotify")
        self.assertFalse(any("Spotify" in a for a in args[:-1]))

    def test_spotify_not_installed(self):
        mock = self.run_with("true")
        with self.assertRaises(ActionError) as ctx:
            media("play", "Spotify")
        self.assertEqual(ctx.exception.short, "Spotify не установлен")
        mock.assert_not_called()

    def test_not_running_is_refused_except_play(self):
        mock = self.run_with("false")
        with self.assertRaises(ActionError) as ctx:
            media("next", "Music")
        self.assertEqual(ctx.exception.short, "Music не запущен")
        self.assertEqual(mock.call_count, 1)   # проверка запуска, сама команда не отправлялась
        mock = self.run_with("")
        self.assertEqual(media("play", "Music").speech, "Играю.")   # play запускает плеер

    def test_now_playing(self):
        self.run_with("true", "playing\nBelieve\nCher")
        self.assertEqual(media("now_playing", "Music").speech, "Играет трек Believe — Cher.")
        self.run_with("true", "paused\nBelieve\nCher")
        self.assertEqual(media("now_playing", "Music").speech, "На паузе трек Believe — Cher.")
        self.run_with("true", "stopped")
        self.assertEqual(media("now_playing", "Music").speech, "Сейчас ничего не играет.")

    def test_player_volume(self):
        mock = self.run_with("true", "60")
        self.assertEqual(media("volume_up", "Music").speech, "Громкость Music 60.")
        self.assertEqual(mock.call_args.args[0][-2:], ["Music", "10"])
        mock = self.run_with("true", "40")
        media("volume_down", "Music")
        self.assertEqual(mock.call_args.args[0][-2:], ["Music", "-10"])

    def test_permission_denied(self):
        self.run_with("true", completed("", 1, "Not authorized to send Apple events to Music. (-1743)"))
        with self.assertRaises(ActionError) as ctx:
            media("pause", "Music")
        self.assertIn("Автоматизация", ctx.exception.short)

    def test_pick_media_app(self):
        with patch_installed(FAKE_INSTALLED + ["Spotify"]):
            self.run_with("true", "false", "playing\nA\nB")        # Music играет, Spotify не запущен
            self.assertEqual(mac_actions.pick_media_app(), "Music")
            self.run_with("true", "true", "paused\nA\nB", "playing\nC\nD")   # играющий важнее порядка
            self.assertEqual(mac_actions.pick_media_app(), "Spotify")
            self.run_with("true", "true", "paused\nA\nB", "paused\nC\nD")    # никто не играет — первый запущенный
            self.assertEqual(mac_actions.pick_media_app(), "Music")
            self.run_with("false")
            self.assertIsNone(mac_actions.pick_media_app())

    def test_bad_app_or_action(self):
        mock = self.run_with("true")
        for app in ("Terminal", "", None, "Music; quit"):
            with self.subTest(app=app), self.assertRaises(ActionError):
                media("pause", app)
        with self.assertRaises(ActionError):
            media("eject", "Music")
        mock.assert_not_called()


class TestSafetyLevels(unittest.TestCase):
    def classify(self, name, **args):
        return safety.classify({"name": name, "args": args})[0]

    def test_all_declared_values_are_safe(self):
        for what in mac_actions.SYSTEM_INFO_VALUES:
            self.assertEqual(self.classify("system_info", what=what), safety.SAFE, what)
        for action in mac_actions.DISPLAY_VALUES:
            self.assertEqual(self.classify("display", action=action, value=""), safety.SAFE, action)
        for action in mac_actions.MEDIA_VALUES:
            for app in ("Music", "Spotify"):
                self.assertEqual(self.classify("media", action=action, app=app), safety.SAFE, (action, app))

    def test_unsupported_action_blocked(self):
        for name, key in (("system_info", "what"), ("display", "action"), ("media", "action")):
            for bad in ("format", "", None, 5, ["battery"], "BATTERY", "battery; rm -rf /"):
                with self.subTest(name=name, bad=bad):
                    args = {key: bad, "value": "", "app": "Music"}
                    self.assertEqual(self.classify(name, **args), safety.BLOCKED)
            self.assertEqual(safety.classify({"name": name, "args": {}})[0], safety.BLOCKED)

    def test_media_other_apps_blocked(self):
        for app in ("Terminal", "", None, "Safari", "music"):
            self.assertEqual(self.classify("media", action="pause", app=app), safety.BLOCKED, app)

    def test_execute_rechecks_level(self):
        ai = AIClient(llm=ScriptedLLM(final("x")))
        [result] = ai.run_actions([{"name": "display", "args": {"action": "format_disk", "value": ""}}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["short"], "это запрещено")

    def test_tables_and_tools_agree(self):
        self.assertEqual(set(mac_actions.TOOL_FUNCS), set(safety.GROUP_VALUES))
        self.assertTrue(set(mac_actions.TOOL_FUNCS) <= set(ACTIONS))
        self.assertEqual(set(mac_actions.SYSTEM_INFO_VALUES), set(mac_actions.SYSTEM_INFO))
        self.assertEqual(set(mac_actions.DISPLAY_VALUES), set(mac_actions.DISPLAY))
        tools = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}
        for name, param in safety.GROUP_PARAMS.items():
            enum = tools[name]["properties"][param]["enum"]
            self.assertEqual(set(enum), safety.GROUP_VALUES[name], name)
            self.assertEqual(len(enum), len(set(enum)))
            self.assertEqual(set(tools[name]["required"]), set(tools[name]["properties"]), name)
        self.assertEqual(tools["media"]["properties"]["app"]["enum"], list(mac_actions.MEDIA_APPS))

    def test_supported_values_match_task(self):
        self.assertEqual(set(mac_actions.SYSTEM_INFO_VALUES),
                         {"battery", "storage", "memory", "cpu", "uptime", "wifi", "ip", "macos", "running_apps",
                          "frontmost_app"})
        self.assertEqual(set(mac_actions.DISPLAY_VALUES),
                         {"brightness_up", "brightness_down", "brightness_set", "dark_mode_on", "dark_mode_off",
                          "dark_mode_toggle", "lock_screen", "sleep_display", "screenshot"})
        self.assertEqual(set(mac_actions.MEDIA_VALUES),
                         {"play", "pause", "toggle", "next", "previous", "now_playing", "volume_up", "volume_down"})


class TestTokenBudget(unittest.TestCase):
    def test_tool_schemas_fit_budget(self):
        size = len(json.dumps(TOOLS, ensure_ascii=False))
        self.assertLessEqual(size, TOKEN_BUDGET_CHARS, f"схемы инструментов {size} символов, бюджет {TOKEN_BUDGET_CHARS}")

    def test_prompt_unchanged_limit(self):
        self.assertLessEqual(len(prompts.tools_prompt()), prompts.MAX_PROMPT_CHARS)


class TestModelFlow(MacActionsCase):
    def test_system_info_answer_without_second_request(self):
        """Шаблон: результат читается сразу, второй запрос к модели не нужен (экономия токенов Groq)."""
        self.run_with(PMSET_DISCHARGING)
        llm = ScriptedLLM(tools_reply(call("system_info", {"what": "battery"})))
        turn = AIClient(llm=llm).respond("сколько у меня осталось батареи на ноутбуке")
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Заряд 62 процента, не заряжается, хватит на 3 часа 5 минут.")

    def test_two_results_in_one_phrase(self):
        usage = SimpleNamespace(total=500_000_000_000, used=380_000_000_000, free=120_000_000_000)
        self.run_with(PMSET_CHARGED)
        llm = ScriptedLLM(tools_reply(call("system_info", {"what": "battery"}, "a"),
                                      call("system_info", {"what": "storage"}, "b")))
        with patch("core.mac_actions.shutil.disk_usage", return_value=usage):
            turn = AIClient(llm=llm).respond("заряд и место на диске")
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Заряд 100 процентов, заряжен, свободно 120 гигабайт из 500.")

    def test_error_goes_back_to_model(self):
        self.run_with(completed("", 1, "Not authorized (-1743)"))
        llm = ScriptedLLM(tools_reply(call("system_info", {"what": "frontmost_app"})),
                          final("Нужно разрешение на Автоматизацию."))
        turn = AIClient(llm=llm).respond("какое приложение активно")
        self.assertEqual(len(llm.requests), 2)
        self.assertIn("Автоматизация", json.loads(llm.requests[1]["messages"][-1]["content"])["error"])
        self.assertIn("разрешение", turn.speech)

    def test_voice_speech_has_words_not_digits(self):
        self.assertEqual(prepare_for_speech("Яркость 90 процентов."), "Яркость девяносто процентов.")


if __name__ == "__main__":
    unittest.main()
