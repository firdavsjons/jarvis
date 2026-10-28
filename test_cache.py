import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import installed_apps
from core.ai import AIClient
from core.cache import CLEARED_REPLY, CommandCache, cacheable_actions, is_forget_cache_command, normalize_phrase
from core.llm import LLMResponse
from fixtures_apps import patch_installed
import main

_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()
    installed_apps.set_user_aliases({})


def call(name, args=None):
    import json
    return {"id": f"id_{name}", "name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}


def tools_reply(*calls):
    return LLMResponse(ok=True, tool_calls=list(calls), mode="tools")


def final(text):
    return LLMResponse(ok=True, text=text, mode="tools")


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), 0

    def chat(self, messages, tools=None, json_messages=None):
        self.requests += 1
        return self.replies[min(self.requests - 1, len(self.replies) - 1)]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


OPEN_TELEGRAM = [{"name": "open_app", "args": {"app": "Telegram"}}]


class TestNormalize(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_phrase("Открой, пожалуйста, Telegram!"), "открой telegram")
        self.assertEqual(normalize_phrase("Ну давай, а открой мне   Telegram"), "открой telegram")
        self.assertEqual(normalize_phrase("  ОТКРОЙ Telegram.  "), "открой telegram")

    def test_exact_match_only(self):
        cache = CommandCache()
        self.assertTrue(cache.store("Открой Telegram", OPEN_TELEGRAM))
        self.assertEqual(cache.lookup("открой, пожалуйста, telegram!"), OPEN_TELEGRAM)
        for other in ["открой телеграм", "открой telegram сейчас", "открой telegra", "открой"]:
            self.assertIsNone(cache.lookup(other), other)

    def test_context_phrases_not_stored(self):
        cache = CommandCache()
        for phrase in ["открой его", "запусти снова", "открой это", "ещё громче"]:
            self.assertFalse(cache.store(phrase, OPEN_TELEGRAM), phrase)
        self.assertEqual(len(cache), 0)

    def test_forget_cache_command(self):
        self.assertTrue(is_forget_cache_command("Забудь кэш команд"))
        self.assertTrue(is_forget_cache_command("очисти кеш"))
        self.assertFalse(is_forget_cache_command("забудь разговор"))
        print("✅ Нормализация и точное совпадение: OK")


class TestLimits(unittest.TestCase):
    def test_evicts_least_recently_used_at_500(self):
        clock = Clock()
        cache = CommandCache(clock=clock)
        for i in range(500):
            clock.t += 1
            cache.store(f"открой приложение {i}", OPEN_TELEGRAM)
        clock.t += 1
        cache.mark_used("открой приложение 0")  # самая старая, но только что использована
        clock.t += 1
        cache.store("открой приложение 500", OPEN_TELEGRAM)
        self.assertEqual(len(cache), 500)
        self.assertIsNotNone(cache.lookup("открой приложение 0"))
        self.assertIsNone(cache.lookup("открой приложение 1"))  # вытеснена давно не использованная
        self.assertIsNotNone(cache.lookup("открой приложение 500"))

    def test_ttl_90_days(self):
        clock = Clock()
        cache = CommandCache(clock=clock)
        cache.store("открой telegram", OPEN_TELEGRAM)
        clock.t += 89 * 24 * 3600
        cache.mark_used("открой telegram")
        self.assertIsNotNone(cache.lookup("открой telegram"))
        clock.t += 2 * 24 * 3600  # 91 день с создания — даже если пользовались
        self.assertIsNone(cache.lookup("открой telegram"))
        self.assertEqual(len(cache), 0)

    def test_file_600_and_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Jarvis" / "command_cache.json"
            cache = CommandCache(path)
            cache.store("открой telegram", OPEN_TELEGRAM)
            cache.mark_used("открой telegram")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            again = CommandCache(path)
            self.assertEqual(again.lookup("открой telegram"), OPEN_TELEGRAM)
            self.assertEqual(again._data["открой telegram"]["uses"], 2)
        print("✅ Лимиты кэша: OK")


class TestCacheable(unittest.TestCase):
    def ok(self, name, args=None):
        return {"action": {"name": name, "args": args or {}}, "ok": True}

    def test_only_successful_safe_whitelist(self):
        self.assertEqual(cacheable_actions([self.ok("open_app", {"app": "Telegram"})], "открой telegram"), OPEN_TELEGRAM)
        self.assertIsNotNone(cacheable_actions([self.ok("open_url", {"url": "https://youtube.com"})], "открой youtube"))
        not_cacheable = [
            [self.ok("get_time")],
            [self.ok("trash_file", {"path": "~/Desktop/a.txt"})],
            [self.ok("find_apps", {"query": "tele"})],
            [self.ok("remember", {"text": "кофе"})],
            [self.ok("open_url", {"url": "https://evil.ru"})],  # CONFIRM — не доверенный
            [self.ok("quit_app", {"app": "Terminal"})],          # CONFIRM — CAREFUL_APPS
            [{"action": {"name": "open_app", "args": {"app": "Spotify"}}, "ok": False}],
            [self.ok("open_app", {"app": "Safari"}), self.ok("get_time")],
            [self.ok("set_volume", {"level": i}) for i in range(4)],  # больше 3 действий
            [],
        ]
        for results in not_cacheable:
            self.assertIsNone(cacheable_actions(results, "открой telegram и safari"), results)
        print("✅ Что кэшируется: OK")

    def test_app_must_be_named_in_phrase(self):
        """Модель выбрала приложение, которого во фразе нет («главный экран» -> Preview) — не кэшируем."""
        preview = [self.ok("open_app", {"app": "Notes"})]
        self.assertIsNone(cacheable_actions(preview, "главный экран"))
        self.assertIsNone(cacheable_actions(preview, "открой главный экран"))
        self.assertIsNone(cacheable_actions([self.ok("quit_app", {"app": "Safari"})], "закрой всё лишнее"))
        # Название, русское название (с падежом), слово из названия, фраза с «пожалуйста»
        named = [("Telegram", "Открой Telegram"), ("Telegram", "открой телеграм"), ("Calculator", "запусти калькулятор"),
                 ("Calculator", "закрой калькулятора"), ("Google Chrome", "открой chrome"),
                 ("Google Chrome", "открой google chrome"), ("Google Chrome", "открой хром"),
                 ("Safari", "открой, пожалуйста, safari!"), ("Safari", "сафари")]
        for app, phrase in named:
            with self.subTest(app=app, phrase=phrase):
                self.assertIsNotNone(cacheable_actions([self.ok("open_app", {"app": app})], phrase))

    def test_ambiguous_app_word_not_enough(self):
        # «code» есть и в Visual Studio Code, и у других — одного слова мало; «microsoft» — Word и Excel
        self.assertIsNone(cacheable_actions([self.ok("open_app", {"app": "Microsoft Word"})], "открой microsoft"))
        self.assertIsNotNone(cacheable_actions([self.ok("open_app", {"app": "Microsoft Word"})], "открой word"))

    def test_user_alias_in_phrase(self):
        installed_apps.set_user_aliases({"мой браузер": "Google Chrome"})
        self.addCleanup(installed_apps.set_user_aliases, {})
        results = [self.ok("open_app", {"app": "Google Chrome"})]
        self.assertIsNotNone(cacheable_actions(results, "открой мой браузер"))
        self.assertIsNone(cacheable_actions(results, "открой интернет"))

    def test_several_apps_each_must_be_named(self):
        results = [self.ok("open_app", {"app": "Safari"}), self.ok("open_app", {"app": "Telegram"})]
        self.assertIsNotNone(cacheable_actions(results, "открой сафари и телеграм"))
        self.assertIsNone(cacheable_actions(results, "открой сафари и главный экран"))

    def test_volume_template(self):
        vol = [self.ok("set_volume", {"level": 50})]
        for phrase in ["громкость 50", "поставь громкость на 50", "сделай звук на 50 процентов",
                       "поставь, пожалуйста, громкость 50"]:
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(cacheable_actions(vol, phrase))
        for phrase in ["сделай поприятнее", "громкость 60", "громкость 50 и открой safari", "включи музыку",
                       "громкость на половину"]:
            with self.subTest(phrase=phrase):
                self.assertIsNone(cacheable_actions(vol, phrase))
        # громкость вместе с другим действием — без шаблона на всю фразу не кэшируем
        both = [self.ok("open_app", {"app": "Safari"}), self.ok("set_volume", {"level": 50})]
        self.assertIsNone(cacheable_actions(both, "открой safari и громкость 50"))

    def test_url_template(self):
        yt = [self.ok("open_url", {"url": "https://www.youtube.com/watch?v=x"})]
        for phrase in ["открой youtube", "открой ютуб", "зайди на youtube.com", "перейди на сайт youtube"]:
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(cacheable_actions(yt, phrase))
        for phrase in ["покажи что-нибудь весёлое", "открой музыку", "найди котиков на youtube"]:
            with self.subTest(phrase=phrase):
                self.assertIsNone(cacheable_actions(yt, phrase))

    def test_other_tools_never_cached(self):
        for result in [self.ok("display", {"action": "dark_mode_on", "value": ""}),
                       self.ok("media", {"action": "pause", "app": "Music"}),
                       self.ok("system_info", {"what": "battery"})]:
            self.assertIsNone(cacheable_actions([result], "включи тёмную тему"))


@patch('core.ai.subprocess.run')
class TestCacheInAIClient(unittest.TestCase):
    def client(self, *replies):
        llm = ScriptedLLM(*replies)
        return AIClient(llm=llm, cache=CommandCache()), llm

    def test_hit_skips_model(self, mock_run):
        mock_run.return_value.returncode = 0
        client, llm = self.client(tools_reply(call("open_app", {"app": "Telegram"})))

        self.assertEqual(client.respond("Открой Telegram").speech, "Открыто приложение Telegram.")
        self.assertEqual(llm.requests, 1)  # простое действие — без второго запроса (4.4)
        turn = client.respond("открой, пожалуйста, telegram!")
        self.assertEqual(llm.requests, 1)  # из кэша — без модели
        self.assertEqual(turn.speech, "Открыто приложение Telegram.")
        self.assertEqual(mock_run.call_count, 2)  # действие выполнено обычным путём
        self.assertEqual(client.history.messages()[-1]["content"],
                         "Открыто приложение Telegram. [open_app Telegram: успешно]")

    def test_confirm_and_errors_not_cached(self, mock_run):
        mock_run.return_value.returncode = 0
        client, _ = self.client(tools_reply(call("quit_app", {"app": "Terminal"})))
        client.respond("закрой терминал")
        self.assertEqual(len(client.cache), 0)

        client, _ = self.client(tools_reply(call("open_app", {"app": "Spotify"})), final("Не получилось."))
        client.respond("открой спотифай")
        self.assertEqual(len(client.cache), 0)

        client, _ = self.client(tools_reply(call("get_time")), final("Сейчас вечер."))
        client.respond("который час")
        self.assertEqual(len(client.cache), 0)

    def test_wrong_app_choice_not_stored(self, mock_run):
        mock_run.return_value.returncode = 0
        client, llm = self.client(tools_reply(call("open_app", {"app": "Notes"})))
        self.assertEqual(client.respond("главный экран").speech, "Открыто приложение Notes.")
        self.assertEqual(len(client.cache), 0)
        client.respond("главный экран")
        self.assertEqual(llm.requests, 2)   # во второй раз снова спрашиваем модель, а не повторяем ошибку

    def test_existing_cache_is_not_touched(self, mock_run):
        client, _ = self.client(final("Привет."))
        client.cache.store("главный экран", [{"name": "open_app", "args": {"app": "Preview"}}])
        client.respond("как дела у тебя")
        self.assertEqual(len(client.cache), 1)   # старые записи сам не чищу: только «забудь кэш команд»

    def test_failed_hit_removes_entry(self, mock_run):
        client, llm = self.client()
        client.cache.store("открой telegram", OPEN_TELEGRAM)
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "boom"
        turn = client.respond("открой telegram")
        self.assertEqual(turn.speech, "Не получилось: команда завершилась с ошибкой.")
        self.assertEqual(len(client.cache), 0)
        self.assertEqual(llm.requests, 0)

    def test_alias_change_clears_cache(self, mock_run):
        client, _ = self.client()
        client.cache.store("открой telegram", OPEN_TELEGRAM)
        client.run_actions([{"name": "remember_alias", "args": {"spoken": "мой браузер", "app": "Google Chrome"}}],
                           phrase="запомни: мой браузер — это Chrome")
        self.assertEqual(len(client.cache), 0)
        installed_apps.set_user_aliases({})

    def test_forget_cache_phrase_in_main(self, mock_run):
        client, llm = self.client()
        client.cache.store("открой telegram", OPEN_TELEGRAM)
        app = MagicMock()
        app.ai = client
        app.has_pending.return_value = False
        main.VoiceApp.process_command(app, "Забудь кэш команд")
        self.assertEqual(len(client.cache), 0)
        app.engine.speak.assert_called_once_with(CLEARED_REPLY)
        self.assertEqual(llm.requests, 0)
        print("✅ Кэш в AIClient: OK")


if __name__ == '__main__':
    unittest.main()
