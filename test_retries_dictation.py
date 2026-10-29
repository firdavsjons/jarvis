"""Этап 8.2.4: до двух повторов для невыполненных приложений, названия в «Остальное я не сделал», предупреждение о неточно
распознанном тексте сообщения. Модель, сеть и Telegram подменены."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core import installed_apps
from core.ai import (MAX_APP_RETRIES, AIClient, incomplete_note, known_app, spoken_app_name, unfinished_segments)
from core.apps import spoken_variant
from core.apps import telegram as tg
from core.cache import CommandCache
from core.llm import FallbackClient, LLMClient, LLMResponse
from core.llm.router import TPM_REPLY
from core.phrase_lists import requested_apps, strip_leading
from core.speech_text import confirmation_question
from core.wake import Phrase
from test_telegram import TelegramCase
from test_tools import ScriptedLLM, call, final, tools_reply
import main

INSTALLED = ["Calculator", "Telegram", "WhatsApp", "Safari", "Mail", "Notes", "App Store", "Clock", "YouTube"]
THREE = "открой калькулятор, телеграм и whatsapp"


# --- повторы -----------------------------------------------------------------------------------------------------------
class RetryCase(unittest.TestCase):
    def setUp(self):
        for target in ("core.installed_apps.list_installed_apps",):
            patcher = patch(target, return_value=list(INSTALLED))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("core.ai.subprocess.run")
        self.run = patcher.start()
        self.run.return_value.returncode = 0
        self.addCleanup(patcher.stop)
        self.addCleanup(installed_apps.set_user_aliases, {})

    def respond(self, phrase, *replies, cache=None, llm=None):
        llm = llm or ScriptedLLM(*replies)
        client = AIClient(llm=llm, cache=cache)
        return client, llm, client.respond(phrase)

    @staticmethod
    def notes(llm):
        """Реплики-просьбы повтора из запросов к модели."""
        return [r["messages"][-1]["content"] for r in llm.requests[1:]]


class TestTwoRetries(RetryCase):
    def test_the_limit_is_two(self):
        self.assertEqual(MAX_APP_RETRIES, 2)

    def test_three_apps_one_per_answer_two_retries_everything_done(self):
        client, llm, turn = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})),
                                         tools_reply(call("open_app", {"app": "Telegram"})),
                                         tools_reply(call("open_app", {"app": "WhatsApp"})))
        self.assertEqual(len(llm.requests), 3)                                    # первый запрос + два повтора
        self.assertEqual(turn.speech, "Открыто приложение Calculator, открыто приложение Telegram, "
                                      "открыто приложение WhatsApp.")
        self.assertNotIn("Остальное", turn.speech)
        self.assertTrue(all(r["ok"] for r in turn.results))
        self.assertEqual(self.run.call_count, 3)

    def test_each_retry_names_only_what_is_still_missing(self):
        _, llm, _ = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})),
                                 tools_reply(call("open_app", {"app": "Telegram"})),
                                 tools_reply(call("open_app", {"app": "WhatsApp"})))
        self.assertEqual(self.notes(llm), ["В фразе названы ещё приложения: Телеграм, WhatsApp. Вызови инструменты для них.",
                                           "В фразе названы ещё приложения: WhatsApp. Вызови инструменты для них."])

    def test_the_third_retry_is_never_made(self):
        four = "открой калькулятор, телеграм, whatsapp и сафари"
        _, llm, turn = self.respond(four, tools_reply(call("open_app", {"app": "Calculator"})),
                                    tools_reply(call("open_app", {"app": "Telegram"})),
                                    tools_reply(call("open_app", {"app": "WhatsApp"})),
                                    tools_reply(call("open_app", {"app": "Safari"})))
        self.assertEqual(len(llm.requests), 3)                                    # не больше двух повторов
        self.assertTrue(turn.speech.endswith("Остальное я не сделал: Сафари."), turn.speech)

    def test_no_progress_on_the_first_retry_stops_after_it(self):
        _, llm, turn = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})))   # модель повторяет себя
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: Телеграм, WhatsApp.")

    def test_no_progress_on_the_second_retry_stops_after_it(self):
        _, llm, turn = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})),
                                    tools_reply(call("open_app", {"app": "Telegram"})))     # дальше повторяет Telegram
        self.assertEqual(len(llm.requests), 3)
        self.assertEqual(turn.speech, "Открыто приложение Calculator, открыто приложение Telegram. "
                                      "Остальное я не сделал: WhatsApp.")

    def test_a_retry_that_returns_text_ends_the_retries(self):
        _, llm, turn = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})), final("Готово."))
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: Телеграм, WhatsApp.")

    def test_everything_done_at_once_has_no_retries(self):
        _, llm, turn = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"}, "a"),
                                                       call("open_app", {"app": "Telegram"}, "b"),
                                                       call("open_app", {"app": "WhatsApp"}, "c")))
        self.assertEqual(len(llm.requests), 1)
        self.assertNotIn("Остальное", turn.speech)

    def test_two_of_three_at_once_needs_one_retry(self):
        _, llm, turn = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"}, "a"),
                                                       call("open_app", {"app": "Telegram"}, "b")),
                                    tools_reply(call("open_app", {"app": "WhatsApp"})))
        self.assertEqual(len(llm.requests), 2)
        self.assertNotIn("Остальное", turn.speech)

    def test_a_retry_that_gets_a_limit_answer_ends_with_an_honest_note(self):
        _, llm, turn = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})),
                                    LLMResponse(ok=False, text=TPM_REPLY))
        self.assertEqual(len(llm.requests), 2)                                    # дальше не пробуем
        self.assertIn("Остальное я не сделал: Телеграм, WhatsApp.", turn.speech)
        self.assertEqual(self.run.call_count, 1)

    def test_the_cache_gets_the_whole_phrase_only_when_it_is_finished(self):
        cache = CommandCache()
        self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})), tools_reply(call("open_app", {"app": "Telegram"})),
                     tools_reply(call("open_app", {"app": "WhatsApp"})), cache=cache)
        self.assertEqual([a["args"]["app"] for a in cache.lookup(THREE)], ["Calculator", "Telegram", "WhatsApp"])
        partial = CommandCache()
        self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})), cache=partial)
        self.assertEqual(len(partial), 0)

    def test_phrases_without_a_list_never_retry(self):
        _, llm, _ = self.respond("закрой сафари", tools_reply(call("quit_app", {"app": "Safari"})))
        self.assertEqual(len(llm.requests), 1)

    def test_unknown_apps_are_not_retried(self):
        _, llm, turn = self.respond("открой калькулятор и чатгпт", tools_reply(call("open_app", {"app": "Calculator"})))
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: чатгпт.")


class Provider(LLMClient):
    def __init__(self, name, replies, tpm=None):
        self.name, self.model, self.daily_limit, self.timezone = name, "m", None, "UTC"
        self.tpm_limit, self.max_tokens = tpm, None
        self.replies, self.calls = list(replies), 0

    def chat(self, messages, tools=None):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


class TestLimitAwareRetry(RetryCase):
    def test_no_retry_when_the_provider_is_at_its_limit(self):
        provider = Provider("groq", [tools_reply(call("open_app", {"app": "Calculator"}))], tpm=1000)
        llm = FallbackClient([provider])
        with patch.object(FallbackClient, "can_accept", return_value=False) as can:
            _, _, turn = self.respond(THREE, llm=llm)
        can.assert_called_once()
        self.assertEqual(provider.calls, 1)                                       # второго запроса не было
        self.assertEqual(turn.speech, "Открыто приложение Calculator. Остальное я не сделал: Телеграм, WhatsApp.")

    def test_real_limit_check_blocks_the_retry(self):
        provider = Provider("groq", [tools_reply(call("open_app", {"app": "Calculator"}))], tpm=2000)
        llm = FallbackClient([provider])
        original = llm.chat

        def chat(messages, tools=None, json_messages=None):
            response = original(messages, tools, json_messages)
            llm.usage.record_tokens("groq", 1790)               # окно токенов теперь почти заполнено
            return response
        llm.chat = chat
        _, _, turn = self.respond(THREE, llm=llm)
        self.assertEqual(provider.calls, 1)
        self.assertIn("Остальное я не сделал", turn.speech)

    def test_retry_is_made_when_there_is_room(self):
        provider = Provider("groq", [tools_reply(call("open_app", {"app": "Calculator"})),
                                     tools_reply(call("open_app", {"app": "Telegram"})),
                                     tools_reply(call("open_app", {"app": "WhatsApp"}))], tpm=200000)
        _, _, turn = self.respond(THREE, llm=FallbackClient([provider]))
        self.assertEqual(provider.calls, 3)
        self.assertNotIn("Остальное", turn.speech)

    def test_can_accept(self):
        clock = [1000.0]
        fresh = Provider("groq", [], tpm=1000)
        client = FallbackClient([fresh], clock=lambda: clock[0])
        messages = [{"role": "user", "content": "привет"}]
        self.assertTrue(client.can_accept(messages))
        client.usage.record_tokens("groq", 800)
        self.assertFalse(client.can_accept([{"role": "user", "content": "я" * 3500}]))    # +1000 токенов поверх 800
        self.assertTrue(client.can_accept(messages))                                      # маленький запрос помещается
        clock[0] += 61
        self.assertTrue(client.can_accept([{"role": "user", "content": "я" * 3500}]))     # окно очистилось
        client._down_until["groq"] = clock[0] + 60
        self.assertFalse(client.can_accept(messages))                                     # провайдер в паузе

    def test_can_accept_second_provider_counts(self):
        busy, free = Provider("groq", [], tpm=1000), Provider("gemini", [])
        client = FallbackClient([busy, free], clock=lambda: 1000.0)
        client.usage.record_tokens("groq", 950)
        self.assertTrue(client.can_accept([{"role": "user", "content": "я" * 3500}]))     # Gemini без лимита

    def test_can_accept_writes_nothing_and_does_not_wait(self):
        sleeps = []
        provider = Provider("groq", [], tpm=1000)
        client = FallbackClient([provider], sleep=sleeps.append)
        before = client.usage.tokens_last_minute("groq")
        client.can_accept([{"role": "user", "content": "привет"}])
        self.assertEqual((client.usage.tokens_last_minute("groq"), sleeps, provider.calls), (before, [], 0))

    def test_a_client_without_can_accept_always_retries(self):
        _, llm, _ = self.respond(THREE, tools_reply(call("open_app", {"app": "Calculator"})))
        self.assertEqual(len(llm.requests), 2)


# --- названия в «Остальное я не сделал» --------------------------------------------------------------------------------
class TestNames(RetryCase):
    def test_leading_prepositions_and_conjunctions_are_dropped(self):
        for raw, expected in (("в whatsapp", "WhatsApp"), ("и ватсап", "Ватсап"), ("и телеграм", "Телеграм"),
                              ("на сафари", "Сафари"), ("также app store", "App Store"), ("в app store", "App Store"),
                              ("ещё часы", "Часы"), ("потом whatsapp", "WhatsApp")):
            with self.subTest(raw=raw):
                self.assertEqual(spoken_app_name(raw), expected)

    def test_messenger_variants(self):
        for raw, expected in (("ватсап", "Ватсап"), ("вотсап", "Вотсап"), ("вацап", "Вацап"), ("whatsapp", "WhatsApp"),
                              ("телеграм", "Телеграм"), ("телеграмм", "Телеграмм"), ("telegram", "Telegram"),
                              ("ютуб", "Ютуб"), ("youtube", "YouTube")):
            with self.subTest(raw=raw):
                self.assertEqual(spoken_app_name(raw), expected)

    def test_unknown_stays_as_said_without_the_preposition(self):
        self.assertEqual(spoken_app_name("и чатгпт"), "чатгпт")
        self.assertEqual(spoken_app_name("в абракадабра"), "абракадабра")

    def test_a_preposition_inside_the_name_is_left_alone(self):
        self.assertEqual(strip_leading("кино в москве"), "кино в москве")
        self.assertEqual(strip_leading("в"), "в")                                  # единственное слово не удаляем
        self.assertEqual(strip_leading("и в whatsapp"), "whatsapp")

    def test_requested_apps_drops_the_leading_words(self):
        self.assertEqual(requested_apps("открой калькулятор, телеграм и в whatsapp"), ["калькулятор", "телеграм", "whatsapp"])
        self.assertEqual(requested_apps("закрой календарь, чатгпт и настройки"), ["календарь", "чатгпт", "настройки"])
        self.assertEqual(requested_apps("открой сафари"), [])

    def test_spoken_variants_table(self):
        for word, app in (("ватсап", "WhatsApp"), ("Вотсап", "WhatsApp"), ("вацап", "WhatsApp"), ("whatsapp", "WhatsApp"),
                          ("телеграм", "Telegram"), ("телеграмм", "Telegram"), ("TELEGRAM", "Telegram"), ("ютуб", "YouTube"),
                          ("хром", "Google Chrome")):
            self.assertEqual(spoken_variant(word), app, word)
        self.assertIsNone(spoken_variant("калькулятор"))
        from core.stt.prompt import SPOKEN_EXTRAS
        self.assertIn(("вацап", "WhatsApp"), SPOKEN_EXTRAS)                       # те же добавки, что в подсказке Whisper

    def test_known_app(self):
        self.assertEqual(known_app("ватсап"), "WhatsApp")
        self.assertEqual(known_app("whatsapp"), "WhatsApp")
        self.assertEqual(known_app("телеграмм"), "Telegram")
        self.assertEqual(known_app("ютуб"), "YouTube")
        self.assertIsNone(known_app("чатгпт"))
        with patch("core.installed_apps.list_installed_apps", return_value=["Calculator"]):
            self.assertIsNone(known_app("ватсап"))                                # не установлено — не повторяем

    def result(self, name, app):
        return {"ok": True, "action": {"name": name, "args": {"app": app}}}

    def test_an_opened_whatsapp_closes_every_spoken_variant(self):
        done = [self.result("open_app", "Calculator"), self.result("open_app", "WhatsApp")]
        for word in ("ватсап", "вотсап", "вацап", "whatsapp", "в whatsapp"):
            with self.subTest(word=word):
                self.assertEqual(unfinished_segments(f"открой калькулятор и {word}", done), [])
        self.assertEqual(incomplete_note("открой калькулятор и ватсап", done), "")

    def test_a_telegram_variant_with_two_m(self):
        done = [self.result("open_app", "Calculator"), self.result("open_app", "Telegram")]
        self.assertEqual(unfinished_segments("открой калькулятор и телеграмм", done), [])

    def test_the_live_example(self):
        """Живой лог: после повтора оставалось «В whatsapp.» и «ватсап»."""
        done = [self.result("open_app", "Calculator"), self.result("open_app", "Telegram")]
        self.assertEqual(incomplete_note("открой калькулятор, телеграм и в whatsapp", done), "Остальное я не сделал: WhatsApp.")
        self.assertEqual(incomplete_note("открой калькулятор, телеграм и ватсап", done), "Остальное я не сделал: Ватсап.")
        self.assertEqual(incomplete_note("открой калькулятор, телеграм и ютуб", done), "Остальное я не сделал: Ютуб.")

    def test_end_to_end_note_without_the_preposition(self):
        _, llm, turn = self.respond("открой калькулятор, телеграм и в whatsapp", tools_reply(call("open_app", {"app": "Calculator"})),
                                    tools_reply(call("open_app", {"app": "Telegram"})), final("Готово."))
        self.assertEqual(turn.speech, "Открыто приложение Calculator, открыто приложение Telegram. "
                                      "Остальное я не сделал: WhatsApp.")
        self.assertIn("ещё приложения: WhatsApp.", llm.requests[-1]["messages"][-1]["content"])


# --- предупреждение о неточном тексте ---------------------------------------------------------------------------------
class TestInaccuracySigns(unittest.TestCase):
    def signs(self, text, seconds=None):
        return tg.inaccuracy_signs(text, seconds)

    def test_the_example_from_the_live_log(self):
        self.assertTrue(tg.looks_inaccurate("мама, привет. связи. провека, связи."))
        self.assertEqual(self.signs("мама, привет. связи. провека, связи."), ["повтор слова"])

    def test_normal_text_has_no_signs(self):
        for text in ("привет, как дела", "привет", "Я приду в семь. Ты где?", "Привет! Как дела? Всё хорошо.",
                     "Купи хлеб и молоко", "Буду через десять минут", "Привет... как дела"):
            with self.subTest(text=text):
                self.assertEqual(self.signs(text, 3.0), [])

    def test_repeated_words(self):
        self.assertIn("повтор слова", self.signs("привет привет как дела"))            # подряд
        self.assertIn("повтор слова", self.signs("встреча тогда встреча"))             # через одно слово (от 4 букв)
        self.assertNotIn("повтор слова", self.signs("он пришёл он ушёл"))              # короткое слово через одно — не считаем
        self.assertNotIn("повтор слова", self.signs("привет как дела привет"))         # далеко друг от друга

    def test_repeat_exceptions(self):
        for text in ("да да, давай", "очень очень хорошо", "нет нет нет", "ну ну ладно", "так так так", "вот вот",
                     "ага ага", "угу угу", "давай давай", "пока пока", "стоп стоп", "хватит хватит", "ещё ещё", "еще еще",
                     "уже уже пришёл"):
            with self.subTest(text=text):
                self.assertEqual(self.signs(text), [])
                self.assertFalse(tg.looks_inaccurate(text))

    def test_punctuation_runs_inside_a_sentence(self):
        for text in ("привет,., как дела", "привет?!? как дела", "привет.,; как дела", "привет.... как дела",
                     "привет,,,, как дела"):
            with self.subTest(text=text):
                self.assertIn("знаки препинания подряд", self.signs(text))
        for text in ("привет... как дела", "привет!!! как дела", "привет, как дела...", "привет?!", "привет... "):
            with self.subTest(text=text):
                self.assertEqual(self.signs(text), [])

    def test_short_word_after_a_period(self):
        for text in ("привет. ма", "привет. то пришёл", "мама. ка дела"):
            with self.subTest(text=text):
                self.assertIn("короткое слово после точки", self.signs(text))
        for text in ("Привет. Я иду.", "Привет. Ты где", "Привет. И я тоже", "Привет. Не надо", "Привет. В магазин", "Привет. Да",
                     "Привет. Ок", "Привет. Но почему"):
            with self.subTest(text=text):
                self.assertEqual(self.signs(text), [])

    def test_long_recording_with_few_words(self):
        self.assertIn("длинная запись, мало слов", self.signs("привет", 9.0))
        self.assertIn("длинная запись, мало слов", self.signs("привет как дела хорошо", 8.5))     # 4 слова
        self.assertEqual(self.signs("привет как дела хорошо тут", 9.0), [])                       # 5 слов
        self.assertEqual(self.signs("привет", 8.0), [])                                           # ровно 8 с — ещё нет
        self.assertEqual(self.signs("привет", None), [])                                          # текстовый ввод
        self.assertEqual(self.signs("привет", 3.0), [])

    def test_constants(self):
        self.assertEqual((tg.LONG_RECORDING, tg.SHORT_TEXT_WORDS), (8.0, 5))
        self.assertEqual(tg.INACCURATE_WARNING, "Текст мог распознаться неточно. Проверьте в окне Telegram.")


class TestWarningBeforeTheQuestion(TelegramCase):
    GARBLED = "мама, привет. связи. провека, связи."
    PHRASE = "напиши маме в телеграм мама привет связи провека связи"

    def setUp(self):
        super().setUp()
        self.addCleanup(tg.note_recording, None)
        tg.note_recording(None)

    def ask(self, text, phrase=None):
        client = self.client(tools_reply(self.send_call(text=text)))
        return client.respond(phrase or f"напиши маме в телеграм {text}")

    def test_garbled_text_gets_the_warning_before_the_question(self):
        turn = self.ask(self.GARBLED, self.PHRASE)
        self.assertEqual(len(turn.pending), 1)
        voice = confirmation_question(turn.pending)
        self.assertTrue(voice.startswith("Текст мог распознаться неточно. Проверьте в окне Telegram. Отправить "), voice)
        self.assertTrue(voice.endswith("?"))
        self.assertIn(f"«{self.GARBLED}»", voice)
        self.assertTrue(turn.pending[0]["preview_full"].startswith("Текст мог распознаться неточно."))   # и в окне

    def test_clean_text_has_no_warning(self):
        turn = self.ask("привет, как дела")
        voice = confirmation_question(turn.pending)
        self.assertNotIn("распознаться неточно", voice)
        self.assertTrue(voice.startswith("Отправить "))

    def test_the_warning_does_not_forbid_sending_it_waits_for_yes(self):
        turn = self.ask(self.GARBLED, self.PHRASE)
        self.assertEqual(turn.pending[0]["name"], "telegram")
        self.assertNotIn("enter", self.mac.names())                                # Enter только после «да»
        self.assertIn("paste_keycode", self.mac.names())                           # черновик в чате, как обычно

    def test_confirmed_send_works_after_the_warning(self):
        client = self.client(tools_reply(self.send_call(text=self.GARBLED)))
        turn = client.respond(self.PHRASE)
        results = client.run_actions(turn.pending, phrase=self.PHRASE, confirmed=True)
        self.assertTrue(results[0]["ok"])
        self.assertIn("enter", self.mac.names())

    def test_recording_length_comes_from_the_phrase(self):
        tg.note_recording(9.5)
        turn = self.ask("привет", "напиши маме в телеграм привет")
        self.assertIn("Текст мог распознаться неточно", confirmation_question(turn.pending))
        tg.note_recording(3.0)
        turn = self.ask("привет", "напиши маме в телеграм привет")
        self.assertNotIn("распознаться неточно", confirmation_question(turn.pending))

    def test_long_text_keeps_its_shortened_voice_form_with_the_warning(self):
        text = "связи связи " + "слово " * 60
        turn = self.ask(text.strip(), f"напиши маме в телеграм {text}")
        voice = confirmation_question(turn.pending)
        self.assertTrue(voice.startswith("Текст мог распознаться неточно."))
        self.assertIn("и ещё", voice)
        self.assertIn("полный текст в окне", voice)

    def test_the_log_says_it_without_the_text(self):
        with self.assertLogs("core.apps.telegram", level="INFO") as logs:
            self.ask(self.GARBLED, self.PHRASE)
        joined = "\n".join(logs.output)
        self.assertIn("текст сообщения мог распознаться неточно (признаки: повтор слова)", joined)
        for word in ("провека", "связи", "привет"):
            self.assertNotIn(word, joined)


class TestRecordingReachesTelegram(unittest.TestCase):
    def app(self):
        app = MagicMock()
        app.wake = None
        app.has_pending.return_value = False
        app.pending_is_private.return_value = False
        app.ai.message_dialog_open.return_value = False
        app.ai.bare_confirmation_reply.return_value = None
        app.ai.quick_confirm.return_value = None
        app.mac.execute.return_value = None
        return app

    def setUp(self):
        self.addCleanup(tg.note_recording, None)

    def test_voice_phrase_passes_its_length(self):
        main.VoiceApp.process_command(self.app(), "открой калькулятор", phrase=Phrase(audio_seconds=9.2))
        self.assertEqual(tg._recording_seconds, 9.2)

    def test_text_input_and_missing_info_reset_it(self):
        tg.note_recording(9.2)
        main.VoiceApp.process_command(self.app(), "открой калькулятор", source="text", phrase=Phrase(audio_seconds=9.2))
        self.assertIsNone(tg._recording_seconds)
        tg.note_recording(9.2)
        main.VoiceApp.process_command(self.app(), "открой калькулятор")
        self.assertIsNone(tg._recording_seconds)


if __name__ == "__main__":
    unittest.main()
