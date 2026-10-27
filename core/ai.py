import openai
import logging
import subprocess
import json
import re
import os
import unicodedata
from urllib.parse import urlparse

from core import installed_apps, journal, safety

log = logging.getLogger(__name__)

TIMEOUT = 10
SPOKEN_LIST_LIMIT = 10  # сколько названий читать вслух, остальное — в окне


class ActionError(ValueError):
    """Действие отклонено: неверные аргументы или ошибка выполнения.

    short — короткая причина по-русски для голоса и окна («файл не найден»).
    """

    def __init__(self, message, short="команда завершилась с ошибкой"):
        super().__init__(message)
        self.short = short


class Done:
    """Результат действия: message — в лог и окно, speech — сказать пользователю (для действий-чтений)."""

    def __init__(self, message, speech=None):
        self.message = message
        self.speech = speech


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
        return Done(f"По запросу «{query.strip()}» ничего не найдено",
                    f"Не нашёл приложений по запросу {query.strip()}.")
    return Done(f"Найдено: {', '.join(found)}", f"Нашёл: {_spoken_list(found)}.")


def list_apps():
    """Только чтение: до 30 установленных приложений, популярные первыми."""
    apps = installed_apps.popular_first(installed_apps.list_installed_apps())
    if not apps:
        return Done("Список приложений пуст", "Не удалось получить список приложений.")
    return Done(f"Установлены: {', '.join(apps)}", f"Установлены: {_spoken_list(apps)}.")


# Белый список: имя -> функция. Уровень риска — в core/safety.py
ACTIONS = {
    "open_app": open_app,
    "quit_app": quit_app,
    "set_volume": set_volume,
    "open_url": open_url,
    "trash_file": trash_file,
    "find_apps": find_apps,
    "list_apps": list_apps,
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
    return str(name)


def summarize_results(results):
    """(всё ли успешно, что сказать). Ошибка -> «Не получилось: …», без «Готово»."""
    failed = []
    for r in results:
        if not r["ok"] and r["short"] not in failed:
            failed.append(r["short"])
    if failed:
        return False, f"Не получилось: {', '.join(failed)}."
    return True, " ".join(r["speech"] for r in results if r["speech"])


class LocalAIClient:
    def __init__(self):
        # Конфигурация для LM Studio
        self.client = openai.OpenAI(
            base_url="http://localhost:1234/v1",
            api_key="lm-studio"
        )
        self.model_name = "local-model"

        # Системный промпт для JSON
        self.system_prompt = (
            "Ты — голосовой ассистент для macOS. Ты управляешь компьютером только через действия из списка ниже.\n"
            "Shell-команд нет: никакие другие действия выполнить нельзя.\n"
            "Формат ответа СТРОГО JSON. Не пиши никакого лишнего текста, только JSON объект.\n"
            "Структура JSON:\n"
            "{\n"
            '  "response": "Текст, который нужно произнести пользователю (на русском).",\n'
            '  "actions": [\n'
            '    {"name": "имя действия", "args": {...}}\n'
            '  ]\n'
            "}\n"
            "Доступные действия:\n"
            '- open_app: открыть приложение. args: {"app": "Safari"}\n'
            '- quit_app: закрыть приложение. args: {"app": "Safari"}\n'
            '- set_volume: установить громкость 0-100. args: {"level": 50}\n'
            '- open_url: открыть ссылку http/https в браузере. args: {"url": "https://example.com"}\n'
            '- trash_file: переместить файл или папку пользователя в Корзину. args: {"path": "~/Desktop/отчёт.txt"}\n'
            '- find_apps: найти установленные приложения по названию или части. args: {"query": "telegram"}\n'
            "- list_apps: список установленных приложений. args: {}\n"
            "Названия приложений передавай по-английски, как они называются в macOS: "
            "Calculator, Safari, Telegram, Google Chrome, Notes.\n"
            f"Установленные приложения: {', '.join(installed_apps.popular_first(installed_apps.list_installed_apps(), 40))}.\n"
            "Если пользователь просит открыть приложение, которого нет в этом списке, сначала вызови find_apps "
            "и не обещай открыть его в response.\n"
            "Удалять файлы можно ТОЛЬКО через trash_file: это перемещение в Корзину, файл можно восстановить. "
            "Путь — полный, от / или ~. Удалять можно только файлы пользователя в домашней папке. "
            "Системные папки (/System, /Library, /Applications, ~/Library, скрытые файлы) и домашнюю папку целиком "
            "удалять нельзя: вежливо откажи и оставь actions пустым.\n"
            "Если действий не требуется или запрос нельзя выполнить этими действиями, оставь массив actions пустым "
            "и объясни это в response.\n"
            "В response не пиши ссылки, пути к файлам и Markdown: текст будет прочитан вслух.\n"
            "Примеры:\n"
            'Пользователь: "Привет"\n'
            'Ответ: {"response": "Привет! Чем могу помочь?", "actions": []}\n'
            'Пользователь: "Открой Сафари и сделай громкость на половину"\n'
            'Ответ: {"response": "Открываю Safari и ставлю громкость 50.", "actions": '
            '[{"name": "open_app", "args": {"app": "Safari"}}, {"name": "set_volume", "args": {"level": 50}}]}\n'
            'Пользователь: "Удали файл отчёт с рабочего стола"\n'
            'Ответ: {"response": "Перемещаю отчёт в Корзину.", "actions": '
            '[{"name": "trash_file", "args": {"path": "~/Desktop/отчёт.txt"}}]}\n'
            'Пользователь: "Удали папку System"\n'
            'Ответ: {"response": "Системные файлы удалять нельзя.", "actions": []}\n'
        )

    def ask(self, text):
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": text}
                ],
                temperature=0.3, # Меньше креативности, больше точности
            )
            content = response.choices[0].message.content
            return self._parse_json(content)
        except openai.APIConnectionError:
            log.error("❌ Ошибка: Не удалось подключиться к LM Studio.")
            return {"response": "Ошибка: Локальный сервер Gemma не запущен. Пожалуйста, запустите LM Studio.", "actions": []}
        except Exception as e:
            log.error("❌ Ошибка запроса к AI: %s", e)
            return {"response": "Произошла ошибка при обращении к нейросети.", "actions": []}

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

    def split_actions(self, actions):
        """Делит действия по уровню риска: (SAFE, CONFIRM, BLOCKED)."""
        if not isinstance(actions, list):
            return [], [], []
        groups = {safety.SAFE: [], safety.CONFIRM: [], safety.BLOCKED: []}
        for action in actions:
            level, _ = safety.classify(action)
            groups[level].append(action)
        return groups[safety.SAFE], groups[safety.CONFIRM], groups[safety.BLOCKED]

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
            journal_confirmed = confirmed if level == safety.CONFIRM else None
            ok, short, speech = False, None, None

            if level == safety.BLOCKED:
                message, short = f"⛔ Заблокировано: {action} — {reason}", "это запрещено"
            elif level == safety.CONFIRM and not confirmed:
                message, short = f"⛔ Нужно подтверждение: {action} — {reason}", "нужно подтверждение"
            else:
                func = ACTIONS[action["name"]]
                args = action.get("args") or {}
                log.info("▶ Действие: %s %s", action["name"], args)
                try:
                    if not isinstance(args, dict):
                        raise ActionError(f"args должен быть объектом: {args!r}", "неверные параметры")
                    out = func(**args)
                    done = out if isinstance(out, Done) else Done(out)
                    ok, message, speech = True, f"✅ {done.message}", done.speech
                except TypeError as e:
                    message, short = f"⛔ Отклонено (неверные аргументы): {action} — {e}", "неверные параметры"
                except ActionError as e:
                    message, short = f"⛔ Отклонено: {action} — {e}", e.short

            results.append({"action": action, "level": level, "ok": ok, "message": message,
                            "short": short, "speech": speech})
            journal.record(phrase, action, level, journal_confirmed, ok, message)

        return results

    def execute_actions(self, actions, phrase="", confirmed=False):
        """То же, что run_actions, но одной строкой для лога."""
        return "\n".join(r["message"] for r in self.run_actions(actions, phrase, confirmed))
