"""Этап 7.1: Telegram. Контакты и разрешение имён, проверки текста, двухфазная отправка, буфер обмена, журнал,
быстрые правила. subprocess замокан (tests_base): Telegram не запускается, клавиши не нажимаются."""
import json
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки

import main
from core import contacts as contacts_store
from core import journal, safety
from core.ai import GENERIC_REFUSAL_REPLY, AIClient, describe_action
from core.apps import registry
from core.apps import telegram as tg
from core.cache import CACHEABLE_ACTIONS, CommandCache, cacheable_actions
from core.errors import ActionError
from core.speech_text import confirmation_question
from fixtures_apps import patch_installed
from fixtures_run import SAMPLE_CONTACTS, completed, isolated_apps
from modules.mac_control import MacControl
from test_tools import ScriptedLLM, call, final, tools_reply

CLIENT = "ru.keepcoder.Telegram"
TEXT_INFO = "«class utf8», 12, «class ut16», 24, string, 12, Unicode text, 24"


class FakeMac:
    """subprocess.run для Telegram: узнаёт команды и записывает порядок вызовов в events."""

    def __init__(self, clipboard_info=TEXT_INFO, clipboard="старый буфер", front=CLIENT, window="ok",
                 fail_on=None):
        self.events, self.pasteboard, self.lenient = [], clipboard, False
        self.clipboard_info, self.front, self.window, self.fail_on = clipboard_info, front, window, fail_on or {}

    def names(self):
        return [e[0] for e in self.events]

    def __call__(self, args, **kwargs):
        cmd = args[0]
        if cmd == "open":
            self.events.append(("open", args[-1]))
            return self.reply("open")
        if cmd == "pbpaste":
            self.events.append(("pbpaste",))
            return self.reply("pbpaste", self.pasteboard)
        if cmd == "pbcopy":
            self.events.append(("pbcopy", kwargs.get("input")))
            self.pasteboard = kwargs.get("input")
            return self.reply("pbcopy")
        script = " ".join(a for a in args if a != "-e")
        for marker, name, out in (("clipboard info", "clipboard_info", self.clipboard_info),
                                  ("first process whose frontmost", "front", self.front),
                                  ("repeat tenths times", "wait", self.window),
                                  ('keystroke "v"', "paste", ""), ("key code 36", "enter", "")):
            if marker in script:
                self.events.append((name,))
                return self.reply(name, out)
        if self.lenient:                    # не про Telegram (календарь, заметки): запоминаем и отвечаем пусто
            self.events.append(("other",))
            return completed("")
        raise AssertionError(f"неожиданная команда {args}")

    def reply(self, name, out=""):
        if name in self.fail_on:
            return completed("", 1, self.fail_on[name])
        return completed(out)


class TelegramCase(unittest.TestCase):
    def setUp(self):
        self.sched, _ = isolated_apps(self, {})
        installed = patch_installed()
        installed.start()
        self.addCleanup(installed.stop)
        self.mac = FakeMac()
        for target, kwargs in (("core.apps.common.subprocess.run", {"side_effect": self.dispatch}),
                               ("core.apps.telegram.detect_client", {"return_value": CLIENT}),
                               ("core.apps.telegram.time.sleep", {})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        tg._state = None
        self.addCleanup(setattr, tg, "_state", None)

    def dispatch(self, args, **kwargs):
        return self.mac(args, **kwargs)

    def client(self, *replies):
        return AIClient(llm=ScriptedLLM(*replies))

    def send_call(self, to="мама", text="привет", call_id="s"):
        return call("telegram", {"action": "send", "to": to, "text": text}, call_id)


# --- Контакты ---

class TestContacts(TelegramCase):
    def book(self):
        return contacts_store.load_contacts()

    def test_exact_case_insensitive_and_cases(self):
        book = self.book()
        for spoken in ("мама", "Мама", "МАМА", "маме", "мамой", "маму", "мамы"):
            with self.subTest(spoken=spoken):
                self.assertEqual([c.key for c in contacts_store.find(spoken, book)], ["мама"])
        for spoken in ("Анна", "Анне", "Анной", "Анну"):
            self.assertEqual([c.key for c in contacts_store.find(spoken, book)], ["Анна"], spoken)

    def test_short_names_only_exact(self):
        book = self.book()
        self.assertEqual([c.key for c in contacts_store.find("Аня", book)], ["Аня"])
        self.assertEqual(contacts_store.find("Ане", book), [])      # короче 4 букв — падежи не угадываем

    def test_fuzzy_from_085(self):
        book = self.book()
        self.assertEqual([c.key for c in contacts_store.find("мамма", book)], ["мама"])     # 0,89
        self.assertEqual(contacts_store.find("мамочка", book), [])
        self.assertEqual(contacts_store.find("майя", book), [])

    def test_ambiguous_returns_all_candidates(self):
        self.assertEqual(sorted(c.key for c in contacts_store.find("Иван", self.book())), ["Иван Иванов", "Иван Петров"])
        self.assertEqual([c.key for c in contacts_store.find("Иван Петров", self.book())], ["Иван Петров"])

    def test_unknown_name_message(self):
        with self.assertRaises(ActionError) as ctx:
            tg.resolve_contact("Незнакомец", "напиши незнакомцу привет")
        self.assertEqual(ctx.exception.reply, "Не знаю такого контакта. Добавьте его в contacts.json, "
                                              "инструкция в docs/telegram.md.")

    def test_ambiguous_asks_who(self):
        with self.assertRaises(ActionError) as ctx:
            tg.resolve_contact("Иван", "напиши ивану привет")
        self.assertEqual(ctx.exception.reply, "Кому именно: Иван Иванов, Иван Петров?")

    def test_recipient_must_be_in_phrase(self):
        with self.assertRaises(ActionError) as ctx:
            tg.resolve_contact("мама", "напиши Анне привет")
        self.assertEqual(ctx.exception.reply, "Этого получателя нет в вашей фразе.")
        self.assertEqual(tg.resolve_contact("мама", "напиши маме привет").key, "мама")
        self.assertEqual(tg.resolve_contact("Мама", "напиши мамой привет").key, "мама")

    def test_unknown_checked_before_phrase(self):
        with self.assertRaises(ActionError) as ctx:
            tg.resolve_contact("Незнакомец", "напиши маме привет")
        self.assertIn("Не знаю такого контакта", ctx.exception.reply)

    def test_file_example_created_once_private_and_ignored(self):
        path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / "data" / "contacts.json"
        self.assertEqual(contacts_store.load_contacts(path), {})          # примеры «_…» контактами не считаются
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertTrue(all(key.startswith("_") for key in json.loads(path.read_text(encoding="utf-8"))))
        before = path.stat().st_mtime_ns
        contacts_store.load_contacts(path)
        self.assertEqual(path.stat().st_mtime_ns, before)               # второй раз не перезаписывается

    def test_file_validation(self):
        path = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT)) / "contacts.json"
        path.write_text(json.dumps({
            "Саша": {"username": "@sasha_user", "title": ""},
            "Петя": {"phone": "+7 (999) 123-45-67", "title": "Пётр"},
            "Плохой": {"username": "x y z"}, "Пустой": {"title": "ничего"}, "Строка": "не словарь",
            "Телефон": {"phone": "12"}, "_скрытый": {"username": "hidden_user"}}), encoding="utf-8")
        book = contacts_store.load_contacts(path)
        self.assertEqual(set(book), {"саша", "петя"})
        self.assertEqual(book["саша"].username, "sasha_user")
        self.assertEqual(book["саша"].title, "Саша")
        self.assertEqual(book["петя"].phone, "79991234567")
        self.assertEqual(tg.chat_url(book["саша"]), "tg://resolve?domain=sasha_user")
        self.assertEqual(tg.chat_url(book["петя"]), "tg://resolve?phone=79991234567")
        for broken in ("{не json", "[]", '"x"'):
            path.write_text(broken, encoding="utf-8")
            self.assertEqual(contacts_store.load_contacts(path), {})

    def test_repr_has_no_phone(self):
        self.assertNotIn("999", repr(self.book()["иван иванов"]))


# --- Проверки текста ---

class TestTextChecks(TelegramCase):
    def check(self, text, phrase):
        return tg.check_text(text, phrase)

    def test_text_from_phrase_ok(self):
        self.assertEqual(self.check("привет, как дела", "напиши маме в телеграм привет, как дела"), "привет, как дела")

    def test_text_not_from_phrase_refused(self):
        with self.assertRaises(ActionError) as ctx:
            self.check("я задержусь на работе", "напиши маме в телеграм привет")
        self.assertEqual(ctx.exception.reply, "Не уверен, что вы просили отправить именно это.")

    def test_sixty_percent_boundary(self):
        phrase = "напиши маме одно два три"
        self.assertEqual(self.check("одно два три ещё чужое", phrase), "одно два три ещё чужое")     # 3 из 5 = 0,6
        with self.assertRaises(ActionError):
            self.check("одно два чужое ещё другое", phrase)                                          # 2 из 5

    def test_cases_by_stem(self):
        self.assertTrue(self.check("куплю молоко", "напиши маме что куплю молока"))

    def test_text_without_words_refused(self):
        with self.assertRaises(ActionError):
            self.check("👍", "напиши маме в телеграм 👍")

    def test_secrets_refused(self):
        for text in ("мой пароль 12345", "пароль: qwerty", "карта 4111 1111 1111 1111", "cvv 123",
                     "токен abcdef", "паспорт 4510 123456"):
            with self.subTest(text=text), self.assertRaises(ActionError) as ctx:
                self.check(text, f"напиши маме {text}")      # даже слово в слово из фразы
            self.assertEqual(ctx.exception.reply, "Такое я отправлять не буду.")

    def test_length_and_control_chars(self):
        with self.assertRaises(ActionError):
            self.check("слово " * 100, "слово " * 100)
        with self.assertRaises(ActionError):
            self.check("привет\x00", "привет")
        with self.assertRaises(ActionError):
            self.check("   ", "напиши маме")


# --- Уровни ---

class TestLevels(TelegramCase):
    def level(self, **args):
        return safety.classify({"name": "telegram", "args": args})[0]

    def test_levels(self):
        self.assertEqual(self.level(action="send", to="мама", text="привет"), safety.CONFIRM)
        self.assertEqual(self.level(action="open_chat", to="мама", text=""), safety.SAFE)
        for bad in ("delete", "", None, "SEND", "send; rm"):
            self.assertEqual(self.level(action=bad, to="мама", text=""), safety.BLOCKED, bad)

    def test_send_never_runs_without_confirmation(self):
        [result] = self.client().run_actions([{"name": "telegram", "args": {"action": "send", "to": "мама",
                                                                            "text": "привет"}}], phrase="напиши маме привет")
        self.assertFalse(result["ok"])
        self.assertEqual(result["short"], "нужно подтверждение")
        self.assertEqual(self.mac.events, [])

    def test_send_is_destructive_and_not_cacheable(self):
        tool = registry.get("telegram")
        self.assertTrue(tool.is_destructive({"action": "send"}))
        self.assertNotIn("telegram", CACHEABLE_ACTIONS)
        self.assertIsNone(cacheable_actions([{"action": {"name": "telegram", "args": {"action": "open_chat", "to": "мама",
                                                                                      "text": ""}}, "ok": True}],
                                            "открой чат с мамой"))

    def test_schema_all_parameters_required(self):
        function = tool_schema()["function"]
        self.assertEqual(function["parameters"]["required"], ["action", "to", "text"])
        self.assertEqual(function["parameters"]["properties"]["action"]["enum"], ["open_chat", "send"])


def tool_schema():
    from core.tools import TOOLS
    return next(t for t in TOOLS if t["function"]["name"] == "telegram")


# --- Двухфазная отправка ---

class TestTwoPhaseSend(TelegramCase):
    PHRASE = "напиши маме в телеграм привет"

    def ask(self, text="привет", phrase=None, client=None):
        client = client or self.client(tools_reply(self.send_call(text=text)))
        return client, client.respond(phrase or f"напиши маме в телеграм {text}")

    def test_phase1_draft_before_question_and_enter_only_after_yes(self):
        client, turn = self.ask()
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(turn.pending[0]["preview"], "отправить Мама сообщение: «привет»")
        self.assertEqual(confirmation_question(turn.pending), "Отправить Мама сообщение: «привет»?")
        names = self.mac.names()
        self.assertEqual(names, ["clipboard_info", "pbpaste", "open", "wait", "pbcopy", "paste", "pbcopy"])
        self.assertNotIn("enter", names)                                     # до «да» Enter не нажат
        self.assertEqual(self.mac.events[2], ("open", "tg://resolve?domain=mama_user"))
        self.assertEqual(self.mac.events[4], ("pbcopy", "привет"))           # черновик через буфер обмена
        before = len(self.mac.events)
        results = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertTrue(results[0]["ok"], results[0])
        self.assertEqual(results[0]["speech"], "Нажал отправку в чате с Мама.")
        phase2 = self.mac.names()[before:]
        self.assertEqual(phase2, ["open", "wait", "front", "enter"])         # повторное открытие → фокус → Enter
        self.assertEqual(self.mac.names().count("enter"), 1)
        self.assertEqual(self.mac.names().index("enter"), len(self.mac.names()) - 1)

    def test_clipboard_restored_with_old_content(self):
        self.mac.pasteboard = "важный буфер"
        self.ask()
        self.assertEqual(self.mac.pasteboard, "важный буфер")
        self.assertEqual(self.mac.events[-1], ("pbcopy", "важный буфер"))

    def test_empty_clipboard_restored_as_empty(self):
        self.mac.clipboard_info = ""
        self.mac.pasteboard = ""
        self.ask()
        self.assertEqual(self.mac.events[0], ("clipboard_info",))
        self.assertNotIn("pbpaste", self.mac.names())
        self.assertEqual(self.mac.events[-1], ("pbcopy", ""))

    def test_clipboard_restored_when_paste_fails(self):
        self.mac.fail_on = {"paste": "osascript is not allowed to send keystrokes. (1002)"}
        client, turn = self.ask()
        self.assertEqual(turn.pending, [])
        self.assertIn("Универсальный доступ", turn.speech)
        self.assertEqual(self.mac.pasteboard, "старый буфер")                # вернули, хотя вставка упала
        self.assertEqual(self.mac.events[-1], ("pbcopy", "старый буфер"))
        self.assertNotIn("enter", self.mac.names())
        self.assertIsNone(tg._state)

    def test_clipboard_restored_when_chat_does_not_open(self):
        self.mac.window = "timeout"
        client, turn = self.ask()
        self.assertEqual(turn.pending, [])
        self.assertIn("Telegram не открылся", turn.speech)
        self.assertEqual(self.mac.pasteboard, "старый буфер")
        self.assertNotIn("paste", self.mac.names())

    def test_clipboard_restored_when_open_fails(self):
        self.mac.fail_on = {"open": "Unable to find application"}
        client, turn = self.ask()
        self.assertEqual(self.mac.pasteboard, "старый буфер")
        self.assertEqual(turn.pending, [])

    def test_non_text_clipboard_is_not_touched(self):
        self.mac.clipboard_info = "«class PNGf», 123456, «class TIFF», 234567"
        client, turn = self.ask()
        self.assertEqual(turn.pending, [])
        self.assertEqual(turn.speech, "В буфере обмена не текст, я его не сохраню. Скопируйте текст или очистите буфер.")
        self.assertEqual(self.mac.names(), ["clipboard_info"])               # ничего не открыто и не вставлено

    def test_file_in_clipboard_refused(self):
        self.mac.clipboard_info = "«class furl», 80, «class utf8», 10"
        client, turn = self.ask()
        self.assertEqual(self.mac.names(), ["clipboard_info"])

    def test_permission_phrases_name_the_section(self):
        cases = {"(1002)": "Универсальный доступ", "(-1719)": "Универсальный доступ", "(-25211)": "Универсальный доступ",
                 "(-1743)": "Автоматизация, System Events"}
        for code, section in cases.items():
            with self.subTest(code=code):
                self.mac = FakeMac(fail_on={"paste": f"Not authorized {code}"})
                tg._state = None
                client, turn = self.ask()
                self.assertIn(section, turn.speech)
                self.assertIn("для приложения, из которого запущен Jarvis", turn.speech)
                self.assertEqual(self.mac.pasteboard, "старый буфер")

    def test_no_means_no_keys_and_draft_stays(self):
        client, turn = self.ask()
        before = list(self.mac.events)
        notes = client.reject_actions(turn.pending, self.PHRASE, "отменено пользователем", confirmed=False)
        self.assertEqual(notes, ["Отменил, черновик остался в чате, можете стереть его вручную."])
        self.assertEqual(self.mac.events, before)                           # ни одной клавиши, ни одной команды
        self.assertNotIn("enter", self.mac.names())
        self.assertIsNone(tg._state)                                        # снимок очищен
        [late] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertFalse(late["ok"])                                        # после «нет» отправить уже нельзя
        self.assertEqual(self.mac.events, before)

    def test_timeout_same_as_no(self):
        client, turn = self.ask()
        before = list(self.mac.events)
        notes = client.reject_actions(turn.pending, self.PHRASE, "нет ответа за 20 с", confirmed=False)
        self.assertEqual(notes, [tg.DISCARD_NOTE])
        self.assertEqual(self.mac.events, before)

    def test_reject_for_other_reasons_is_silent(self):
        client, turn = self.ask()
        self.assertEqual(client.reject_actions(turn.pending, self.PHRASE, "в ответе есть опасное действие"), [])

    def test_stale_draft_refused(self):
        client, turn = self.ask()
        with patch("core.apps.telegram.time.monotonic", return_value=tg.time.monotonic() + tg.STATE_TTL + 5):
            [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["short"], "черновик устарел, повторите команду")
        self.assertNotIn("enter", self.mac.names())

    def test_changed_text_or_recipient_refused(self):
        client, turn = self.ask()
        tampered = [{**turn.pending[0], "args": {"action": "send", "to": "мама", "text": "совсем другое"}}]
        [result] = client.run_actions(tampered, phrase="напиши маме в телеграм совсем другое", confirmed=True)
        self.assertFalse(result["ok"])
        self.assertNotIn("enter", self.mac.names())
        client, turn = self.ask()
        other = [{**turn.pending[0], "args": {"action": "send", "to": "Анна", "text": "привет"}}]
        [result] = client.run_actions(other, phrase="напиши Анне привет", confirmed=True)
        self.assertFalse(result["ok"])
        self.assertNotIn("enter", self.mac.names())

    def test_draft_is_single_use_no_second_send(self):
        client, turn = self.ask()
        [first] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        [second] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(self.mac.names().count("enter"), 1)

    def test_telegram_not_in_focus_before_enter(self):
        client, turn = self.ask()
        self.mac.front = "com.apple.Safari"
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["short"], "Telegram не в фокусе")
        self.assertNotIn("enter", self.mac.names())
        self.assertEqual(tg_summary([result]), "Не получилось: Telegram не в фокусе.")
        self.assertEqual(self.mac.names()[-2:], ["wait", "front"])           # после повторного открытия ссылки

    def test_window_did_not_come_forward_before_enter(self):
        client, turn = self.ask()
        self.mac.window = "timeout"
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertEqual(result["short"], "Telegram не в фокусе")
        self.assertNotIn("enter", self.mac.names())

    def test_enter_failure_is_reported_without_retry(self):
        client, turn = self.ask()
        self.mac.fail_on = {"enter": "not allowed to send keystrokes (1002)"}
        [result] = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("Универсальный доступ", result["reply"])
        self.assertEqual(self.mac.names().count("enter"), 1)                # без повторов

    def test_second_send_in_one_answer_refused(self):
        client = self.client(tools_reply(self.send_call(text="привет", call_id="a"),
                                         self.send_call(to="Анна", text="привет", call_id="b")))
        turn = client.respond("напиши маме и Анне в телеграм привет")
        self.assertEqual(len(turn.pending), 1)
        self.assertEqual(self.mac.names().count("paste"), 1)
        self.assertIn("только одно", turn.speech)

    def test_refusals_happen_before_anything_is_touched(self):
        cases = [("Незнакомец", "привет", "напиши незнакомцу в телеграм привет", "Не знаю такого контакта"),
                 ("Иван", "привет", "напиши ивану в телеграм привет", "Кому именно: Иван Иванов, Иван Петров?"),
                 ("мама", "привет", "напиши Анне в телеграм привет", "Этого получателя нет в вашей фразе"),
                 ("мама", "я задержусь на работе", "напиши маме в телеграм привет", "Не уверен, что вы просили"),
                 ("мама", "мой пароль 12345", "напиши маме в телеграм мой пароль 12345", "Такое я отправлять не буду.")]
        for to, text, phrase, expected in cases:
            with self.subTest(phrase=phrase):
                client = self.client(tools_reply(self.send_call(to=to, text=text)))
                turn = client.respond(phrase)
                self.assertEqual(turn.pending, [])
                self.assertIn(expected, turn.speech)
                self.assertEqual(self.mac.events, [])                       # даже буфер не читали

    def test_long_text_voice_head_window_full(self):
        text = " ".join(f"слово{i}" for i in range(60))
        self.assertGreater(len(text), 200)
        client, turn = self.ask(text=text, phrase=f"напиши маме в телеграм {text}")
        question = confirmation_question(turn.pending)
        self.assertIn("и ещё", question)
        self.assertIn("полный текст в окне", question)
        self.assertNotIn("слово59", question)
        self.assertIn("слово59", describe_action(turn.pending[0]))
        self.assertTrue(describe_action(turn.pending[0]).endswith("»"))

    def test_text_up_to_200_voiced_in_full(self):
        text = "привет " * 20
        text = text.strip()
        self.assertLessEqual(len(text), 200)
        client, turn = self.ask(text=text, phrase=f"напиши маме в телеграм {text}")
        self.assertIn(text, confirmation_question(turn.pending))
        self.assertNotIn("и ещё", confirmation_question(turn.pending))

    def test_phone_contact_uses_phone_link(self):
        client = self.client(tools_reply(self.send_call(to="Иван Иванов", text="привет")))
        client.respond("напиши Ивану Иванову в телеграм привет")
        self.assertEqual(self.mac.events[2], ("open", "tg://resolve?phone=79991234567"))

    def test_open_chat_is_safe_and_runs_at_once(self):
        client = self.client(tools_reply(call("telegram", {"action": "open_chat", "to": "мама", "text": ""})))
        turn = client.respond("открой чат с мамой")
        self.assertEqual(turn.speech, "Открыл чат с Мама.")
        self.assertEqual(self.mac.names(), ["open", "wait"])
        self.assertEqual(turn.pending, [])


def tg_summary(results):
    from core.ai import summarize_results
    return summarize_results(results)[1]


class TestClientDetection(unittest.TestCase):
    def make(self, *pairs):
        tmp = Path(tempfile.mkdtemp(dir=tests_base.TEMP_ROOT))
        paths = []
        for name, bundle in pairs:
            (tmp / name / "Contents").mkdir(parents=True)
            (tmp / name / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle}))
            paths.append(tmp / name)
        return paths

    def test_macos_client_preferred_then_desktop(self):
        both = self.make(("Telegram.app", "ru.keepcoder.Telegram"), ("Telegram Desktop.app", "org.telegram.desktop"))
        self.assertEqual(tg.detect_client(both), "ru.keepcoder.Telegram")
        self.assertEqual(tg.detect_client(both[1:]), "org.telegram.desktop")

    def test_not_installed_or_foreign_bundle(self):
        with self.assertRaises(ActionError) as ctx:
            tg.detect_client(self.make(("Other.app", "com.example.other")) + [Path("/нет/такого.app")])
        self.assertEqual(ctx.exception.short, "Telegram не установлен")


if __name__ == "__main__":
    unittest.main()
