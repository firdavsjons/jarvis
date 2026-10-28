"""Реестр приложений core/apps/: подхват новых файлов, схемы, уровни, неизвестные действия, запрет записи в файлы
разрешений (shortcuts_allowed.json, trusted_domains.txt)."""
import builtins
import io
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core import safety
from core.ai import ACTIONS, AIClient
from core.apps import registry, shortcuts
from core.apps.registry import BLOCKED, CONFIRM, SAFE, TOPICS, AppTool
from core.errors import ActionError
from core.tools import TOOLS
from fixtures_apps import patch_installed
from fixtures_run import completed, isolated_apps, scripted_run
from test_tool_schemas import schema_problems
from test_tools import ScriptedLLM, final

EXPECTED = {"timer", "alarm", "reminders", "calendar", "notes", "shortcuts", "settings", "telegram"}

NEW_APP = textwrap.dedent('''
    from core.apps import common
    from core.apps.registry import SAFE, AppTool

    def run(action, text, phrase=""):
        return common.done("Привет из нового файла: " + text)

    TOOLS = [AppTool(name="demo_app", description="Демо.", topic="apps", keywords=("демо",),
                     actions={"hello": SAFE}, params={"text": {"type": "string", "description": "Текст"}}, run=run)]
''')


class TestRegistry(unittest.TestCase):
    def setUp(self):
        isolated_apps(self, {})

    def test_all_tools_found(self):
        self.assertEqual(registry.names(), EXPECTED)

    def test_new_file_is_picked_up_without_other_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "fake_apps_pkg"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "demo.py").write_text(NEW_APP, encoding="utf-8")
            sys.path.insert(0, tmp)
            self.addCleanup(sys.path.remove, tmp)
            self.addCleanup(sys.modules.pop, "fake_apps_pkg.demo", None)
            found = registry.load_tools(package="fake_apps_pkg", path=[str(package)])
        self.assertEqual(set(found), {"demo_app"})
        tool = found["demo_app"]
        self.assertEqual(tool.classify({"action": "hello"}), (SAFE, "только чтение или безобидное изменение"))
        self.assertEqual(tool.classify({"action": "bye"})[0], BLOCKED)
        self.assertEqual(tool.schema()["function"]["parameters"]["required"], ["action", "text"])
        self.assertEqual(tool.run(action="hello", text="мир").speech, "Привет из нового файла: мир.")

    def test_duplicate_and_bad_topic_rejected(self):
        for n, (body, expected) in enumerate([("TOOLS = [make('a'), make('a')]", "дважды"),
                                              ("TOOLS = [make('b', topic='nope')]", "тема")]):
            pkg_name = f"bad_apps_pkg{n}"
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp) / pkg_name
                package.mkdir()
                (package / "__init__.py").write_text("")
                (package / "x.py").write_text(
                    "from core.apps.registry import SAFE, AppTool\n"
                    "def make(name, topic='apps'):\n"
                    "    return AppTool(name=name, description='д', topic=topic, keywords=(), actions={'a': SAFE},"
                    " run=lambda **k: None)\n" + body, encoding="utf-8")
                sys.path.insert(0, tmp)
                self.addCleanup(sys.path.remove, tmp)
                self.addCleanup(sys.modules.pop, f"{pkg_name}.x", None)
                with self.assertRaises(ValueError) as ctx:
                    registry.load_tools(package=pkg_name, path=[str(package)])
                self.assertIn(expected, str(ctx.exception))

    def test_every_tool_follows_schema_rules(self):
        schemas = registry.schemas()
        self.assertEqual(schema_problems(schemas), [])
        for tool in registry.tools().values():
            params = tool.schema()["function"]["parameters"]
            self.assertEqual(params["required"], list(params["properties"]), tool.name)   # все параметры обязательные
            enum = params["properties"][tool.action_param]["enum"]
            self.assertEqual(enum, list(tool.actions), tool.name)
            self.assertIn(tool.topic, TOPICS)
            self.assertTrue(tool.keywords, tool.name)

    def test_levels_for_every_tool_and_action(self):
        for tool in registry.tools().values():
            for action, declared in tool.actions.items():
                self.assertIn(declared, {SAFE, CONFIRM}, (tool.name, action))
                args = {tool.action_param: action, "name": "Jarvis Alarm", "app": "Music"}
                level, _ = safety.classify({"name": tool.name, "args": args})
                self.assertIn(level, {SAFE, CONFIRM, BLOCKED})
                if tool.name != "shortcuts":
                    self.assertEqual(level, declared, (tool.name, action))

    def test_unknown_action_blocked_for_every_tool(self):
        for tool in registry.tools().values():
            for bad in ("format_disk", "", None, 5, ["start"], "START", "start; rm -rf /"):
                with self.subTest(tool=tool.name, bad=bad):
                    self.assertEqual(safety.classify({"name": tool.name, "args": {tool.action_param: bad}})[0], BLOCKED)
            self.assertEqual(safety.classify({"name": tool.name, "args": {}})[0], BLOCKED)
            self.assertEqual(safety.classify({"name": tool.name, "args": "строка"})[0], BLOCKED)

    def test_tools_in_whitelist_and_schemas(self):
        self.assertTrue(registry.names() <= safety.ACTION_NAMES)
        self.assertTrue({"more_tools"} <= safety.ACTION_NAMES)
        names = {t["function"]["name"] for t in TOOLS}
        self.assertEqual(names, safety.ACTION_NAMES)

    def test_delete_like_actions_are_confirm_and_destructive(self):
        for name, action in [("reminders", "delete"), ("calendar", "delete")]:
            tool = registry.get(name)
            self.assertEqual(tool.actions[action], CONFIRM)
            self.assertIn(action, tool.destructive)
            self.assertIsNotNone(tool.preview)

    def test_external_data_flags(self):
        flags = {name: tool.external for name, tool in registry.tools().items()}
        self.assertEqual(flags["calendar"], frozenset({"list_today", "list_tomorrow", "list_week"}))
        self.assertEqual(flags["reminders"], frozenset({"list"}))
        self.assertEqual(flags["notes"], frozenset({"list_titles", "read_last"}))
        self.assertEqual(flags["shortcuts"], frozenset({"run"}))
        for name in ("timer", "alarm", "settings"):
            self.assertEqual(flags[name], frozenset(), name)


class WriteGuard:
    """Фиксирует любую запись, удаление или переименование защищённых файлов."""

    def __init__(self, *paths):
        self.paths = {os.path.realpath(p) for p in paths}
        self.hits = []

    def _check(self, path, how):
        try:
            if os.path.realpath(os.fspath(path)) in self.paths:
                self.hits.append((how, str(path)))
        except (TypeError, ValueError):
            pass

    def patches(self):
        real_open, real_io_open, real_os_open = builtins.open, io.open, os.open
        real_replace, real_rename, real_remove, real_unlink = os.replace, os.rename, os.remove, os.unlink
        guard = self

        def writing(mode):
            return isinstance(mode, str) and any(c in mode for c in "wax+")

        def fake_open(file, mode="r", *a, **k):
            if writing(mode):
                guard._check(file, "open")
            return real_open(file, mode, *a, **k)

        def fake_io_open(file, mode="r", *a, **k):
            if writing(mode):
                guard._check(file, "io.open")
            return real_io_open(file, mode, *a, **k)

        def fake_os_open(path, flags, *a, **k):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                guard._check(path, "os.open")
            return real_os_open(path, flags, *a, **k)

        def move_like(real, how):
            def fake(src, dst, *a, **k):
                guard._check(src, how)
                guard._check(dst, how)
                return real(src, dst, *a, **k)
            return fake

        def remove_like(real, how):
            def fake(path, *a, **k):
                guard._check(path, how)
                return real(path, *a, **k)
            return fake
        return [patch.object(builtins, "open", fake_open), patch.object(io, "open", fake_io_open),
                patch.object(os, "open", fake_os_open), patch.object(os, "replace", move_like(real_replace, "replace")),
                patch.object(os, "rename", move_like(real_rename, "rename")),
                patch.object(os, "remove", remove_like(real_remove, "remove")),
                patch.object(os, "unlink", remove_like(real_unlink, "unlink")),
                patch.object(shutil, "move", move_like(shutil.move, "shutil.move")),
                patch.object(shutil, "copyfile", move_like(shutil.copyfile, "shutil.copyfile"))]


SAMPLE_ARGS = {
    "action": None, "what": "battery", "pane": "wifi",
    "name": "Jarvis Alarm", "input": "07:30", "seconds": 60, "label": "x", "when": "", "title": "x", "text": "x",
    "list_name": "", "start": "", "minutes": 30, "value": "", "app": "Music", "area": "web", "to": "мама",
}
ATTACK_STRINGS = ["shortcuts_allowed.json", "../shortcuts_allowed.json", "trusted_domains.txt",
                  "~/Library/Application Support/Jarvis/shortcuts_allowed.json", "/etc/passwd", "x; rm -rf ~"]


class TestNoWritesToPermissionFiles(unittest.TestCase):
    """Файлы shortcuts_allowed.json и trusted_domains.txt меняет только пользователь руками."""

    def setUp(self):
        self.sched, self.allowed = isolated_apps(self, {"Jarvis Alarm": "SAFE", "Jarvis Timer": "SAFE",
                                                        "Выключить свет": "CONFIRM"})
        installed = patch_installed()
        installed.start()
        self.addCleanup(installed.stop)
        self.trusted = Path(safety.TRUSTED_DOMAINS_FILE)
        self.contacts = self.contacts_file     # исходные контакты теста (isolated_apps)
        protected = (self.allowed, self.trusted, self.contacts)
        self.before = {p: p.read_bytes() for p in protected}
        self.mtimes = {p: p.stat().st_mtime_ns for p in protected}

    def run_everything(self, guard):
        for patcher in guard.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        run_mock = scripted_run(completed("Jarvis Alarm\nJarvis Timer\nВыключить свет\n"))
        for target in ("core.apps.common.subprocess.run", "core.apps.shortcuts.subprocess.run",
                       "core.apps.settings.subprocess.run", "core.mac_actions.subprocess.run",
                       "core.ai.subprocess.run"):
            patch(target, run_mock).start()
            self.addCleanup(patch.stopall)
        calls = 0
        names = set(registry.tools()) | {"system_info", "display", "media"}
        for name in sorted(names):
            tool = registry.get(name)
            actions = list(tool.actions) if tool else None
            if tool is None:
                from core import mac_actions
                actions = {"system_info": mac_actions.SYSTEM_INFO_VALUES, "display": mac_actions.DISPLAY_VALUES,
                           "media": mac_actions.MEDIA_VALUES}[name]
            param = tool.action_param if tool else ("what" if name == "system_info" else "action")
            for action in actions:
                for attack in [None, *ATTACK_STRINGS]:
                    args = dict(SAMPLE_ARGS)
                    args[param] = action
                    if attack:
                        args.update({"name": attack, "input": attack, "title": attack, "text": attack,
                                     "label": attack, "list_name": attack, "value": attack, "to": attack})
                    funcs = [tool.run if tool else ACTIONS[name]]
                    if tool is not None:      # хуки подготовки черновика и отмены тоже не должны ничего писать
                        funcs += [h for h in (tool.prepare, tool.preview, tool.discard) if h is not None]
                    for func in funcs:
                        kwargs = {k: v for k, v in args.items() if k in self._params(func)}
                        try:
                            func(**kwargs)
                        except (ActionError, TypeError, ValueError, OSError):
                            pass
                        calls += 1
        return calls

    @staticmethod
    def _params(func):
        import inspect
        return set(inspect.signature(func).parameters)

    def test_no_tool_or_action_writes_to_permission_files(self):
        guard = WriteGuard(self.allowed, self.trusted, self.contacts)
        calls = self.run_everything(guard)
        self.assertGreater(calls, 150)       # перебор действительно прошёл по всем инструментам и действиям
        self.assertEqual(guard.hits, [])
        for path, data in self.before.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mtime_ns, self.mtimes[path])

    def test_model_flow_cannot_write_either(self):
        """Через AIClient: ответ модели с вызовом shortcuts / любым действием не пишет в эти файлы."""
        guard = WriteGuard(self.allowed, self.trusted, self.contacts)
        for patcher in guard.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        patch("core.apps.shortcuts.subprocess.run", scripted_run(completed("Jarvis Alarm\n"))).start()
        patch("core.apps.common.subprocess.run", scripted_run(completed(""))).start()
        self.addCleanup(patch.stopall)
        from test_tools import call, tools_reply
        for payload in ATTACK_STRINGS:
            client = AIClient(llm=ScriptedLLM(tools_reply(call("shortcuts", {"action": "run", "name": payload,
                                                                             "input": payload})), final("ок")))
            client.respond("запусти команду")
            client = AIClient(llm=ScriptedLLM(tools_reply(call("telegram", {"action": "send", "to": payload,
                                                                            "text": payload})), final("ок")))
            client.respond(f"напиши {payload} в телеграм {payload}")
        self.assertEqual(guard.hits, [])

    def test_guard_itself_detects_writes(self):
        """Проверка самой проверки: прямая запись в защищённый файл ловится всеми путями."""
        guard = WriteGuard(self.allowed)
        for patcher in guard.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        with open(self.allowed, "a", encoding="utf-8"):
            pass
        self.allowed.write_text(self.allowed.read_text(encoding="utf-8"), encoding="utf-8")
        os.close(os.open(self.allowed, os.O_WRONLY))
        self.assertGreaterEqual(len(guard.hits), 3)

    def test_guard_detects_contacts_writes_too(self):
        guard = WriteGuard(self.contacts)
        for patcher in guard.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        with open(self.contacts, "a", encoding="utf-8"):
            pass
        self.contacts.write_text(self.contacts.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertGreaterEqual(len(guard.hits), 2)

    def test_source_references_to_the_files(self):
        """Статически: имя файла разрешений встречается только там, где его читают (и создают по умолчанию)."""
        root = Path(__file__).parent
        writers = {}
        for py in list(root.glob("core/**/*.py")) + list(root.glob("modules/*.py")) + [root / "main.py"]:
            text = py.read_text(encoding="utf-8")
            for token in ("shortcuts_allowed", "trusted_domains"):
                if token in text:
                    writers.setdefault(token, set()).add(str(py.relative_to(root)))
        self.assertEqual(writers["shortcuts_allowed"], {"core/apps/shortcuts.py"})
        # contacts.json: создаётся (пример) и читается только в core/contacts.py; остальные файлы лишь называют его
        # в тексте подсказки и никуда не пишут
        contacts_text = (root / "core/contacts.py").read_text(encoding="utf-8")
        self.assertEqual(contacts_text.count("atomic_write_json("), 1)
        for py in list(root.glob("core/**/*.py")) + list(root.glob("modules/*.py")) + [root / "main.py"]:
            if py.name == "contacts.py":
                continue
            text = py.read_text(encoding="utf-8")
            if "contacts.json" in text:
                for forbidden in ("write_text", "write_bytes", "atomic_write", "os.replace"):
                    self.assertNotIn(forbidden, text, py.name)
        self.assertEqual(writers["trusted_domains"], {"core/safety.py"})
        # В core/safety.py файл доверенных сайтов только читается
        safety_text = (root / "core/safety.py").read_text(encoding="utf-8")
        for forbidden in ("write_text", "write_bytes", "atomic_write", "open(", "os.replace"):
            self.assertNotIn(forbidden, safety_text)
        # В shortcuts.py запись одна: создание файла по умолчанию, если его нет
        text = (root / "core/apps/shortcuts.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("atomic_write_json("), 1)   # единственный вызов (в импорте скобки нет)
        self.assertNotIn("write_text", text)

    def test_no_schema_parameter_can_name_a_file(self):
        for tool in registry.tools().values():
            for param in tool.params:
                self.assertNotIn(param, {"path", "file", "filename", "file_path", "allowlist", "allowed"}, tool.name)


if __name__ == "__main__":
    unittest.main()
