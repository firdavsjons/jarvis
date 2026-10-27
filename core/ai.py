import logging
import subprocess
import json
import re
import os
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

from core import installed_apps, journal, prompts, safety, web
from core.cache import cacheable_actions
from core.history import ConversationHistory
from core.memory import SECRET_REFUSAL, MemoryRefused, UserMemory, asked_to_remember, contains_secret
from core.llm import build_default_client
from core.speech_text import confirmation_question, url_for_speech
from core.tools import TOOLS, assistant_message, call_to_action, tool_message

log = logging.getLogger(__name__)

TIMEOUT = 10
SPOKEN_LIST_LIMIT = 10  # сколько названий читать вслух, остальное — в окне
MAX_TOOL_ROUNDS = 3     # кругов «модель вызвала инструменты -> мы вернули результаты»

BLOCKED_REPLY = "Это слишком опасно, я этого не сделаю."
TOO_MANY_ROUNDS_REPLY = "Не получилось завершить запрос."
AI_ERROR_REPLY = "Произошла ошибка при обращении к нейросети."
PROMISE_RETRY_NOTE = "Вызови инструмент или скажи, что не можешь. Не обещай действия без вызова."
PROMISE_FAIL_REPLY = "Не получилось: действие не было выполнено."
# Модель обещает действие («Открываю Safari»), но не вызвала ни одного инструмента
_PROMISE_RE = re.compile(r"(?<!\w)(открываю|закрываю|запускаю|устанавливаю|выполняю|перемещаю)(?!\w)", re.IGNORECASE)
MEMORY_ACTIONS = {"remember", "remember_alias", "list_memory", "forget_memory", "clear_memory"}
REMEMBER_ACTIONS = {"remember", "remember_alias"}
NOT_ASKED_SHORT = "запоминаю только по вашей просьбе со словом «запомни»"
NOT_FOUND_SHORT = "не нашёл"
# Простые действия: при успехе модели результат не возвращаем — фраза по шаблону, на 1 запрос меньше
TEMPLATE_ACTIONS = {"open_app", "quit_app", "set_volume", "open_url"}
MAX_TEMPLATE_PARTS = 3
# Данные из интернета: после них в этой фразе нельзя менять память, открывать ссылки, удалять и закрывать
WEB_ACTIONS = {"web_search", "get_weather", "get_exchange_rate"}
RESTRICTED_AFTER_WEB = {"remember", "remember_alias", "forget_memory", "clear_memory", "open_url", "trash_file",
                        "quit_app"}
WEB_RESTRICTED_SHORT = "в ответе были данные из интернета"
# Признаки того, что модель честно сообщила о неудаче (страховка от «Готово» после ошибки инструмента)
_ADMITS_FAILURE_RE = re.compile(r"(?<!\w)не(?!\w)|нельзя|невозможн|ошибк|сожалени|увы|не удалось|не получилось",
                                re.IGNORECASE)


class ActionError(ValueError):
    """Действие отклонено: неверные аргументы или ошибка выполнения.

    short — короткая причина по-русски для голоса и окна («файл не найден»).
    """

    def __init__(self, message, short="команда завершилась с ошибкой", reply=None):
        super().__init__(message)
        self.short = short
        self.reply = reply  # готовая фраза вместо «Не получилось: …» («Такое я запоминать не буду.»)


class Done:
    """Результат действия: message — в лог и окно, speech — сказать пользователю (для действий-чтений),
    for_model — что вернуть модели (если короче окна), web — данные из интернета (не инструкции)."""

    def __init__(self, message, speech=None, for_model=None, web=False):
        self.message = message
        self.speech = speech
        self.for_model = for_model
        self.web = web


def _run(args):
    """Запускает команду списком аргументов, без shell."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ActionError(f"таймаут {TIMEOUT} с", "команда не ответила вовремя")
    except OSError as e:
        raise ActionError(str(e), "команда не запустилась")
    if result.returncode != 0:
        raise ActionError(result.stderr.strip() or f"код возврата {result.returncode}")


def _check_app(app):
    """Название приложения: непустое, не начинается с '-', без управляющих символов, установлено."""
    if not isinstance(app, str) or not app.strip():
        raise ActionError("пустое название приложения", "не указано приложение")
    app = app.strip()
    if app.startswith("-"):
        raise ActionError(f"название приложения начинается с '-': {app!r}", "неверное название приложения")
    if any(unicodedata.category(c) == "Cc" for c in app):
        raise ActionError(f"управляющие символы в названии приложения: {app!r}", "неверное название приложения")
    resolved = installed_apps.resolve_app_name(app)
    if resolved is None:
        raise ActionError(f"приложение {app} не найдено среди установленных", f"приложение {app} не найдено")
    return resolved


def _spoken_list(names):
    spoken = ", ".join(names[:SPOKEN_LIST_LIMIT])
    rest = len(names) - SPOKEN_LIST_LIMIT
    return f"{spoken} и ещё {rest}" if rest > 0 else spoken


# --- Белый список действий ---

def open_app(app):
    app = _check_app(app)
    _run(["open", "-a", app])
    return f"Открыто приложение {app}"


def quit_app(app):
    app = _check_app(app)
    # Название передаётся аргументом argv, а не вставляется в текст AppleScript
    _run(["osascript", "-e", "on run argv", "-e", "tell application (item 1 of argv) to quit",
          "-e", "end run", app])
    return f"Закрыто приложение {app}"


def set_volume(level):
    if isinstance(level, bool):
        raise ActionError(f"громкость должна быть числом: {level!r}", "громкость должна быть числом")
    try:
        level = int(level)
    except (TypeError, ValueError, OverflowError):
        raise ActionError(f"громкость должна быть числом: {level!r}", "громкость должна быть числом")
    level = max(0, min(100, level))
    _run(["osascript", "-e", f"set volume output volume {level}"])
    return f"Громкость {level}"


def open_url(url):
    if not isinstance(url, str) or any(unicodedata.category(c) == "Cc" for c in url):
        raise ActionError(f"некорректная ссылка: {url!r}", "некорректная ссылка")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ActionError(f"разрешены только http/https ссылки: {url!r}", "некорректная ссылка")
    _run(["open", url.strip()])
    return f"Открыта ссылка {url.strip()}"


def trash_file(path):
    """Перемещает файл или папку в Корзину через Finder. Никогда не rm."""
    level, reason, resolved = safety.classify_path(path)
    if level == safety.BLOCKED:
        raise ActionError(reason, "это запрещено")
    if not os.path.lexists(resolved):
        raise ActionError(f"файл не найден: {resolved}", "файл не найден")
    # Путь передаётся аргументом argv, а не вставляется в текст AppleScript
    _run(["osascript", "-e", "on run argv", "-e",
          'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
          "-e", "end run", resolved])
    return f"Перемещено в Корзину: {resolved}"


def find_apps(query):
    """Только чтение: до 10 установленных приложений по запросу."""
    if not isinstance(query, str) or not query.strip():
        raise ActionError("пустой запрос", "не указано, что искать")
    found = installed_apps.find_apps(query)
    if not found:
        # «Не найдено» — неудача: модель и страховка от неправды не объявят успех
        raise ActionError(f"по запросу «{query.strip()}» ничего не найдено", NOT_FOUND_SHORT)
    return Done(f"Найдено: {', '.join(found)}", f"Нашёл: {_spoken_list(found)}.")


def list_apps():
    """Только чтение: до 30 установленных приложений, популярные первыми."""
    apps = installed_apps.popular_first(installed_apps.list_installed_apps())
    if not apps:
        return Done("Список приложений пуст", "Не удалось получить список приложений.")
    return Done(f"Установлены: {', '.join(apps)}", f"Установлены: {_spoken_list(apps)}.")


def _web(call, *args):
    try:
        return call(*args)
    except web.WebError as e:
        raise ActionError(str(e), e.short)


def web_search(query):
    """Только чтение: поиск в интернете (ddgs). Модели — сжато, в окно — со ссылками."""
    for_model, window = _web(web.search, query)
    first = for_model.split("\n", 1)[0].split(". ", 1)[-1]
    return Done(f"Поиск «{query}»:\n{window}", f"Нашёл: {first}", for_model=for_model, web=True)


def get_weather(city=None):
    """Только чтение: погода Open-Meteo, город по умолчанию — DEFAULT_CITY из .env."""
    text = _web(web.weather, city)
    return Done(text, text, for_model=text, web=True)


def get_exchange_rate(base, target):
    """Только чтение: курс валют (open.er-api.com, запасной — currency-api)."""
    text = _web(web.exchange_rate, base, target)
    return Done(text, text, for_model=text, web=True)


def get_time():
    """Только чтение: текущие дата, время и день недели (системный часовой пояс)."""
    now = prompts.describe_now()
    return Done(f"Сейчас {now}", f"Сейчас {now}.")


# Белый список: имя -> функция. Уровень риска — в core/safety.py
ACTIONS = {
    "open_app": open_app,
    "quit_app": quit_app,
    "set_volume": set_volume,
    "open_url": open_url,
    "trash_file": trash_file,
    "find_apps": find_apps,
    "list_apps": list_apps,
    "get_time": get_time,
    "web_search": web_search,
    "get_weather": get_weather,
    "get_exchange_rate": get_exchange_rate,
}


def describe_action(action):
    """Полное описание действия для окна и журнала (для голоса — speech_text.action_for_speech)."""
    args = action.get("args") or {}
    name = action.get("name")
    if name == "quit_app":
        return f"закрыть {args.get('app')}"
    if name == "open_url":
        return f"открыть ссылку {args.get('url')}"
    if name == "trash_file":
        _, _, resolved = safety.classify_path(args.get("path"))
        return f"переместить в Корзину {resolved or args.get('path')}"
    if name == "remember":
        return f"запомнить: {args.get('text')}"
    if name == "remember_alias":
        return f"запомнить: {args.get('spoken')} — это {args.get('app')}"
    return str(name)


def summarize_results(results):
    """(всё ли успешно, что сказать). Ошибка -> «Не получилось: …», без «Готово»."""
    failed, replies = [], []
    for r in results:
        if r["ok"]:
            continue
        if r.get("reply"):
            if r["reply"] not in replies:
                replies.append(r["reply"])
        elif r["short"] not in failed:
            failed.append(r["short"])
    if failed or replies:
        parts = ([f"Не получилось: {', '.join(failed)}."] if failed else []) + replies
        return False, " ".join(parts)
    return True, " ".join(r["speech"] for r in results if r["speech"])


def _template_part(result):
    action = result["action"]
    if action.get("name") == "open_url":
        return f"Открыта ссылка {url_for_speech(str((action.get('args') or {}).get('url', '')))}"
    return result.get("result") or "Готово"


def template_speech(results):
    """«Открыто приложение Telegram.», «Открыто приложение Telegram, громкость 50.», больше 3 — «Готово.»."""
    if not results or len(results) > MAX_TEMPLATE_PARTS:
        return "Готово."
    parts = [_template_part(r) for r in results]
    return ", ".join([parts[0]] + [p[:1].lower() + p[1:] for p in parts[1:]]) + "."


def normalize_ai_response(data):
    """(текст ответа, действия) из ответа модели в режиме JSON. Неожиданный формат — пустой ответ."""
    if not isinstance(data, dict):
        log.warning("Ответ AI не словарь: %r", data)
        return "", []
    response_text = data.get("response")
    if not isinstance(response_text, str):
        response_text = ""
    return response_text, data.get("actions", [])


@dataclass
class Turn:
    """Итог одной фразы пользователя: что сказать сейчас и что ждёт подтверждения."""
    speech: str = ""
    pending: list = field(default_factory=list)  # CONFIRM-действия: main задаёт вопрос
    blocked: list = field(default_factory=list)  # BLOCKED: весь ответ отклонён
    results: list = field(default_factory=list)  # выполненные действия — для окна и лога


class AIClient:
    def __init__(self, llm=None, tools=TOOLS, allowed_actions=None, memory=None, cache=None):
        # Провайдеры (Gemini → Groq → LM Studio) с запасными вариантами — core/llm, настройки из .env
        self.llm = llm if llm is not None else build_default_client()
        self.tools = tools
        self.allowed_actions = allowed_actions  # None — весь белый список (подмножество — для проверки связи)
        self.history = ConversationHistory()
        # Память: по умолчанию только в памяти процесса; файл (~/Library/Application Support/Jarvis) — из main
        self.memory = memory if memory is not None else UserMemory()
        self.memory.on_change = self._memory_changed
        installed_apps.set_user_aliases(self.memory.aliases)
        self.cache = cache  # None — кэш команд выключен (тесты); в приложении — CommandCache с файлом

    def _memory_changed(self, aliases_changed):
        installed_apps.set_user_aliases(self.memory.aliases)
        if aliases_changed and self.cache is not None:
            self.cache.clear()  # «открой браузер» мог закэшироваться со старым псевдонимом

    def _prompt_kwargs(self):
        return {"memory_block": self.memory.prompt_block(), "assistant_name": self.memory.assistant_name}

    @property
    def system_prompt(self):
        return prompts.tools_prompt(**self._prompt_kwargs())

    def forget(self):
        self.history.clear()

    def remember(self, user_text, assistant_text, results=()):
        self.history.add_exchange(user_text, assistant_text, results)

    # --- Режим инструментов ---

    def respond(self, text):
        """Фраза пользователя -> Turn. Сначала кэш команд; иначе модель (инструменты или режим JSON)."""
        turn = self._from_cache(text)
        if turn is None:
            turn = self._respond_model(text)
            self._maybe_cache(text, turn)
        return turn

    def _from_cache(self, text):
        """Попадание в кэш: без модели, через обычный путь (classify, повторная проверка, журнал)."""
        if self.cache is None:
            return None
        actions = self.cache.lookup(text)
        if not actions:
            return None
        if any(safety.classify(a)[0] != safety.SAFE for a in actions):
            self.cache.remove(text)  # уровень изменился (например, сайт убрали из доверенных) — спросим модель
            return None
        log.info("Кэш команд: «%s» -> %s", text, actions)
        results = self.run_actions(actions, phrase=text)
        ok, summary = summarize_results(results)
        if ok:
            self.cache.mark_used(text)
            speech = ". ".join(r["result"] for r in results if r.get("result")) + "."
        else:
            self.cache.remove(text)
            speech = summary
        return self._finish(text, Turn(speech, results=results))

    def _maybe_cache(self, text, turn):
        if self.cache is None or turn.pending or turn.blocked:
            return
        actions = cacheable_actions(turn.results)
        if actions and self.cache.store(text, actions):
            log.info("Кэш команд: запомнил «%s» -> %s", text, actions)

    def _respond_model(self, text):
        """Инструменты: SAFE — выполнить и вернуть модели результат (до 3 кругов), CONFIRM — вопрос
        пользователю, BLOCKED — отклонить весь ответ. Модели без инструментов — режим JSON."""
        history = self.history.messages()
        user = {"role": "user", "content": text}
        messages = [{"role": "system", "content": prompts.tools_prompt(**self._prompt_kwargs())}, *history, user]
        json_messages = [{"role": "system", "content": prompts.json_prompt(**self._prompt_kwargs())}, *history, user]
        executed = []
        tool_rounds, requests, retried, web_seen = 0, 0, False, False

        while True:
            try:
                # json_messages только в первом запросе: без инструментов результаты им не вернуть
                response = self.llm.chat(messages, tools=self.tools,
                                         json_messages=json_messages if requests == 0 else None)
                requests += 1
            except Exception as e:
                log.error("❌ Ошибка запроса к AI: %s", type(e).__name__)
                return self._finish(text, self._results_turn(executed, AI_ERROR_REPLY))
            if not response.ok:
                # Нет связи — или остался только провайдер без инструментов: ответ по результатам, без модели
                return self._finish(text, self._results_turn(executed, response.text))
            if response.mode == "json":
                return self._finish(text, self._json_turn(text, response.text))
            if not response.tool_calls:
                if not executed and _PROMISE_RE.search(response.text or ""):
                    if not retried:
                        # Обещание без вызова инструмента — переспросить один раз
                        log.warning("LLM: обещание без вызова инструмента — повторяю запрос: %s", response.text)
                        retried = True
                        messages = messages + [{"role": "assistant", "content": response.text},
                                               {"role": "user", "content": PROMISE_RETRY_NOTE}]
                        continue
                    return self._finish(text, Turn(PROMISE_FAIL_REPLY))
                return self._finish(text, Turn(self._honest(response.text, executed), results=executed))
            if tool_rounds == MAX_TOOL_ROUNDS:
                log.warning("LLM: больше %s кругов с инструментами — прерываю", MAX_TOOL_ROUNDS)
                return self._finish(text, Turn(TOO_MANY_ROUNDS_REPLY, results=executed))

            calls = []  # (вызов, действие или None, ошибка разбора)
            for call in response.tool_calls:
                action, error = call_to_action(call)
                calls.append((call, action, error))
            actions = [a for _, a, _ in calls if a is not None]
            safe, confirm, blocked = self.split_actions(actions, phrase=text)

            if blocked:
                self.reject_actions(actions, text, "в ответе есть опасное действие")
                return self._finish(text, Turn(BLOCKED_REPLY, blocked=blocked, results=executed))

            # Веб-данные в этой фразе (уже получены или запрошены в этом же круге) — запрет на память,
            # ссылки, удаление и закрытие: текст из интернета мог подсказать модели такие вызовы
            web_seen = web_seen or any(a and a.get("name") in WEB_ACTIONS for _, a, _ in calls)
            if web_seen and any(a and a.get("name") in RESTRICTED_AFTER_WEB for _, a, _ in calls):
                self.reject_actions([a for _, a, _ in calls if a and a.get("name") in RESTRICTED_AFTER_WEB],
                                    text, WEB_RESTRICTED_SHORT)
                calls = [(c, None, WEB_RESTRICTED_SHORT) if a and a.get("name") in RESTRICTED_AFTER_WEB else (c, a, e)
                         for c, a, e in calls]
                actions = [a for _, a, _ in calls if a is not None]
                safe, confirm, _ = self.split_actions(actions, phrase=text)
            if confirm:
                # SAFE — сейчас, CONFIRM — после «да»; модели ничего не возвращаем, пока пользователь не ответит
                results = self.run_actions(safe, phrase=text)
                executed += results
                ok, summary = summarize_results(results)
                return self._finish(text, Turn(summary, pending=confirm, results=executed))

            tool_rounds += 1
            round_results = self.run_actions(safe, phrase=text)
            if self._template_ok(calls, round_results):
                # Всё простое и успешное — фраза по шаблону, второй запрос к модели не нужен
                executed += round_results
                return self._finish(text, Turn(template_speech(round_results), results=executed))
            results = iter(round_results)
            messages = messages + [assistant_message(response)]
            for call, action, error in calls:
                if action is None:
                    executed.append({"action": {"name": call.get("name"), "args": {}}, "ok": False,
                                     "short": error, "speech": None, "message": f"⛔ {error}"})
                    messages.append(tool_message(call["id"], False, error=error))
                    continue
                result = next(results)
                executed.append(result)
                messages.append(tool_message(call["id"], result["ok"], result=result.get("result"),
                                             error=result["short"], web=result.get("web", False)))

    @staticmethod
    def _template_ok(calls, results):
        return (all(action is not None for _, action, _ in calls) and bool(results)
                and all(r["ok"] and r["action"].get("name") in TEMPLATE_ACTIONS for r in results))

    def _honest(self, final_text, executed):
        """Страховка: инструмент упал, а модель говорит как об успехе — говорим правду сами."""
        ok, summary = summarize_results(executed)
        if not ok and not _ADMITS_FAILURE_RE.search(final_text or ""):
            log.warning("LLM: ответ похож на успех после ошибки инструмента — заменяю: %s", final_text)
            return summary
        return final_text

    def _results_turn(self, executed, fallback_text):
        if not executed:
            return Turn(fallback_text)
        ok, summary = summarize_results(executed)
        return Turn(summary if not ok else (summary or "Готово."), results=executed)

    def _finish(self, text, turn):
        said = " ".join(p for p in [turn.speech, confirmation_question(turn.pending) if turn.pending else ""] if p)
        self.remember(text, said, turn.results)
        return turn

    # --- Режим JSON (модели без tool calling) ---

    def _json_turn(self, text, content):
        """Как до шага 4.2: SAFE — выполнить, потом сказать; CONFIRM — вопрос; BLOCKED — отказ."""
        response_text, actions = normalize_ai_response(self._parse_json(content))
        safe, confirm, blocked = self.split_actions(actions, phrase=text)
        if blocked:
            self.reject_actions(actions, text, "в ответе есть опасное действие")
            return Turn(BLOCKED_REPLY, blocked=blocked)
        if isinstance(actions, list) and any(isinstance(a, dict) and a.get("name") in WEB_ACTIONS for a in actions):
            restricted = [a for a in actions if isinstance(a, dict) and a.get("name") in RESTRICTED_AFTER_WEB]
            if restricted:
                self.reject_actions(restricted, text, WEB_RESTRICTED_SHORT)
                safe = [a for a in safe if a not in restricted]
                confirm = [a for a in confirm if a not in restricted]
        if not safe:
            if not confirm and not actions and _PROMISE_RE.search(response_text):
                return Turn(PROMISE_FAIL_REPLY)  # «Открываю Safari» без единого действия
            return Turn("" if confirm else response_text, pending=confirm)

        results = self.run_actions(safe, phrase=text)
        ok, summary = summarize_results(results)
        if not ok or confirm:
            # Ответ модели («Открываю…», «Перемещаю…») мог быть неправдой — не озвучиваем
            return Turn(summary, pending=confirm, results=results)
        return Turn(" ".join(p for p in (response_text, summary) if p) or "Готово.", results=results)

    def ask(self, text):
        """Один запрос в режиме JSON: {"response", "actions"}."""
        messages = [
            {"role": "system", "content": prompts.json_prompt(**self._prompt_kwargs())},
            {"role": "user", "content": text},
        ]
        try:
            response = self.llm.chat(messages)
        except Exception as e:
            log.error("❌ Ошибка запроса к AI: %s", type(e).__name__)
            return {"response": AI_ERROR_REPLY, "actions": []}
        if not response.ok:
            # Все провайдеры недоступны: фраза для голоса, без действий
            return {"response": response.text, "actions": []}
        return self._parse_json(response.text)

    def _parse_json(self, content):
        """Пытается извлечь и распарсить JSON из ответа модели."""
        try:
            # 1. Попытка распарсить "чистый" JSON (или с небольшим мусором)
            clean_content = re.sub(r"```json\s*|\s*```", "", content).strip()
            return json.loads(clean_content)
        except json.JSONDecodeError:
            try:
                # 2. Попытка найти JSON объект внутри текста
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if match:
                    json_str = match.group(0)
                    return json.loads(json_str)
            except:
                pass

            log.warning("⚠️ Ошибка парсинга JSON: %s", content)
            return {"response": content, "actions": []}

    def _memory_action(self, name, phrase, confirmed=False):
        """Действие памяти. phrase — исходная фраза ПОЛЬЗОВАТЕЛЯ (не аргументы модели): запись в память
        только если в ней есть «запомни», «запиши» или «учти», либо пользователь ответил «да» на вопрос
        «Запомнить: …?» (confirmed). Текст со страниц и ответы модели не в счёт."""
        def run(**args):
            # Секрет — отказ сразу и с понятной фразой, ещё до проверки «запомни»
            if name in REMEMBER_ACTIONS and any(contains_secret(v) for v in args.values() if isinstance(v, str)):
                raise ActionError(f"{name}: секрет", "такое я запоминать не буду", SECRET_REFUSAL)
            if name in REMEMBER_ACTIONS and not asked_to_remember(phrase) and not confirmed:
                raise ActionError(f"{name} без просьбы пользователя: {phrase!r}", NOT_ASKED_SHORT)
            method = {"remember": self.memory.remember, "remember_alias": self.memory.remember_alias,
                      "list_memory": self.memory.list_memory, "forget_memory": self.memory.forget,
                      "clear_memory": self.memory.clear}[name]
            if name == "forget_memory":
                args = {"query": args.get("query")} if set(args) <= {"query"} else args
            try:
                message, speech = method(**args)
            except MemoryRefused as e:
                raise ActionError(f"память: {e.short}", e.short, e.reply)
            return Done(message, speech)
        return run

    def split_actions(self, actions, phrase=None):
        """Делит действия по уровню риска: (SAFE, CONFIRM, BLOCKED).

        phrase — исходная фраза пользователя: remember / remember_alias без «запомни» в ней становятся
        CONFIRM («Запомнить: …?»). Секреты остаются SAFE, чтобы сразу получить отказ, без вопроса.
        """
        if not isinstance(actions, list):
            return [], [], []
        groups = {safety.SAFE: [], safety.CONFIRM: [], safety.BLOCKED: []}
        for action in actions:
            level, _ = safety.classify(action)
            if level == safety.SAFE and phrase is not None and self._needs_remember_confirmation(action, phrase):
                level = safety.CONFIRM
            groups[level].append(action)
        return groups[safety.SAFE], groups[safety.CONFIRM], groups[safety.BLOCKED]

    @staticmethod
    def _needs_remember_confirmation(action, phrase):
        if action.get("name") not in REMEMBER_ACTIONS or asked_to_remember(phrase):
            return False
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        return not any(contains_secret(v) for v in args.values() if isinstance(v, str))

    def reject_actions(self, actions, phrase, reason, confirmed=None):
        """Записывает в журнал действия, которые не будут выполнены (опасный ответ, «нет», таймаут)."""
        for action in actions:
            level, level_reason = safety.classify(action)
            result = f"{reason}: {level_reason}" if level == safety.BLOCKED else reason
            log.info("⛔ Не выполнено: %s — %s", action, result)
            journal.record(phrase, action, level, confirmed, False, result)

    def run_actions(self, actions, phrase="", confirmed=False):
        """Выполняет действия из белого списка. Уровень проверяется заново прямо перед запуском:
        BLOCKED — никогда, CONFIRM — только при confirmed=True.

        Возвращает по словарю на действие: ok, message (лог/окно), short (причина отказа), speech.
        """
        results = []
        for action in actions:
            level, reason = safety.classify(action)
            remember_confirmed = isinstance(action, dict) and action.get("name") in REMEMBER_ACTIONS and confirmed
            journal_confirmed = confirmed if level == safety.CONFIRM or remember_confirmed else None
            ok, short, speech, result_text, reply, web_data = False, None, None, None, None, False

            if level == safety.BLOCKED:
                message, short = f"⛔ Заблокировано: {action} — {reason}", "это запрещено"
            elif self.allowed_actions is not None and action["name"] not in self.allowed_actions:
                message, short = f"⛔ Не разрешено в этом режиме: {action}", "это действие сейчас недоступно"
            elif level == safety.CONFIRM and not confirmed:
                message, short = f"⛔ Нужно подтверждение: {action} — {reason}", "нужно подтверждение"
            else:
                func = self._memory_action(action["name"], phrase, confirmed) if action["name"] in MEMORY_ACTIONS \
                    else ACTIONS[action["name"]]
                args = action.get("args") or {}
                log.info("▶ Действие: %s %s", action["name"], args)
                try:
                    if not isinstance(args, dict):
                        raise ActionError(f"args должен быть объектом: {args!r}", "неверные параметры")
                    out = func(**args)
                    done = out if isinstance(out, Done) else Done(out)
                    ok, message, speech = True, f"✅ {done.message}", done.speech
                    result_text, web_data = done.for_model or done.message, done.web
                except TypeError as e:
                    message, short = f"⛔ Отклонено (неверные аргументы): {action} — {e}", "неверные параметры"
                except ActionError as e:
                    message, short, reply = f"⛔ Отклонено: {action} — {e}", e.short, e.reply

            results.append({"action": action, "level": level, "ok": ok, "message": message,
                            "short": short, "speech": speech, "result": result_text, "reply": reply,
                            "web": web_data})
            journal.record(phrase, action, level, journal_confirmed, ok, message)

        return results

    def execute_actions(self, actions, phrase="", confirmed=False):
        """То же, что run_actions, но одной строкой для лога."""
        return "\n".join(r["message"] for r in self.run_actions(actions, phrase, confirmed))
