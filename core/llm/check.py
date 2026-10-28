"""Проверка связи с провайдерами: python -m core.llm.check [--models]

Для каждого провайдера из .env: список моделей и один короткий запрос.
Выводит только имя, OK/ошибку, время ответа и модель. Ключи не выводятся.
"""
import argparse
import sys
import time

from core.llm.base import describe_error
from core.llm.providers import PROVIDER_SPECS, build_providers, load_config
from core.logger import redact_secrets

PROMPT = 'Скажи "работаю"'


def _print(line, out):
    print(redact_secrets(line), file=out)


def check_provider(provider, show_models=False, out=sys.stdout):
    models, models_error = [], None
    try:
        models = provider.list_models()
    except Exception as e:
        models_error = describe_error(e)

    started = time.monotonic()
    try:
        provider.chat([{"role": "user", "content": PROMPT}])
        status = "OK"
    except Exception as e:
        status = f"ошибка: {describe_error(e)}"
    elapsed = time.monotonic() - started

    _print(f"{provider.name:<7} {status:<45} {elapsed:5.1f} с   модель: {provider.model}", out)
    if show_models:
        if models_error:
            _print(f"        /models: ошибка: {models_error}", out)
        else:
            # Gemini отдаёт ID с префиксом «models/»
            available = set(models) | {m.removeprefix("models/") for m in models}
            mark = "есть" if provider.model in available else "НЕТ"
            _print(f"        /models: {len(models)} шт., выбранная модель в списке: {mark}", out)
            for model_id in models:
                _print(f"          {model_id}", out)
    return status == "OK"


TOOLS_PROMPT = "Который час?"
# Типичные запросы по одной теме: их размер схем должен укладываться в 3500 символов
BUDGET_PHRASES = ["поставь таймер на 5 минут", "что у меня сегодня", "открой настройки Wi-Fi", "какая погода"]


def request_estimate(phrase):
    """(символы схем, оценка токенов всего запроса: системный промпт + фраза + схемы, / 3,5) без обращения к сети."""
    from core import prompts, topics
    from core.tools import TOOLS
    tools, _ = topics.select(phrase, TOOLS)
    chars = topics.size(tools) + len(prompts.tools_prompt()) + len(phrase)
    return topics.size(tools), topics.estimate_tokens(chars), [t["function"]["name"] for t in tools]


def check_budget(config, out=sys.stdout):
    """Оценка токенов запросов без сети: четыре типичные фразы, полный набор и доля лимита токенов в минуту."""
    from core import topics
    from core.tools import TOOLS
    limits = {"groq": int(config.get("GROQ_TPM_LIMIT", 8000)), "gemini": int(config.get("GEMINI_TPM_LIMIT", 200000))}
    _print(f"{'запрос':<28} {'схемы':>6} {'токенов':>8}   доля минутного лимита Groq", out)
    ok = True
    for phrase in BUDGET_PHRASES:
        chars, tokens, names = request_estimate(phrase)
        ok = ok and chars <= topics.TYPICAL_REQUEST_CHARS
        _print(f"{phrase:<28} {chars:>6} {tokens:>8}   {tokens / limits['groq']:.0%}  ({', '.join(names[5:])})", out)
    all_chars = topics.size(TOOLS)
    _print(f"{'все схемы вместе':<28} {all_chars:>6} {topics.estimate_tokens(all_chars):>8}   "
           f"(в запрос целиком не уходят)", out)
    _print(f"типичный запрос ≤ {topics.TYPICAL_REQUEST_CHARS} символов схем: {'да' if ok else 'НЕТ'}; "
           f"лимиты токенов в минуту: Groq {limits['groq']}, Gemini {limits['gemini']}", out)
    return ok


def check_tools(provider, out=sys.stdout):
    """Живая проверка tool calling: «Который час?» с единственным инструментом get_time.
    Выполнить модель может только get_time (только чтение) — на Mac ничего не меняется."""
    from core.ai import AIClient
    from core.llm.router import FallbackClient
    from core.tools import tools_subset

    client = AIClient(llm=FallbackClient([provider]), tools=tools_subset({"get_time"}),
                      allowed_actions={"get_time"})
    started = time.monotonic()
    turn = client.respond(TOOLS_PROMPT)
    elapsed = time.monotonic() - started
    called = any(isinstance(r.get("action"), dict) and r["action"].get("name") == "get_time" and r.get("ok")
                 for r in turn.results)
    _print(f"{provider.name:<7} get_time: {'да' if called else 'нет':<4} {elapsed:5.1f} с   «{turn.speech}»", out)
    chars, tokens, _ = request_estimate(TOOLS_PROMPT)
    _print(f"        оценка запроса с набором по теме: схемы {chars} символов, ≈{tokens} токенов", out)
    return called


PAGE_CHECK_URL = "https://en.wikipedia.org/wiki/Dushanbe"


def _read_page_check(url):
    """Только загрузка и извлечение текста, без модели. Результат — число символов."""
    from core import web
    from core.ai import ActionError, Done
    try:
        title, domain, text, _ = web.read_page(url)
    except web.WebError as e:
        raise ActionError(str(e), e.short)
    return Done(f"{len(text)} символов ({domain})")


def check_web(out=sys.stdout):
    """По одному разу: погода, курс USD→TJS, поиск. Выводит ok/ошибку, время и первые 80 символов."""
    from core.ai import ActionError, get_exchange_rate, get_weather, web_search

    checks = [("get_weather", lambda: get_weather()),
              ("get_exchange_rate", lambda: get_exchange_rate("USD", "TJS")),
              ("web_search", lambda: web_search("погода Душанбе")),
              ("web_search (новости)", lambda: web_search("новости Душанбе")),
              ("read_page", lambda: _read_page_check(PAGE_CHECK_URL))]
    results = []
    for name, run in checks:
        started = time.monotonic()
        try:
            done = run()
            status, text = "OK", " ".join((done.for_model or done.message).split())
        except ActionError as e:
            status, text = f"ошибка: {e.short}", ""
        elapsed = time.monotonic() - started
        _print(f"{name:<22} {status:<32} {elapsed:4.1f} с   {text[:80]}", out)
        results.append(status == "OK")
    return results


def main(argv=None, config=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Проверка связи с LLM-провайдерами")
    parser.add_argument("--models", action="store_true", help="показать доступные модели")
    parser.add_argument("--provider", help="проверить только этого провайдера (gemini, groq, local)")
    parser.add_argument("--tools", action="store_true",
                        help="живая проверка tool calling: «Который час?» с get_time (2 запроса на провайдера)")
    parser.add_argument("--web", action="store_true",
                        help="проверка погоды, курса USD→TJS и поиска (бесплатные сервисы, без ключей)")
    parser.add_argument("--budget", action="store_true",
                        help="оценка токенов запросов (символы / 3,5) для четырёх типичных фраз, без сети")
    args = parser.parse_args(argv)
    if args.budget:
        return 0 if check_budget(load_config() if config is None else config, out) else 1
    if args.web:
        return 0 if any(check_web(out)) else 1

    config = load_config() if config is None else config
    if args.provider:
        config = {**config, "LLM_PROVIDERS": args.provider}
    providers = build_providers(config)
    configured = {p.name for p in providers}
    for name in (n.strip().lower() for n in config.get("LLM_PROVIDERS", "").split(",")):
        if name in PROVIDER_SPECS and name not in configured:
            _print(f"{name:<7} пропущен: в .env не задан {PROVIDER_SPECS[name].key_var}", out)

    if args.tools:
        results = [check_tools(p, out) for p in providers if p.supports_tools]
    else:
        results = [check_provider(p, args.models, out) for p in providers]
    return 0 if any(results) else 1


if __name__ == "__main__":
    sys.exit(main())
