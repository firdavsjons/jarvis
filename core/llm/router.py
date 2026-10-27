"""Цепочка запасных вариантов: провайдеры по порядку, упавший — пауза 60 с, лимиты — заранее."""
import logging
import time

import openai

from core.llm.base import NO_CONNECTION_REPLY, LLMClient, LLMResponse, describe_error
from core.llm.providers import ENV_FILE, build_providers, load_config
from core.llm.usage import UsageTracker

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60
RATE_LIMIT_STREAK = 3            # столько 429 подряд…
RATE_LIMIT_WINDOW = 10 * 60      # …за 10 минут
RATE_LIMIT_PAUSE = 15 * 60       # -> пауза 15 минут вместо 60 с
DISPLAY_NAMES = {"gemini": "Gemini", "groq": "Groq", "local": "LM Studio"}


class FallbackClient(LLMClient):
    name = "fallback"

    def __init__(self, providers, usage=None, clock=time.monotonic, rate_limit_pause=RATE_LIMIT_PAUSE):
        self.providers = list(providers)
        self.usage = usage or UsageTracker(
            {p.name: getattr(p, "daily_limit", None) for p in self.providers},
            {p.name: getattr(p, "timezone", "UTC") for p in self.providers},
        )
        self._clock = clock
        self._down_until = {}  # провайдер -> до какого момента не пробовать
        self.rate_limit_pause = rate_limit_pause
        self._rate_limits = {}  # провайдер -> моменты 429 подряд (сбрасываются успехом или другой ошибкой)

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
                pause = self._pause_after(provider.name, e)
                self._down_until[provider.name] = self._clock() + pause
                if pause == COOLDOWN_SECONDS:
                    log.warning("LLM %s: %s — переключаюсь на следующего, пауза %s с",
                                provider.name, describe_error(e), COOLDOWN_SECONDS)
                else:
                    log.warning("лимит %s, пауза %d мин", DISPLAY_NAMES.get(provider.name, provider.name),
                                round(pause / 60))
                continue

            self._rate_limits.pop(provider.name, None)

            log.info("LLM: ответ от %s (%s) за %.1f с", provider.name, getattr(provider, "model", "?"),
                     self._clock() - started)
            return response

        log.error("LLM: все провайдеры недоступны")
        return LLMResponse(ok=False, text=NO_CONNECTION_REPLY)

    def _pause_after(self, name, error):
        """60 с; но 3 ответа 429 подряд за 10 минут — пауза 15 минут (лимит явно исчерпан)."""
        if not isinstance(error, openai.RateLimitError):
            self._rate_limits.pop(name, None)
            return COOLDOWN_SECONDS
        now = self._clock()
        hits = [t for t in self._rate_limits.get(name, []) if now - t <= RATE_LIMIT_WINDOW] + [now]
        if len(hits) >= RATE_LIMIT_STREAK:
            self._rate_limits.pop(name, None)
            return self.rate_limit_pause
        self._rate_limits[name] = hits
        return COOLDOWN_SECONDS


def build_default_client(env_path=ENV_FILE, usage_path=None):
    """Цепочка из .env. usage_path — файл счётчика лимитов (в приложении), None — только в памяти."""
    config = load_config(env_path)
    providers = build_providers(config)
    usage = UsageTracker({p.name: p.daily_limit for p in providers},
                         {p.name: p.timezone for p in providers}, path=usage_path)
    try:
        pause = max(1.0, float(config.get("LLM_RATE_LIMIT_PAUSE_MIN", 15))) * 60
    except ValueError:
        pause = RATE_LIMIT_PAUSE
    return FallbackClient(providers, usage, rate_limit_pause=pause)
