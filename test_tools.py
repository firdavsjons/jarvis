import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core import prompts, safety
from core.ai import (BLOCKED_REPLY, PROMISE_FAIL_REPLY, PROMISE_RETRY_NOTE, TOO_MANY_ROUNDS_REPLY, AIClient,
                     get_time)
from core.history import FORGET_REPLY, ConversationHistory, is_forget_command
from core.llm import FallbackClient, LLMClient, LLMResponse
from core.llm.providers import _tool_call_dict, build_providers, load_config
from core.tools import HONESTY_NOTE, TOOLS, call_to_action
from fixtures_apps import patch_installed
import main

_installed = patch_installed()


def setUpModule():
    _installed.start()


def tearDownModule():
    _installed.stop()


def call(name, args=None, call_id=None, raw=None):
    return {"id": call_id or f"id_{name}", "name": name,
            "arguments": raw if raw is not None else json.dumps(args or {}, ensure_ascii=False)}


def tools_reply(*calls, text=""):
    return LLMResponse(ok=True, text=text, tool_calls=list(calls), provider="gemini", mode="tools")


def final(text):
    return LLMResponse(ok=True, text=text, provider="gemini", mode="tools")


class ScriptedLLM:
    """Мок LLM: отдаёт ответы по очереди и запоминает, что ему прислали."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, messages, tools=None, json_messages=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools,
                              "json_messages": json_messages})
        reply = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


def tool_messages(request):
    return [json.loads(m["content"]) | {"tool_call_id": m["tool_call_id"]}
            for m in request["messages"] if m["role"] == "tool"]


class RunMock(unittest.TestCase):
    def setUp(self):
        patcher = patch('core.ai.subprocess.run')
        self.mock_run = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_run.return_value.returncode = 0
        self.mock_run.return_value.stderr = ""


class TestToolLoop(RunMock):
    def test_safe_result_returned_to_model(self):
        llm = ScriptedLLM(tools_reply(call("get_time")), final("Сейчас пять минут шестого."))
        turn = AIClient(llm=llm).respond("Который час?")

        self.assertEqual(turn.speech, "Сейчас пять минут шестого.")
        self.assertEqual(len(llm.requests), 2)
        self.assertIsNotNone(llm.requests[0]["tools"])
        second = llm.requests[1]["messages"]
        assistant = [m for m in second if m["role"] == "assistant" and m.get("tool_calls")][0]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "get_time")
        [result] = tool_messages(llm.requests[1])
        self.assertEqual((result["ok"], result["tool_call_id"]), (True, "id_get_time"))
        self.assertTrue(result["result"].startswith("Сейчас "))
        self.assertIsNone(llm.requests[1]["json_messages"])  # 2-й круг — только модели с инструментами
        self.mock_run.assert_not_called()

    def test_tool_error_honest_answer(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Spotify"})),
                          final("Не получилось: Spotify не установлен. Могу открыть Музыку."))
        turn = AIClient(llm=llm).respond("Открой спотифай")

        self.assertEqual(turn.speech, "Не получилось: Spotify не установлен. Могу открыть Музыку.")
        [result] = tool_messages(llm.requests[1])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "приложение Spotify не найдено")
        self.assertEqual(result["note"], HONESTY_NOTE)
        self.assertNotIn("Готово", turn.speech)
        self.mock_run.assert_not_called()

    def test_model_claims_success_after_error(self):
        # Страховка от неправды: инструмент упал, а модель говорит «готово»
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Spotify"})), final("Готово, Spotify открыт."))
        turn = AIClient(llm=llm).respond("Открой спотифай")
        self.assertEqual(turn.speech, "Не получилось: приложение Spotify не найдено.")

    def test_success_is_spoken_after_execution(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Safari"})), final("Открыла Safari."))
        turn = AIClient(llm=llm).respond("Открой сафари")
        self.assertEqual(turn.speech, "Открыто приложение Safari.")  # шаблон, без второго запроса (4.4)
        self.assertEqual(len(llm.requests), 1)
        self.mock_run.assert_called_once_with(["open", "-a", "Safari"], capture_output=True, text=True, timeout=10)
        self.assertTrue(turn.results[0]["ok"])

    def test_blocked_rejects_whole_reply(self):
        llm = ScriptedLLM(tools_reply(call("open_app", {"app": "Safari"}),
                                      call("trash_file", {"path": "/System"}), text="Удаляю System."))
        turn = AIClient(llm=llm).respond("открой сафари и удали систему")

        self.assertEqual(turn.speech, BLOCKED_REPLY)
        self.assertEqual([a["name"] for a in turn.blocked], ["trash_file"])
        self.assertEqual(len(llm.requests), 1)  # модели ничего не возвращали
        self.mock_run.assert_not_called()       # и Safari не открыт

    def test_unknown_tool_blocked(self):
        llm = ScriptedLLM(tools_reply(call("run_shell", {"command": "sudo rm -rf /"})))
        turn = AIClient(llm=llm).respond("выполни команду")
        self.assertEqual(turn.speech, BLOCKED_REPLY)
        self.mock_run.assert_not_called()

    def test_confirm_not_executed_and_not_returned(self):
        llm = ScriptedLLM(tools_reply(call("quit_app", {"app": "Terminal"}), text="Закрываю Terminal."))
        turn = AIClient(llm=llm).respond("закрой терминал")

        self.assertEqual([a["name"] for a in turn.pending], ["quit_app"])
        self.assertEqual(turn.speech, "")       # текст модели до подтверждения не звучит
        self.assertEqual(len(llm.requests), 1)  # результат модели не возвращается
        self.mock_run.assert_not_called()

    def test_mixed_safe_and_confirm(self):
        llm = ScriptedLLM(tools_reply(call("get_time"), call("quit_app", {"app": "Terminal"})))
        turn = AIClient(llm=llm).respond("скажи время и закрой терминал")
        self.assertTrue(turn.speech.startswith("Сейчас "))  # SAFE выполнен и озвучен
        self.assertEqual([a["name"] for a in turn.pending], ["quit_app"])
        self.mock_run.assert_not_called()

    def test_three_rounds_limit(self):
        llm = ScriptedLLM(tools_reply(call("get_time")))  # модель бесконечно просит инструмент
        turn = AIClient(llm=llm).respond("который час")
        self.assertEqual(turn.speech, TOO_MANY_ROUNDS_REPLY)
        self.assertEqual(len(llm.requests), 4)  # 3 круга с инструментами + 4-й ответ, который прерываем
        self.assertEqual(sum(1 for r in turn.results if r["action"]["name"] == "get_time"), 3)

    def test_bad_arguments_returned_as_error(self):
        llm = ScriptedLLM(tools_reply(call("open_app", raw="{не json")), final("Не получилось, повторите, пожалуйста."))
        turn = AIClient(llm=llm).respond("открой")
        [result] = tool_messages(llm.requests[1])
        self.assertEqual((result["ok"], result["error"]), (False, "неверные параметры"))
        self.assertEqual(turn.speech, "Не получилось, повторите, пожалуйста.")

    def test_all_providers_down(self):
        llm = ScriptedLLM(LLMResponse(ok=False, text="Нет связи с моделью. Попробуйте позже."))
        self.assertEqual(AIClient(llm=llm).respond("привет").speech, "Нет связи с моделью. Попробуйте позже.")
        print("✅ Цикл инструментов: OK")


class TestPromiseWithoutAction(RunMock):
    def test_retry_once_then_tool_called(self):
        llm = ScriptedLLM(final("Открываю Safari."), tools_reply(call("open_app", {"app": "Safari"})),
                          final("Открыла Safari."))
        turn = AIClient(llm=llm).respond("открой сафари")
        self.assertEqual(turn.speech, "Открыто приложение Safari.")
        self.assertEqual(len(llm.requests), 2)  # обещание + повтор с вызовом; результат модели не нужен
        self.assertEqual(llm.requests[1]["messages"][-1], {"role": "user", "content": PROMISE_RETRY_NOTE})
        self.mock_run.assert_called_once()

    def test_still_no_tool_after_retry(self):
        llm = ScriptedLLM(final("Открываю Safari."), final("Запускаю Safari!"))
        turn = AIClient(llm=llm).respond("открой сафари")
        self.assertEqual(turn.speech, PROMISE_FAIL_REPLY)
        self.assertEqual(len(llm.requests), 2)  # повтор — ровно один
        self.mock_run.assert_not_called()

    def test_honest_refusal_after_retry_is_spoken(self):
        llm = ScriptedLLM(final("Перемещаю файл."), final("Не могу: укажите, какой файл."))
        self.assertEqual(AIClient(llm=llm).respond("убери файл").speech, "Не могу: укажите, какой файл.")

    def test_promise_after_real_tool_call_is_fine(self):
        # Инструмент с данными уже вызван — «Выполняю» в финальном тексте не считается пустым обещанием
        llm = ScriptedLLM(tools_reply(call("get_time")), final("Выполняю: сейчас вечер."))
        self.assertEqual(AIClient(llm=llm).respond("который час").speech, "Выполняю: сейчас вечер.")
        self.assertEqual(len(llm.requests), 2)

    def test_false_success_without_call_is_caught(self):
        # Команда в фразе + «Светлая тема включена» без единого вызова = ложный успех
        llm = ScriptedLLM(final("Светлая тема включена."), final("Готово, тема включена."))
        turn = AIClient(llm=llm).respond("включи светлую тему")
        self.assertEqual(turn.speech, PROMISE_FAIL_REPLY)
        self.assertEqual(len(llm.requests), 2)   # повтор ровно один
        self.assertEqual(llm.requests[1]["messages"][-1], {"role": "user", "content": PROMISE_RETRY_NOTE})
        self.assertIn("Не сообщай о результате без вызова", PROMISE_RETRY_NOTE)

    def test_false_success_retry_with_tool_call(self):
        llm = ScriptedLLM(final("Тема выключена."), tools_reply(call("display", {"action": "dark_mode_off", "value": ""})))
        with patch("core.mac_actions.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            turn = AIClient(llm=llm).respond("выключи тёмную тему")
        self.assertEqual(turn.speech, "Тёмная тема выключена.")

    def test_each_success_word_is_caught(self):
        for answer in ["Тема включена", "Звук выключена", "Включил", "Выключил.", "Safari открыт", "Файл закрыт",
                       "Скриншот сохранён", "Сохранил в документы", "Установлен", "Сделано", "Готово.",
                       "Выполнено", "Перемещён в Корзину", "Отправлено"]:
            with self.subTest(answer=answer):
                llm = ScriptedLLM(final(answer))
                turn = AIClient(llm=llm).respond("сделай что-нибудь с этим файлом")
                self.assertEqual(turn.speech, PROMISE_FAIL_REPLY)

    def test_ordinary_answers_not_touched(self):
        cases = [("до скольки открыт музей", "Музей открыт до 18:00."),
                 ("кто ты", "Я Jarvis, голосовой помощник."),
                 ("работает ли магазин", "Магазин сейчас закрыт."),
                 ("открой сафари", "Safari не открыт: приложение не найдено."),      # «не открыт» — честный отказ
                 ("какой статус", "Статус: готово.")]    # «готово» без команды в фразе
        for phrase, answer in cases:
            with self.subTest(phrase=phrase):
                llm = ScriptedLLM(final(answer))
                self.assertEqual(AIClient(llm=llm).respond(phrase).speech, answer)
                self.assertEqual(len(llm.requests), 1)

    def test_success_claim_after_real_call_not_touched(self):
        llm = ScriptedLLM(tools_reply(call("get_time")), final("Всё сделано: сейчас вечер."))
        self.assertEqual(AIClient(llm=llm).respond("сделай запрос времени").speech, "Всё сделано: сейчас вечер.")
        self.assertEqual(len(llm.requests), 2)

    def test_json_mode_false_success(self):
        llm = ScriptedLLM(LLMResponse(ok=True, mode="json", text='{"response": "Светлая тема включена.", "actions": []}'))
        self.assertEqual(AIClient(llm=llm).respond("включи светлую тему").speech, PROMISE_FAIL_REPLY)
        llm = ScriptedLLM(LLMResponse(ok=True, mode="json", text='{"response": "Музей открыт до 18:00.", "actions": []}'))
        self.assertEqual(AIClient(llm=llm).respond("до скольки открыт музей").speech, "Музей открыт до 18:00.")

    def test_json_mode_promise_without_actions(self):
        llm = ScriptedLLM(LLMResponse(ok=True, mode="json", text='{"response": "Открываю Safari.", "actions": []}'))
        self.assertEqual(AIClient(llm=llm).respond("открой сафари").speech, PROMISE_FAIL_REPLY)
        print("✅ Обещание без действия: OK")


class TestConfirmFlow(RunMock):
    """main + ai: CONFIRM не выполняется до «да» и выполняется после."""

    def make_app(self, llm):
        app = MagicMock()
        app.ai = AIClient(llm=llm)
        app.has_pending.return_value = False
        app.report_result = lambda results: main.VoiceApp.report_result(app, results)
        app.deliver = lambda turn, text: main.VoiceApp.deliver(app, turn, text)
        app.ask_confirmation = lambda pending, phrase: main.VoiceApp.ask_confirmation(app, pending, phrase)
        return app

    def spoken(self, app):
        return [c.args[0] for c in app.engine.speak.call_args_list]

    def test_confirm_then_yes(self):
        app = self.make_app(ScriptedLLM(tools_reply(call("quit_app", {"app": "Terminal"}))))
        app.mac.execute.return_value = None

        main.VoiceApp.process_command(app, "закрой терминал")
        self.mock_run.assert_not_called()
        pending = app.set_pending.call_args.args[0]
        self.assertEqual(self.spoken(app), ["Закрыть Terminal?"])

        app.engine.speak.reset_mock()
        app.take_pending.return_value = (pending, "закрой терминал")
        main.VoiceApp.handle_confirmation(app, "да")
        self.assertEqual(self.spoken(app), ["Выполняю.", "Готово."])
        self.assertEqual(self.mock_run.call_args.args[0][-1], "Terminal")
        # Ответ на подтверждение попал в историю
        self.assertEqual(app.ai.history.messages()[-2:], [{"role": "user", "content": "да"},
                                                          {"role": "assistant", "content": "Готово. [quit_app Terminal: успешно]"}])

    def test_confirm_then_failure(self):
        self.mock_run.return_value.returncode = 1
        app = self.make_app(ScriptedLLM())
        app.take_pending.return_value = ([{"name": "quit_app", "args": {"app": "Terminal"}}], "закрой терминал")
        main.VoiceApp.handle_confirmation(app, "да")
        self.assertEqual(self.spoken(app), ["Выполняю.", "Не получилось: команда завершилась с ошибкой."])

    def test_forget_command(self):
        llm = ScriptedLLM(final("Привет!"))
        app = self.make_app(llm)
        app.ai.remember("как дела", "Хорошо.")
        main.VoiceApp.process_command(app, "Забудь разговор")
        self.assertEqual(app.ai.history.messages(), [])
        self.assertEqual(self.spoken(app), [FORGET_REPLY])
        app.mac.execute.assert_not_called()
        self.assertEqual(llm.requests, [])
        print("✅ Подтверждение и «забудь разговор»: OK")


class TestHistory(RunMock):
    def test_keeps_last_six_messages(self):
        history = ConversationHistory()
        for i in range(5):
            history.add_exchange(f"вопрос {i}", f"ответ {i}")
        messages = history.messages()
        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[0]["content"], "вопрос 2")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"] * 3)

    def test_limit_2000_chars(self):
        history = ConversationHistory()
        for i in range(3):
            history.add_exchange("в" * 590, "о" * 590)
        self.assertLessEqual(history.total_chars(), 2000)
        self.assertEqual(len(history.messages()) % 2, 0)

    def test_tool_results_one_line(self):
        history = ConversationHistory()
        results = [{"action": {"name": "open_app", "args": {"app": "Safari"}}, "ok": True},
                   {"action": {"name": "find_apps", "args": {"query": "tele"}}, "ok": False, "short": "пустой запрос"}]
        history.add_exchange("открой", "Открыла Safari.", results)
        assistant = history.messages()[1]["content"]
        self.assertEqual(assistant, "Открыла Safari. [open_app Safari: успешно; find_apps tele: ошибка: пустой запрос]")
        self.assertNotIn("\n", assistant)

    def test_history_sent_to_model(self):
        llm = ScriptedLLM(final("Хорошо."), final("Вы спрашивали про погоду."))
        client = AIClient(llm=llm)
        client.respond("какая погода")
        client.respond("о чём я спрашивал?")
        roles = [m["role"] for m in llm.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(llm.requests[1]["messages"][1]["content"], "какая погода")
        # Тот же контекст и для режима JSON
        self.assertEqual([m["role"] for m in llm.requests[1]["json_messages"]], roles)

    def test_forget_phrases(self):
        for text in ["забудь разговор", "Забудь всё", "очисти историю", "забудь, пожалуйста, наш разговор"]:
            self.assertTrue(is_forget_command(text), text)
        for text in ["забудь", "какая история у Рима", "не забудь купить хлеб и молоко завтра утром"]:
            self.assertFalse(is_forget_command(text), text)
        print("✅ История: OK")


class FakeProvider(LLMClient):
    def __init__(self, name, replies, supports_tools=True):
        self.name, self.model, self.supports_tools = name, f"{name}-model", supports_tools
        self.daily_limit, self.timezone = None, "UTC"
        self.replies, self.requests = list(replies), []

    def chat(self, messages, tools=None):
        self.requests.append({"messages": messages, "tools": tools})
        reply = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


class TestProvidersAndModes(RunMock):
    def test_local_without_tools_uses_json_mode(self):
        local = FakeProvider("local", [LLMResponse(ok=True, text='{"response": "Открываю Safari.", "actions": '
                                                                 '[{"name": "open_app", "args": {"app": "Safari"}}]}')],
                             supports_tools=False)
        turn = AIClient(llm=FallbackClient([local])).respond("открой сафари")

        self.assertEqual(turn.speech, "Открываю Safari.")
        self.assertIsNone(local.requests[0]["tools"])
        self.assertIn("СТРОГО одним JSON", local.requests[0]["messages"][0]["content"])
        self.mock_run.assert_called_once()

    def test_tools_error_switches_provider(self):
        bad_request = openai.BadRequestError("tools not supported",
                                             response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                                             body=None)
        gemini = FakeProvider("gemini", [bad_request])
        groq = FakeProvider("groq", [tools_reply(call("get_time")), final("Сейчас вечер.")])
        turn = AIClient(llm=FallbackClient([gemini, groq])).respond("который час")
        self.assertEqual(turn.speech, "Сейчас вечер.")
        self.assertEqual(len(gemini.requests), 1)  # на паузе 60 с — во 2-м круге не пробуется

    def test_round_two_without_tool_provider_answers_from_results(self):
        # Gemini вызвал инструмент, во 2-м круге упал; LM Studio без инструментов результат не примет
        gemini = FakeProvider("gemini", [tools_reply(call("get_time")), openai.APIConnectionError(
            request=httpx.Request("POST", "https://x"))])
        local = FakeProvider("local", [LLMResponse(ok=True, text="{}")], supports_tools=False)
        turn = AIClient(llm=FallbackClient([gemini, local])).respond("который час")
        self.assertTrue(turn.speech.startswith("Сейчас "))  # ответ по результату get_time, без модели
        self.assertEqual(local.requests, [])

    def test_env_flags_and_timeouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("GEMINI_API_KEY=test-key-1234567890\nLLM_PROVIDERS=gemini,local\n", encoding="utf-8")
            with patch('openai.OpenAI') as MockOpenAI:
                gemini, local = build_providers(load_config(env))
            self.assertEqual((gemini.supports_tools, gemini.timeout), (True, 15))
            self.assertEqual((local.supports_tools, local.timeout), (False, 45.0))
            self.assertEqual([c.kwargs["timeout"] for c in MockOpenAI.call_args_list], [15, 45.0])

            env.write_text("LLM_PROVIDERS=local\nLOCAL_TOOLS=true\nLOCAL_TIMEOUT=60\n", encoding="utf-8")
            with patch('openai.OpenAI'):
                [local] = build_providers(load_config(env))
            self.assertEqual((local.supports_tools, local.timeout), (True, 60.0))

    def test_tool_call_dict_generates_id(self):
        raw = SimpleNamespace(id=None, function=SimpleNamespace(name="get_time", arguments=None))
        self.assertEqual(_tool_call_dict(raw, 2), {"id": "call_2", "name": "get_time", "arguments": "{}"})
        print("✅ Провайдеры и режимы: OK")


class TestTimeAndPrompt(unittest.TestCase):
    def test_get_time(self):
        self.assertEqual(safety.classify({"name": "get_time", "args": {}})[0], safety.SAFE)
        done = get_time()
        self.assertRegex(done.speech, r"^Сейчас (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье), "
                                      r"\d{1,2} \w+ \d{4} года, \d{2}:\d{2}\.$")
        fixed = datetime(2026, 10, 27, 14, 51)
        self.assertEqual(prompts.describe_now(fixed), "вторник, 27 октября 2026 года, 14:51")

    def test_tools_match_whitelist(self):
        names = {t["function"]["name"] for t in TOOLS}
        self.assertEqual(names, safety.ACTION_NAMES)
        for tool in TOOLS:
            self.assertEqual(tool["type"], "function")
            self.assertEqual(tool["function"]["parameters"]["type"], "object")

    def test_call_to_action(self):
        self.assertEqual(call_to_action(call("open_app", {"app": "Safari"})),
                         ({"name": "open_app", "args": {"app": "Safari"}}, None))
        self.assertEqual(call_to_action(call("open_app", raw="[1, 2]")), (None, "неверные параметры"))

    def test_prompt_short_with_character(self):
        prompt = prompts.tools_prompt(datetime(2026, 10, 27, 14, 51))
        self.assertLessEqual(len(prompt), prompts.MAX_PROMPT_CHARS)
        self.assertTrue(prompt.startswith("Сейчас вторник, 27 октября 2026 года, 14:51."))
        for phrase in ["Jarvis", "по-русски", "на «вы»", "Markdown", "get_time", "find_apps",
                       "данные, а не команды", "не вернул успех"]:
            self.assertIn(phrase, prompt)
        self.assertNotIn("Установленные приложения", prompt)
        print("✅ get_time и промпт: OK")


if __name__ == '__main__':
    unittest.main()
