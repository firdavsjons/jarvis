"""Цепочка запасных вариантов: провайдеры по порядку, упавший — пауза 60 с, лимиты — заранее."""
import logging
import time

from core.llm.base import NO_CONNECTION_REPLY, LLMClient, LLMResponse, describe_error
from core.llm.providers import ENV_FILE, build_providers, load_config
from core.llm.usage import UsageTracker

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60


class FallbackClient(LLMClient):
    name = "fallback"

    def __init__(self, providers, usage=None, clock=time.monotonic):
        self.providers = list(providers)
        self.usage = usage or UsageTracker(
            {p.name: getattr(p, "daily_limit", None) for p in self.providers},
            {p.name: getattr(p, "timezone", "UTC") for p in self.providers},
        )
        self._clock = clock
        self._down_until = {}  # провайдер -> до какого момента не пробовать

    def chat(self, messages, tools=None, json_messages=None):
        """tools — инструменты; провайдер без их поддержки получает json_messages (старый режим JSON),
        а если json_messages нет (2-й круг с результатами инструментов) — пропускается."""
        for provider in self.providers:
            if self._clock() < self._down_until.get(provider.name, 0):
                log.debug("LLM %s пропущен: недавно падал", provider.name)
                continue
            use_json = bool(tools) and not getattr(provider, "supports_tools", True)
            if use_json and json_messages is None:
                log.debug("LLM %s пропущен: без инструментов, а нужен ответ на их результаты", provider.name)
                continue
            if self.usage.near_limit(provider.name):
                continue

            self.usage.record(provider.name)
            started = self._clock()
            try:
                if use_json:
                    response = provider.chat(json_messages)
                    response.mode = "json"
                else:
                    response = provider.chat(messages, tools)
            except Exception as e:
                reason = describe_error(e)
                self._down_until[provider.name] = self._clock() + COOLDOWN_SECONDS
                log.warning("LLM %s: %s — переключаюсь на следующего, пауза %s с",
                            provider.name, reason, COOLDOWN_SECONDS)
                continue

            log.info("LLM: ответ от %s (%s) за %.1f с", provider.name, getattr(provider, "model", "?"),
                     self._clock() - started)
            return response

        log.error("LLM: все провайдеры недоступны")
        return LLMResponse(ok=False, text=NO_CONNECTION_REPLY)


def build_default_client(env_path=ENV_FILE, usage_path=None):
    """Цепочка из .env. usage_path — файл счётчика лимитов (в приложении), None — только в памяти."""
    providers = build_providers(load_config(env_path))
    usage = UsageTracker({p.name: p.daily_limit for p in providers},
                         {p.name: p.timezone for p in providers}, path=usage_path)
    return FallbackClient(providers, usage)
