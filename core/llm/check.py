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


def main(argv=None, config=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Проверка связи с LLM-провайдерами")
    parser.add_argument("--models", action="store_true", help="показать доступные модели")
    parser.add_argument("--provider", help="проверить только этого провайдера (gemini, groq, local)")
    args = parser.parse_args(argv)

    config = load_config() if config is None else config
    if args.provider:
        config = {**config, "LLM_PROVIDERS": args.provider}
    providers = build_providers(config)
    configured = {p.name for p in providers}
    for name in (n.strip().lower() for n in config.get("LLM_PROVIDERS", "").split(",")):
        if name in PROVIDER_SPECS and name not in configured:
            _print(f"{name:<7} пропущен: в .env не задан {PROVIDER_SPECS[name].key_var}", out)

    results = [check_provider(p, args.models, out) for p in providers]
    return 0 if any(results) else 1


if __name__ == "__main__":
    sys.exit(main())
