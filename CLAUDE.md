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
              ├─ response → SpeechEngine.speak
              └─ actions  → split_actions
                   open_app, set_volume       → execute_actions сразу
                   quit_app, open_url         → pending_actions, ждут «да»/«нет» 15 с (голосом или текстом)
                   всё вне белого списка ACTIONS → отклоняется и пишется в лог
```

Если есть отложенные действия, следующая фраза (голос или текст) в `process_command` считается ответом на подтверждение (`parse_confirmation`, целые слова).

## Структура

- `main.py` — окно Tkinter (`VoiceApp`), очередь обновлений окна `ui_queue` + `poll_ui`, цикл прослушивания `background_loop` (Старт/Стоп через `stop_event`), подтверждение команд, `normalize_ai_response`.
- `core/engine.py` — `SpeechEngine`: STT через `recognize_google` (ru-RU, онлайн), калибровка шума `calibrate()` один раз при старте; TTS через `say` (голос Milena или другой ru_RU, `pick_voice`), прерывание `stop_speaking()`, флаг `speaking`.
- `core/logger.py` — `setup_logging()`: лог в консоль и в `~/Library/Logs/Jarvis/jarvis.log`.
- `core/ai.py` — белый список действий `ACTIONS` (`open_app`, `quit_app`, `set_volume`, `open_url`) и `LocalAIClient`: системный промпт, запрос к LM Studio, парсинг JSON, `split_actions` / `execute_actions`.
- `modules/mac_control.py` — `MacControl`: быстрые команды через `osascript` / `open` (subprocess со списком аргументов).
- `test_json_mode.py` — парсинг JSON, белый список, проверка аргументов, подтверждение по целым словам.
- `test_main.py` — `parse_confirmation`, `normalize_ai_response`, выбор голоса, `say` с текстом после `--`, прерывание речи.
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

- **Безопасность команд.** LLM не выполняет shell — только действия из `ACTIONS` в `core/ai.py`. Новое действие = отдельная функция, которая проверяет аргументы (бросает `ActionError`) и вызывает `_run([...])` со списком аргументов; добавить её в `ACTIONS` с флагом подтверждения и описать в системном промпте. Никогда не использовать `shell=True` и не подставлять пользовательские строки в текст AppleScript (передавать через `argv`). Всё, что закрывает, удаляет или открывает внешнее, — с подтверждением.
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
