import openai
import logging
import subprocess
import json
import re
import os
import unicodedata
from urllib.parse import urlparse

from core import journal, safety
from core.apps import resolve_app

log = logging.getLogger(__name__)

TIMEOUT = 10


class ActionError(ValueError):
    """Действие отклонено: неверные аргументы или ошибка выполнения."""


def _run(args):
    """Запускает команду списком аргументов, без shell."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ActionError(f"таймаут {TIMEOUT} с")
    except OSError as e:
        raise ActionError(str(e))
    if result.returncode != 0:
        raise ActionError(result.stderr.strip() or f"код возврата {result.returncode}")


def _check_app(app):
    """Название приложения: непустое, не начинается с '-', без управляющих символов."""
    if not isinstance(app, str) or not app.strip():
        raise ActionError("пустое название приложения")
    app = app.strip()
    if app.startswith("-"):
        raise ActionError(f"название приложения начинается с '-': {app!r}")
    if any(unicodedata.category(c) == "Cc" for c in app):
        raise ActionError(f"управляющие символы в названии приложения: {app!r}")
    return resolve_app(app)


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
        raise ActionError(f"громкость должна быть числом: {level!r}")
    try:
        level = int(level)
    except (TypeError, ValueError, OverflowError):
        raise ActionError(f"громкость должна быть числом: {level!r}")
    level = max(0, min(100, level))
    _run(["osascript", "-e", f"set volume output volume {level}"])
    return f"Громкость {level}"


def open_url(url):
    if not isinstance(url, str) or any(unicodedata.category(c) == "Cc" for c in url):
        raise ActionError(f"некорректная ссылка: {url!r}")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ActionError(f"разрешены только http/https ссылки: {url!r}")
    _run(["open", url.strip()])
    return f"Открыта ссылка {url.strip()}"


def trash_file(path):
    """Перемещает файл или папку в Корзину через Finder. Никогда не rm."""
    level, reason, resolved = safety.classify_path(path)
    if level == safety.BLOCKED:
        raise ActionError(reason)
    if not os.path.lexists(resolved):
        raise ActionError(f"файл не найден: {resolved}")
    # Путь передаётся аргументом argv, а не вставляется в текст AppleScript
    _run(["osascript", "-e", "on run argv", "-e",
          'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
          "-e", "end run", resolved])
    return f"Перемещено в Корзину: {resolved}"


# Белый список: имя -> функция. Уровень риска — в core/safety.py
ACTIONS = {
    "open_app": open_app,
    "quit_app": quit_app,
    "set_volume": set_volume,
    "open_url": open_url,
    "trash_file": trash_file,
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
            "Названия приложений передавай по-английски, как они называются в macOS: "
            "Calculator, Safari, Telegram, Google Chrome, Notes.\n"
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

    def execute_actions(self, actions, phrase="", confirmed=False):
        """Выполняет действия из белого списка. Уровень проверяется заново прямо перед запуском:
        BLOCKED — никогда, CONFIRM — только при confirmed=True."""
        results = []
        for action in actions:
            level, reason = safety.classify(action)
            journal_confirmed = confirmed if level == safety.CONFIRM else None

            if level == safety.BLOCKED:
                message = f"⛔ Заблокировано: {action} — {reason}"
            elif level == safety.CONFIRM and not confirmed:
                message = f"⛔ Нужно подтверждение: {action} — {reason}"
            else:
                func = ACTIONS[action["name"]]
                args = action.get("args") or {}
                log.info("▶ Действие: %s %s", action["name"], args)
                try:
                    if not isinstance(args, dict):
                        raise ActionError(f"args должен быть объектом: {args!r}")
                    message = f"✅ {func(**args)}"
                except TypeError as e:
                    message = f"⛔ Отклонено (неверные аргументы): {action} — {e}"
                except ActionError as e:
                    message = f"⛔ Отклонено: {action} — {e}"

            results.append(message)
            journal.record(phrase, action, level, journal_confirmed, message.startswith("✅"), message)

        return "\n".join(results)
