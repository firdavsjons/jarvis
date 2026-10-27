import openai
import logging
import subprocess
import json
import re
import unicodedata
from urllib.parse import urlparse

log = logging.getLogger(__name__)

TIMEOUT = 10

# Русские названия (нижний регистр) -> имена приложений в macOS
APP_ALIASES = {
    "калькулятор": "Calculator",
    "сафари": "Safari",
    "телеграм": "Telegram",
    "хром": "Google Chrome",
    "заметки": "Notes",
    "музыка": "Music",
    "почта": "Mail",
    "календарь": "Calendar",
    "настройки": "System Settings",
    "терминал": "Terminal",
    "фото": "Photos",
    "сообщения": "Messages",
    "файндер": "Finder",
    "финдер": "Finder",
    "spotify": "Spotify",
    "спотифай": "Spotify",
}
ALIAS_PREFIX_MIN = 5  # ключи короче (например «хром») — только точное совпадение


def _resolve_app(app):
    """Русское название -> имя в macOS. Падежи: «калькулятора» начинается с «калькулятор»."""
    name = app.lower()
    if name in APP_ALIASES:
        return APP_ALIASES[name]
    prefixes = [k for k in APP_ALIASES if len(k) >= ALIAS_PREFIX_MIN and name.startswith(k)]
    if prefixes:
        return APP_ALIASES[max(prefixes, key=len)]
    return app


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
    return _resolve_app(app)


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


# имя -> (функция, нужно ли подтверждение пользователя)
ACTIONS = {
    "open_app": (open_app, False),
    "quit_app": (quit_app, True),
    "set_volume": (set_volume, False),
    "open_url": (open_url, True),
}


def describe_action(action):
    """Короткое описание действия для озвучки при запросе подтверждения."""
    args = action.get("args") or {}
    name = action.get("name")
    if name == "quit_app":
        return f"закрыть {args.get('app')}"
    if name == "open_url":
        return f"открыть ссылку {args.get('url')}"
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
            "Названия приложений передавай по-английски, как они называются в macOS: "
            "Calculator, Safari, Telegram, Google Chrome, Notes.\n"
            "Если действий не требуется или запрос нельзя выполнить этими действиями, оставь массив actions пустым "
            "и объясни это в response.\n"
            "Примеры:\n"
            'Пользователь: "Привет"\n'
            'Ответ: {"response": "Привет! Чем могу помочь?", "actions": []}\n'
            'Пользователь: "Открой Сафари и сделай громкость на половину"\n'
            'Ответ: {"response": "Открываю Safari и ставлю громкость 50.", "actions": '
            '[{"name": "open_app", "args": {"app": "Safari"}}, {"name": "set_volume", "args": {"level": 50}}]}\n'
            'Пользователь: "Удали папку загрузки"\n'
            'Ответ: {"response": "Удалять файлы я не умею.", "actions": []}\n'
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
        """Делит действия на (выполнить сразу, нужно подтверждение).

        Неизвестные действия попадают в первый список: execute_actions их отклонит и запишет в лог.
        """
        if not isinstance(actions, list):
            return [], []
        immediate, to_confirm = [], []
        for action in actions:
            entry = ACTIONS.get(action.get("name")) if isinstance(action, dict) else None
            if entry and entry[1]:
                to_confirm.append(action)
            else:
                immediate.append(action)
        return immediate, to_confirm

    def execute_actions(self, actions):
        """Выполняет действия из белого списка, остальные отклоняет."""
        results = []
        for action in actions:
            if not isinstance(action, dict) or action.get("name") not in ACTIONS:
                results.append(f"⛔ Отклонено (нет в белом списке): {action}")
                continue

            func, _ = ACTIONS[action["name"]]
            args = action.get("args") or {}
            log.info("▶ Действие: %s %s", action["name"], args)
            try:
                if not isinstance(args, dict):
                    raise ActionError(f"args должен быть объектом: {args!r}")
                results.append(f"✅ {func(**args)}")
            except TypeError as e:
                results.append(f"⛔ Отклонено (неверные аргументы): {action} — {e}")
            except ActionError as e:
                results.append(f"⛔ Отклонено: {action} — {e}")

        return "\n".join(results)
