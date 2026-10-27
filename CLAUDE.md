# CLAUDE.md

Голосовой и текстовый ассистент для macOS (MacBook M1). Python + Tkinter, распознавание речи через SpeechRecognition, озвучка через системную команду `say`, «мозг» — локальная LLM в LM Studio (OpenAI-совместимый API).

## Как устроен поток команды

```
голос (SpeechEngine.listen) / текст (Entry в окне)
        │
        ▼
VoiceApp.process_command            (main.py)
        │
        ├─► MacControl.execute(text)   (modules/mac_control.py)
        │     жёсткие команды по ключевым словам: громкость, mute, браузер, Spotify, калькулятор
        │     вернул строку → озвучить и выйти
        │
        └─► LocalAIClient.ask(text)    (core/ai.py)
              LM Studio http://localhost:1234/v1, модель "local-model"
              ответ — JSON {"response": "...", "actions": [{"name": "open_app", "args": {"app": "Safari"}}]}
              │
              └─ actions  → split_actions → safety.classify (core/safety.py)
                   есть BLOCKED → весь ответ отклонён (и текст модели), «Это слишком опасно…», журнал
                   SAFE    → озвучить response, execute_actions сразу
                             (open_app, set_volume, quit_app кроме CAREFUL_APPS, доверенные сайты)
                   CONFIRM → вопрос голосом, затем таймер 20 с; «да» → execute_actions(confirmed=True)
                             (trash_file, сайты не из trusted_domains.txt, quit_app для CAREFUL_APPS)
```

Если есть отложенные действия, следующая фраза (голос или текст) в `process_command` считается ответом на подтверждение (`parse_confirmation`, целые слова). `execute_actions` перепроверяет уровень прямо перед запуском: BLOCKED не выполняется никогда, CONFIRM — только с `confirmed=True`. Каждое действие (выполнено, отклонено, «нет», таймаут) пишется в `~/Library/Logs/Jarvis/actions.jsonl`.

## Структура

- `main.py` — окно Tkinter (`VoiceApp`), очередь обновлений окна `ui_queue` + `poll_ui`, цикл прослушивания `background_loop` (Старт/Стоп через `stop_event`), подтверждение команд, `normalize_ai_response`.
- `core/engine.py` — `SpeechEngine`: STT через `recognize_google` (ru-RU, онлайн), калибровка шума `calibrate()` один раз при старте; TTS через `say` (голос Milena или другой ru_RU, `pick_voice`), прерывание `stop_speaking()`, флаг `speaking`.
- `core/logger.py` — `setup_logging()`: лог в консоль и в `~/Library/Logs/Jarvis/jarvis.log`.
- `core/ai.py` — белый список действий `ACTIONS` (`open_app`, `quit_app`, `set_volume`, `open_url`, `trash_file`) и `LocalAIClient`: системный промпт, запрос к LM Studio, парсинг JSON, `split_actions` / `execute_actions` / `reject_actions`.
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
- `verify_local_ai.py` — `LocalAIClient.ask` возвращает dict (успех, не-JSON ответ, нет связи с LM Studio).

## Запуск

```bash
brew install portaudio          # нужен для pyaudio
pip install -r requirements.txt
# LM Studio: загрузить модель → Developer → Start Server (порт 1234)
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
- **Честный результат.** SAFE: сначала выполнить, потом сказать (ответ модели — только при успехе). CONFIRM: вопрос → «Выполняю» → результат; ответ модели до подтверждения не озвучивать. «Готово» — только если все действия успешны, иначе «Не получилось: <ActionError.short>».
- **MacControl.** Перехватывает только короткие фразы (глагол + приложение или команда громкости, плюс «пожалуйста», «мне», «а») и только если команда реально выполнилась; иначе `None` — решает нейросеть.
- **Потоки и Tkinter.** Обработка идёт в фоновых `Thread` и `Timer`. Виджеты не трогать из них напрямую: только `update_status` / `update_log`, которые кладут сообщение в `ui_queue`; виджеты меняет `poll_ui` в главном потоке.
- **Озвучка.** Только через `SpeechEngine.speak` (Lock, одна фраза за раз). Команду собирает `say_command`: текст всегда после `--`, иначе текст, начинающийся с `-`, станет опцией `say` (а без текста `say` зависает, читая stdin).
- **Микрофон.** Цикл не слушает, пока `engine.speaking` установлен, и отбрасывает фразу, если ассистент заговорил во время записи (`speech_overlaps`). Циклы — по флагу/Event (`stop_event`), не `while True`.
- **Логирование.** `logging.getLogger(__name__)`, не `print`. `setup_logging()` вызывается только в `main.py` при запуске.
- **Сопоставление слов.** В `MacControl` и при подтверждении («да»/«нет») сравнивать целые слова, а не подстроки: `"да" in text` срабатывает на «когда», «надо».
- **Язык.** Интерфейс, ответы ассистента, системный промпт и комментарии — на русском.
- **Конфигурация и секреты.** Настройки читаются из `.env` через `python-dotenv`. `.env` не коммитить.

## План проекта

План развития — в [ROADMAP.md](ROADMAP.md).

- Работай строго по этапам из ROADMAP.md, по одному этапу за раз, и отмечай выполненные пункты галочками [x].
- После каждого этапа пиши подробный отчёт: что сделано по шагам, какие файлы изменены или созданы, что проверено и чем, что осталось и какие есть риски.
