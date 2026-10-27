# CLAUDE.md

Голосовой и текстовый ассистент для macOS (MacBook M1). Python + Tkinter, распознавание речи через SpeechRecognition, озвучка через системную команду `say`, «мозг» — облачные LLM (Gemini, Groq) с запасным вариантом — локальная модель в LM Studio (все через OpenAI-совместимый API, `core/llm/`).

## Как устроен поток команды

```
голос (SpeechEngine.listen) / текст (Entry в окне)
        │
        ▼
VoiceApp.process_command            (main.py)
        │
        ├─► правила до модели: «забудь разговор» (история), «забудь кэш команд» (кэш),
        │     «забудь всё обо мне» → сразу вопрос → clear_memory после «да»
        │
        ├─► MacControl.execute(text)   (modules/mac_control.py)
        │     короткие фразы: громкость, mute, браузер, Spotify, калькулятор; вернул строку → озвучить и выйти
        │
        └─► AIClient.respond(text) → Turn(speech, pending, blocked, results)   (core/ai.py)
              кэш команд (core/cache.py): точная нормализованная фраза → SAFE-действия без модели
                (classify + повторная проверка + журнал); ошибка → запись удаляется
              промпт (core/prompts.py: дата/время + характер с именем из памяти + блок памяти ≤600 симв.)
                + история (6 сообщений, ≤2000 симв.)
              llm.chat → FallbackClient (core/llm): gemini → groq → local из LLM_PROVIDERS;
              ошибка/429/таймаут (облако 15 с, LOCAL_TIMEOUT 45 с) → следующий, пауза 60 с; ≥90% лимита — пропуск
              │
              ├─ модель с инструментами (*_TOOLS=true): tool_calls (core/tools.py) → safety.classify
              │    есть BLOCKED → весь ответ отклонён, «Это слишком опасно…», журнал
              │    есть CONFIRM → SAFE выполнить, про CONFIRM — вопрос, модели ничего не возвращать
              │    только SAFE  → выполнить, результат — модели ролью tool (при ошибке — «ответь честно»),
              │                   до 3 кругов, дальше «Не получилось завершить запрос»
              │    финальный текст; если инструмент упал, а текст похож на успех → «Не получилось: …»
              │    «Открываю…» без единого вызова → один повтор с указанием, потом «Не получилось: …»
              │
              └─ модель без инструментов (LOCAL_TOOLS=false): старый режим JSON {"response", "actions"}
                   (во 2-м круге её пропускаем — ответ строится по результатам действий без модели)
              SAFE: open_app, set_volume, find_apps, list_apps, get_time, quit_app кроме CAREFUL_APPS, доверенные сайты
              CONFIRM: trash_file, сайты не из trusted_domains.txt, quit_app для CAREFUL_APPS
                       → вопрос, таймер 20 с; «да» → execute_actions(confirmed=True) → «Готово» / «Не получилось: …»
```

Если есть отложенные действия, следующая фраза (голос или текст) в `process_command` считается ответом на подтверждение (`parse_confirmation`, целые слова). `execute_actions` перепроверяет уровень прямо перед запуском: BLOCKED не выполняется никогда, CONFIRM — только с `confirmed=True`. Каждое действие (выполнено, отклонено, «нет», таймаут) пишется в `~/Library/Logs/Jarvis/actions.jsonl`.

## Структура

- `main.py` — окно Tkinter (`VoiceApp`), очередь обновлений окна `ui_queue` + `poll_ui`, цикл прослушивания `background_loop` (Старт/Стоп через `stop_event`), подтверждение команд, `normalize_ai_response`.
- `core/engine.py` — `SpeechEngine`: STT через `recognize_google` (ru-RU, онлайн), калибровка шума `calibrate()` один раз при старте; TTS через `say` (голос Milena или другой ru_RU, `pick_voice`), прерывание `stop_speaking()`, флаг `speaking`.
- `core/logger.py` — `setup_logging()`: лог в консоль и в `~/Library/Logs/Jarvis/jarvis.log`; `SecretFilter` / `register_secret` / `redact_secrets` — ключи API заменяются на `***` в любом сообщении и трассировке.
- `core/llm/` — слой LLM: `base.py` (`LLMClient`, `LLMResponse`, `describe_error`), `providers.py` (`load_config` — только файл `.env`, `OpenAICompatProvider`, `build_providers`), `router.py` (`FallbackClient`, `build_default_client`), `usage.py` (`UsageTracker`, `llm_usage.json`, сутки Gemini — по тихоокеанскому времени), `check.py` (проверка связи).
- `core/ai.py` — белый список действий `ACTIONS` (`open_app`, `quit_app`, `set_volume`, `open_url`, `trash_file`, `find_apps`, `list_apps`, `get_time`) и `AIClient(llm)`: `respond()` (цикл инструментов, режим JSON, страховка от неправды, история), `run_actions` / `split_actions` / `reject_actions`, `ask()` (один запрос в режиме JSON).
- `core/tools.py` — схемы инструментов OpenAI (`TOOLS`), разбор вызовов, сообщения `assistant`/`tool`.
- `core/prompts.py` — характер Jarvis (`tools_prompt` ≤1200 символов, `json_prompt` для моделей без инструментов), `describe_now`.
- `core/history.py` — `ConversationHistory` (6 сообщений, ≤2000 символов, только память), `is_forget_command`.
- `core/cache.py` — `CommandCache` (`command_cache.json`: 500 записей LRU, 90 дней с создания), `normalize_phrase`, `cacheable_actions` (только успешные SAFE `open_app`/`quit_app`/`set_volume`/`open_url`, ≤3, без контекстных слов).
- `core/memory.py` — `UserMemory` (`memory.json`: факты, псевдонимы приложений, имя ассистента), `asked_to_remember`, `contains_secret`, `valid_assistant_name`, `prompt_block`, `is_clear_memory_command`.
- `core/storage.py` — `DATA_DIR` (`~/Library/Application Support/Jarvis`, папка 700), `atomic_write_json` (файлы 600). `core/settings.py` — чтение `.env` (`ENV_FILE`, `env_value`, `env_float`).
- `core/safety.py` — уровни риска: `classify(action)`, `classify_path` (только внутри дома, без `~/Library` и скрытых файлов верхнего уровня), `classify_url` (по `trusted_domains.txt`), `CAREFUL_APPS`.
- `core/journal.py` — журнал действий `actions.jsonl`, `redact()` скрывает пароли и токены. Включается `setup_journal()` только при запуске `main.py`.
- `core/speech_text.py` — `prepare_for_speech` (вызывается в `speak`: ссылки → «ютуб точка ком», пути → «отчёт точка тэ-экс-тэ, с рабочего стола», без Markdown и эмодзи) и короткие вопросы `confirmation_question`.
- `core/apps.py` — `APP_ALIASES` / `resolve_app`: русские названия приложений → имена в macOS.
- `core/installed_apps.py` — установленные приложения (`os.scandir` по `/Applications`, `/System/Applications`, `~/Applications` и др., кэш 10 мин), `resolve_app_name` (словарь → точно → по слову → нечётко, неоднозначно → `None`), `find_apps`, `popular_first`.
- `trusted_domains.txt` — сайты, которые открываются без подтверждения (с поддоменами).
- `modules/mac_control.py` — `MacControl`: быстрые команды через `osascript` / `open` (subprocess со списком аргументов).
- `test_json_mode.py` — парсинг JSON, белый список, проверка аргументов, подтверждение по целым словам.
- `test_main.py` — `parse_confirmation`, `normalize_ai_response`, выбор голоса, `say` с текстом после `--`, прерывание речи.
- `test_mac_control.py` — `MacControl` по целым словам.
- `test_safety.py` — опасные пути и действия обязаны быть BLOCKED, уровни сайтов и приложений, выполнение (только мок subprocess), журнал, отклонение всего ответа с BLOCKED.
- `test_speech_text.py` — `prepare_for_speech` и вопросы подтверждения.
- `test_installed_apps.py` — поиск приложений на временной папке с `*.app`, `find_apps` / `list_apps`.
- `fixtures_apps.py` — `patch_installed()`: фиксированный список «установленных» приложений для тестов (не `test*.py`, поэтому не запускается сам).
- `verify_local_ai.py` — `AIClient.ask` возвращает dict (успех, не-JSON ответ, все провайдеры недоступны, исключение).
- `test_llm.py` — цепочка провайдеров (429, таймаут, 5xx, 401/404, пустой ответ), пауза 60 с, лимиты, чтение `.env`, ключи не утекают (без сети).
- `test_cache.py` — кэш команд; `test_memory.py` — память, «запомни», секреты, имена, псевдонимы, очистка, права файла.
- `test_tools.py` — цикл инструментов на мок-LLM: SAFE → результат модели, ошибка → честный ответ, BLOCKED, CONFIRM до/после «да», 3 круга, история, режим JSON, флаги и таймауты, `get_time`, промпт.

## Запуск

```bash
brew install portaudio          # нужен для pyaudio
pip install -r requirements.txt
cp .env.example .env            # вписать GEMINI_API_KEY и GROQ_API_KEY
python -m core.llm.check        # связь с провайдерами (--models — список моделей, --provider groq — один,
                                #   --tools — «Который час?» с get_time, 2 запроса на провайдера)
# LM Studio (запасной вариант): загрузить модель → Developer → Start Server (порт 1234)
python main.py
```

Разрешения macOS: «Микрофон» и «Автоматизация» для терминала/IDE, из которого запускается приложение (иначе `osascript` и запись звука молча не работают).

## Тесты

```bash
python -m unittest                    # находит test_*.py
python -m unittest verify_local_ai.py # не подходит под шаблон test*.py, запускать отдельно
```

Тесты мокают `openai.OpenAI`, `subprocess` и микрофон — LM Studio, звук и микрофон не нужны. Лог приложения: `~/Library/Logs/Jarvis/jarvis.log` (в тестах не пишется).

## Правила при изменении кода

- **Безопасность команд.** LLM не выполняет shell — только действия из `ACTIONS` в `core/ai.py`. Новое действие = отдельная функция, которая проверяет аргументы (бросает `ActionError`) и вызывает `_run([...])` со списком аргументов; добавить её в `ACTIONS`, имя — в `safety.ACTION_NAMES`, правило уровня — в `safety.classify`, описание — в системный промпт, тесты — в `test_safety.py`. Никогда не использовать `shell=True` и не подставлять пользовательские строки в текст AppleScript (передавать через `argv`).
- **Уровни риска.** Решает только `core/safety.py`. Удаление — только `trash_file` (Корзина через Finder), никогда `rm`/`os.remove`/`shutil.rmtree`. Пути — через `classify_path` (expanduser + realpath, сравнение без учёта регистра). В тестах `subprocess.run` всегда мок, реальное удаление не запускать.
- **Приложения.** Имя приложения — только через `installed_apps.resolve_app_name` (и при выполнении, и в `safety.classify`), чтобы уровень и действие относились к одному приложению. Тесты, где есть `open_app` / `quit_app`, используют `fixtures_apps.patch_installed()` и не зависят от того, что установлено на Mac.
- **Инструменты.** Новое действие = функция в `ACTIONS` + уровень в `safety` + схема в `core/tools.py` (тест сверяет наборы имён) + строка в `prompts.JSON_FORMAT`. Модели возвращать только `ok` и короткий результат/причину, без ключей и путей вне нужного. Промпт `tools_prompt` держать ≤1200 символов (тест), список приложений в промпт не добавлять — для этого `find_apps` / `list_apps`.
- **Память и кэш.** Запись в память (`remember`, `remember_alias`) — только если «запомни»/«запиши»/«учти» есть в РЕАЛЬНОЙ фразе пользователя (`run_actions(phrase=…)`), никогда по аргументам модели или тексту со страниц. Секреты (пароли, токены, ключи, карты, CVV, паспорт) не запоминать. Имя ассистента попадает в системный промпт — только `valid_assistant_name` (буквы, цифры, пробел, дефис, ≤30). Файлы — только в `DATA_DIR` через `atomic_write_json`, в тестах — `UserMemory()` / `CommandCache()` без пути или временные папки. В кэш — только успешные SAFE-действия из `CACHEABLE_ACTIONS`; смена псевдонимов очищает кэш.
- **Честный результат.** SAFE: сначала выполнить, потом сказать (ответ модели — только при успехе). CONFIRM: вопрос → «Выполняю» → результат; ответ модели до подтверждения не озвучивать. «Готово» — только если все действия успешны, иначе «Не получилось: <ActionError.short>».
- **MacControl.** Перехватывает только короткие фразы (глагол + приложение или команда громкости, плюс «пожалуйста», «мне», «а») и только если команда реально выполнилась; иначе `None` — решает нейросеть.
- **Потоки и Tkinter.** Обработка идёт в фоновых `Thread` и `Timer`. Виджеты не трогать из них напрямую: только `update_status` / `update_log`, которые кладут сообщение в `ui_queue`; виджеты меняет `poll_ui` в главном потоке.
- **Озвучка.** Только через `SpeechEngine.speak` (Lock, одна фраза за раз). Команду собирает `say_command`: текст всегда после `--`, иначе текст, начинающийся с `-`, станет опцией `say` (а без текста `say` зависает, читая stdin).
- **Микрофон.** Цикл не слушает, пока `engine.speaking` установлен, и отбрасывает фразу, если ассистент заговорил во время записи (`speech_overlaps`). Циклы — по флагу/Event (`stop_event`), не `while True`.
- **Логирование.** `logging.getLogger(__name__)`, не `print`. `setup_logging()` вызывается только в `main.py` при запуске.
- **Сопоставление слов.** В `MacControl` и при подтверждении («да»/«нет») сравнивать целые слова, а не подстроки: `"да" in text` срабатывает на «когда», «надо».
- **Язык.** Интерфейс, ответы ассистента, системный промпт и комментарии — на русском.
- **Конфигурация и секреты.** Настройки LLM читаются только из файла `.env` (`dotenv_values`, не из окружения терминала); имена переменных и значения по умолчанию — в `.env.example` (ключи там пустые). Ключи никогда не печатать: ни в консоль, ни в лог, ни в журнал, ни в тесты, ни в сообщения об ошибках — в лог пишется только `describe_error` (класс и HTTP-код). Новый секрет — через `register_secret`. `.env` не коммитить; перед коммитом проверять diff на значения ключей.

## План проекта

План развития — в [ROADMAP.md](ROADMAP.md).

- Работай строго по этапам из ROADMAP.md, по одному этапу за раз, и отмечай выполненные пункты галочками [x].
- После каждого этапа пиши подробный отчёт: что сделано по шагам, какие файлы изменены или созданы, что проверено и чем, что осталось и какие есть риски.
