"""Провайдеры с OpenAI-совместимым API. Настройки — только из файла .env (не из окружения терминала)."""
import logging
from dataclasses import dataclass
from pathlib import Path

import openai
from dotenv import dotenv_values

from core.llm.base import EmptyResponseError, LLMClient, LLMResponse
from core.logger import register_secret

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
REQUEST_TIMEOUT = 15  # секунд на запрос к облачным провайдерам
TEMPERATURE = 0.3     # меньше креативности, больше точности

DEFAULTS = {
    "LLM_PROVIDERS": "gemini,groq,local",
    "GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "GEMINI_MODEL": "gemini-2.5-flash",
    "GEMINI_DAILY_LIMIT": "200",
    "GROQ_BASE_URL": "https://api.groq.com/openai/v1",
    # Выбраны по `python -m core.llm.check --models` (2026-10-27): llama-3.3-70b-versatile в Groq уже нет (404)
    "GROQ_MODEL": "openai/gpt-oss-120b",
    "GROQ_DAILY_LIMIT": "800",
    "LOCAL_BASE_URL": "http://localhost:1234/v1",
    "LOCAL_MODEL": "google/gemma-3-4b",
    "LOCAL_TIMEOUT": "45",  # локальная модель отвечает медленнее (12,5 с на проверке 4.1)
    # Поддерживает ли модель tool calling; без него — старый режим JSON в тексте
    "GEMINI_TOOLS": "true",
    "GROQ_TOOLS": "true",
    "LOCAL_TOOLS": "false",
}


@dataclass(frozen=True)
class ProviderSpec:
    prefix: str             # GEMINI -> GEMINI_BASE_URL, GEMINI_MODEL, ...
    key_var: str = None     # переменная с ключом; None — ключ не нужен
    limit_var: str = None   # дневной лимит; None — без лимита
    timezone: str = "UTC"   # в каком часовом поясе начинаются «сутки» лимита
    timeout_var: str = None  # отдельный таймаут; None — REQUEST_TIMEOUT


PROVIDER_SPECS = {
    "gemini": ProviderSpec("GEMINI", "GEMINI_API_KEY", "GEMINI_DAILY_LIMIT", "America/Los_Angeles"),
    "groq": ProviderSpec("GROQ", "GROQ_API_KEY", "GROQ_DAILY_LIMIT", "UTC"),
    "local": ProviderSpec("LOCAL", timeout_var="LOCAL_TIMEOUT"),
}


def load_config(path=ENV_FILE):
    """Настройки из .env поверх значений по умолчанию. os.environ не читается и не меняется."""
    values = {k: v.strip() for k, v in dotenv_values(path).items() if isinstance(v, str) and v.strip()}
    for spec in PROVIDER_SPECS.values():
        if spec.key_var:
            register_secret(values.get(spec.key_var))
    return {**DEFAULTS, **values}


class OpenAICompatProvider(LLMClient):
    def __init__(self, name, base_url, model, api_key=None, daily_limit=None, timezone="UTC",
                 supports_tools=True, timeout=REQUEST_TIMEOUT):
        self.name = name
        self.base_url = base_url
        self.model = model
        self.daily_limit = daily_limit
        self.timezone = timezone
        self.supports_tools = supports_tools
        self.timeout = timeout
        register_secret(api_key)
        # max_retries=0: повторы и переключение делает FallbackClient
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key or "not-needed",
                                     timeout=timeout, max_retries=0)

    def __repr__(self):  # без ключа
        return f"<{type(self).__name__} {self.name} {self.model}>"

    def chat(self, messages, tools=None):
        kwargs = {"model": self.model, "messages": messages, "temperature": TEMPERATURE}
        if tools:
            kwargs["tools"] = tools
        completion = self._client.chat.completions.create(**kwargs)
        message = completion.choices[0].message if completion.choices else None
        text = ((message.content if message else None) or "").strip()
        tool_calls = [_tool_call_dict(call, i) for i, call in enumerate((message.tool_calls if message else None) or [])]
        if not text and not tool_calls:
            raise EmptyResponseError()
        return LLMResponse(ok=True, text=text, tool_calls=tool_calls, provider=self.name, model=self.model,
                           mode="tools" if tools else "text")

    def list_models(self):
        return sorted(m.id for m in self._client.models.list())


def _tool_call_dict(call, index):
    """Вызов инструмента из ответа openai -> обычный словарь (переносим между провайдерами и в тестах)."""
    function = getattr(call, "function", None)
    return {
        "id": getattr(call, "id", None) or f"call_{index}",
        "name": getattr(function, "name", None) or "",
        "arguments": getattr(function, "arguments", None) or "{}",
    }


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


def _timeout(config, spec):
    if not spec.timeout_var:
        return REQUEST_TIMEOUT
    raw = config.get(spec.timeout_var, DEFAULTS[spec.timeout_var])
    try:
        return max(1.0, float(raw))
    except ValueError:
        log.warning("%s=%r — не число, беру %s", spec.timeout_var, raw, DEFAULTS[spec.timeout_var])
        return float(DEFAULTS[spec.timeout_var])


def _limit(config, spec):
    if not spec.limit_var:
        return None
    raw = config.get(spec.limit_var, DEFAULTS[spec.limit_var])
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("%s=%r — не число, беру %s", spec.limit_var, raw, DEFAULTS[spec.limit_var])
        return int(DEFAULTS[spec.limit_var])


def build_providers(config):
    """Провайдеры в порядке LLM_PROVIDERS. Без ключа — пропускается (в лог только имя)."""
    providers = []
    for name in (n.strip().lower() for n in config.get("LLM_PROVIDERS", "").split(",")):
        if not name:
            continue
        spec = PROVIDER_SPECS.get(name)
        if spec is None:
            log.warning("Неизвестный провайдер в LLM_PROVIDERS: %s", name)
            continue
        api_key = config.get(spec.key_var) if spec.key_var else None
        if spec.key_var and not api_key:
            log.warning("Провайдер %s пропущен: в .env не задан %s", name, spec.key_var)
            continue
        providers.append(OpenAICompatProvider(
            name=name,
            base_url=config.get(f"{spec.prefix}_BASE_URL", DEFAULTS[f"{spec.prefix}_BASE_URL"]),
            model=config.get(f"{spec.prefix}_MODEL", DEFAULTS[f"{spec.prefix}_MODEL"]),
            api_key=api_key,
            daily_limit=_limit(config, spec),
            timezone=spec.timezone,
            supports_tools=_flag(config.get(f"{spec.prefix}_TOOLS", DEFAULTS[f"{spec.prefix}_TOOLS"])),
            timeout=_timeout(config, spec),
        ))
    return providers
