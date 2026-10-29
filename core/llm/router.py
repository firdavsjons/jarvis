"""Цепочка запасных вариантов: провайдеры по порядку, упавший — пауза 60 с, лимиты — заранее."""
import logging
import time

import openai

from core.llm.base import (NO_CONNECTION_REPLY, RATE_LIMIT_REPLY, LLMClient, LLMResponse, describe_error,
                           server_message)
from core.llm.providers import ENV_FILE, build_providers, load_config
from core.llm.usage import TPM_WINDOW, UsageTracker, estimate_tokens

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60
BAD_REQUEST_PAUSE = 5  # 400 — ошибка самого запроса, сервис доступен: ждать минуту незачем
TPM_MAX_WAIT = 12                # окно токенов освободится не позже чем через столько секунд — ждём и повторяем один раз
TPM_MARGIN = 0.1                 # запас к ожиданию: запись выходит из окна ровно через минуту
SERVER_RETRY_STATUSES = (500, 502, 503, 504)   # временные сбои сервера: тот же запрос повторяется один раз
SERVER_RETRY_DELAY = 2           # через столько секунд
TPM_REPLY = "Слишком много запросов подряд, подождите минуту."
RATE_LIMIT_STREAK = 3            # столько 429 подряд…
RATE_LIMIT_WINDOW = 10 * 60      # …за 10 минут
RATE_LIMIT_PAUSE = 15 * 60       # -> пауза 15 минут вместо 60 с
DISPLAY_NAMES = {"gemini": "Gemini", "groq": "Groq", "local": "LM Studio"}


def _real_sleep(seconds):
    time.sleep(seconds)


def is_transient_server_error(error):
    return isinstance(error, openai.APIStatusError) and error.status_code in SERVER_RETRY_STATUSES


class FallbackClient(LLMClient):
    name = "fallback"
    on_wait = None   # callback() — пришлось ждать (повтор после 5xx, окно токенов): AIClient говорит «Секунду.»

    def __init__(self, providers, usage=None, clock=time.monotonic, rate_limit_pause=RATE_LIMIT_PAUSE,
                 sleep=None):
        self.providers = list(providers)
        self.usage = usage or UsageTracker(
            {p.name: getattr(p, "daily_limit", None) for p in self.providers},
            {p.name: getattr(p, "timezone", "UTC") for p in self.providers},
            tpm_limits={p.name: getattr(p, "tpm_limit", None) for p in self.providers},
            clock=clock,
        )
        self._clock = clock
        self._sleep = sleep if sleep is not None else _real_sleep
        self._down_until = {}  # провайдер -> до какого момента не пробовать
        self.rate_limit_pause = rate_limit_pause
        self._rate_limits = {}  # провайдер -> моменты 429 подряд (сбрасываются успехом или другой ошибкой)
        self._down_reason = {}  # провайдер -> "limit" (429) или "error": почему он на паузе

    def chat(self, messages, tools=None, json_messages=None):
        """tools — инструменты; провайдер без их поддержки получает json_messages (старый режим JSON),
        а если json_messages нет (2-й круг с результатами инструментов) — пропускается.

        Лимит токенов в минуту: провайдер, которого запрос выведет за 90%, пропускается (как при дневном лимите).
        Если ответа нет, а окно токенов хоть у одного освободится не позже чем через TPM_MAX_WAIT секунд — ждём ровно
        столько и пробуем один раз; дольше ждать не будем. 5xx: тот же запрос повторяется один раз (см. _try_providers).
        «Слишком много запросов подряд» — только если остальные провайдеры не просто недоступны: лимиты (токены,
        сутки) без сетевых ошибок; иначе «Нет связи»."""
        response, reasons, wait = self._try_providers(messages, tools, json_messages)
        if response is None and wait is not None and wait <= TPM_MAX_WAIT:
            log.info("LLM: лимит токенов в минуту, жду %.1f с", wait)
            self._notify_wait()
            self._sleep(wait + TPM_MARGIN)
            response, reasons, wait = self._try_providers(messages, tools, json_messages)
        if response is not None:
            return response
        if reasons and "tpm" in reasons and all(r in ("tpm", "limit") for r in reasons):
            log.error("LLM: лимит токенов в минуту, окно освободится через %.0f с", wait if wait is not None else 0)
            return LLMResponse(ok=False, text=TPM_REPLY)
        if reasons and all(r == "limit" for r in reasons):
            log.error("LLM: у всех провайдеров исчерпан лимит запросов")
            return LLMResponse(ok=False, text=RATE_LIMIT_REPLY)
        log.error("LLM: все провайдеры недоступны")
        return LLMResponse(ok=False, text=NO_CONNECTION_REPLY)

    def _notify_wait(self):
        if self.on_wait is None:
            return
        try:
            self.on_wait()
        except Exception as e:
            log.warning("LLM: оповещение об ожидании не удалось: %s", type(e).__name__)

    def _try_providers(self, messages, tools, json_messages):
        """(ответ или None, причины пропуска каждого провайдера: "limit", "tpm" или "error", через сколько секунд
        освободится окно токенов у ближайшего пропущенного по "tpm" провайдера — или None)."""
        reasons = []
        wait = None
        for provider in self.providers:
            if self._clock() < self._down_until.get(provider.name, 0):
                log.debug("LLM %s пропущен: недавно падал", provider.name)
                reasons.append(self._down_reason.get(provider.name, "error"))
                continue
            use_json = bool(tools) and not getattr(provider, "supports_tools", True)
            if use_json and json_messages is None:
                log.debug("LLM %s пропущен: без инструментов, а нужен ответ на их результаты", provider.name)
                reasons.append("error")
                continue
            if self.usage.near_limit(provider.name):
                reasons.append("limit")
                continue
            tokens = estimate_tokens(json_messages if use_json else messages, None if use_json else tools,
                                     getattr(provider, "max_tokens", None))
            if self.usage.tpm_exceeded(provider.name, tokens):
                log.warning("лимит токенов в минуту, пропускаю %s", DISPLAY_NAMES.get(provider.name, provider.name))
                reasons.append("tpm")
                until = self.usage.seconds_until_fits(provider.name, tokens)
                if until is not None and (wait is None or until < wait):
                    wait = until
                continue

            started = self._clock()
            response, error = None, None
            for attempt in (1, 2):
                self.usage.record(provider.name)
                self.usage.record_tokens(provider.name, tokens)
                try:
                    if use_json:
                        response = provider.chat(json_messages)
                        response.mode = "json"
                    else:
                        response = provider.chat(messages, tools)
                    error = None
                    break
                except Exception as e:
                    error = e
                    if attempt == 1 and is_transient_server_error(e):
                        # 500/502/503/504: сервер споткнулся — тот же запрос тому же провайдеру ещё раз через 2 с
                        log.warning("LLM %s: %s — повторяю тот же запрос через %s с", provider.name, describe_error(e),
                                    SERVER_RETRY_DELAY)
                        self._notify_wait()
                        self._sleep(SERVER_RETRY_DELAY)
                        continue
                    break
            if error is not None:
                e = error
                pause = self._pause_after(provider.name, e)
                self._down_until[provider.name] = self._clock() + pause
                self._down_reason[provider.name] = "limit" if isinstance(e, openai.RateLimitError) else "error"
                reasons.append(self._down_reason[provider.name])
                if pause in (COOLDOWN_SECONDS, BAD_REQUEST_PAUSE):
                    detail = server_message(e)
                    log.warning("LLM %s: %s%s — переключаюсь на следующего, пауза %s с", provider.name,
                                describe_error(e), f": {detail}" if detail else "", pause)
                else:
                    log.warning("лимит %s, пауза %d мин", DISPLAY_NAMES.get(provider.name, provider.name),
                                round(pause / 60))
                continue

            self._rate_limits.pop(provider.name, None)

            log.info("LLM: ответ от %s (%s) за %.1f с", provider.name, getattr(provider, "model", "?"),
                     self._clock() - started)
            return response, reasons, wait
        return None, reasons, wait

    def _pause_after(self, name, error):
        """60 с; 400 — 5 с (ошибка запроса); 3 ответа 429 подряд за 10 минут — 15 минут (лимит исчерпан)."""
        if not isinstance(error, openai.RateLimitError):
            self._rate_limits.pop(name, None)
            if isinstance(error, openai.BadRequestError):
                return BAD_REQUEST_PAUSE
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
                         {p.name: p.timezone for p in providers}, path=usage_path,
                         tpm_limits={p.name: p.tpm_limit for p in providers})
    try:
        pause = max(1.0, float(config.get("LLM_RATE_LIMIT_PAUSE_MIN", 15))) * 60
    except ValueError:
        pause = RATE_LIMIT_PAUSE
    return FallbackClient(providers, usage, rate_limit_pause=pause)
