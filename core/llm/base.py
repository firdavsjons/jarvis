from dataclasses import dataclass, field

import openai

NO_CONNECTION_REPLY = "Нет связи с моделью. Попробуйте позже."


@dataclass
class LLMResponse:
    ok: bool
    text: str = ""
    tool_calls: list = field(default_factory=list)  # для tool calling (шаг 4.2)
    provider: str = None
    model: str = None


class LLMClient:
    """Общий интерфейс: chat(messages, tools=None) -> LLMResponse."""

    name = "llm"

    def chat(self, messages, tools=None):
        raise NotImplementedError


class EmptyResponseError(Exception):
    """Модель вернула пустой ответ — считаем ошибкой провайдера."""


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
