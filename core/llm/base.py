from dataclasses import dataclass, field

import openai

NO_CONNECTION_REPLY = "Нет связи с моделью. Попробуйте позже."
RATE_LIMIT_REPLY = "Лимит запросов исчерпан, попробуйте позже."


@dataclass
class LLMResponse:
    ok: bool
    text: str = ""
    tool_calls: list = field(default_factory=list)  # [{"id", "name", "arguments": str JSON}]
    provider: str = None
    model: str = None
    mode: str = "text"  # "tools" — запрос с инструментами, "json" — старый режим JSON в тексте


class LLMClient:
    """Общий интерфейс: chat(messages, tools=None) -> LLMResponse."""

    name = "llm"

    def chat(self, messages, tools=None):
        raise NotImplementedError


class EmptyResponseError(Exception):
    """Модель вернула пустой ответ — считаем ошибкой провайдера."""


SERVER_MESSAGE_CHARS = 200
DIAGNOSTIC_STATUSES = {400, 401, 403, 404}


def server_message(error, limit=SERVER_MESSAGE_CHARS):
    """Начало сообщения сервера для лога (400/401/403/404 — ошибки запроса или настройки), без секретов.

    Тексты пользователя и результаты инструментов сервер в таком сообщении не возвращает, но на всякий
    случай текст обрезается и проходит через redact_secrets."""
    from core.logger import redact_secrets
    if not isinstance(error, openai.APIStatusError) or error.status_code not in DIAGNOSTIC_STATUSES:
        return ""
    body = error.body if isinstance(error.body, dict) else {}
    detail = body.get("error") if isinstance(body.get("error"), dict) else body
    text = detail.get("message") if isinstance(detail.get("message"), str) else str(error)
    code = detail.get("code") if isinstance(detail.get("code"), str) else ""
    text = " ".join(f"[{code}] {text}".split()) if code else " ".join(text.split())
    return redact_secrets(text)[:limit]


def describe_error(error):
    """Короткое описание ошибки для лога и проверки связи: класс и HTTP-код, без текста ответа сервера
    (в нём могут оказаться данные запроса)."""
    if isinstance(error, openai.APITimeoutError):
        return "таймаут"
    if isinstance(error, openai.APIConnectionError):
        return "нет сети"
    if isinstance(error, openai.RateLimitError):
        return "лимит запросов (429)"
    if isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return f"ошибка настройки: ключ не подходит ({error.status_code})"
    if isinstance(error, openai.NotFoundError):
        return "ошибка настройки: модель или адрес не найдены (404)"
    if isinstance(error, openai.APIStatusError):
        if error.status_code >= 500:
            return f"ошибка сервера ({error.status_code})"
        return f"ошибка запроса ({error.status_code})"
    if isinstance(error, EmptyResponseError):
        return "пустой ответ"
    return type(error).__name__
