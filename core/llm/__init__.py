"""Слой LLM: провайдеры (Gemini, Groq, LM Studio) за общим интерфейсом и цепочка запасных вариантов."""
from core.llm.base import NO_CONNECTION_REPLY, LLMClient, LLMResponse
from core.llm.router import FallbackClient, build_default_client

__all__ = ["NO_CONNECTION_REPLY", "LLMClient", "LLMResponse", "FallbackClient", "build_default_client"]
