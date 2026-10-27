# CLAUDE.md

Голосовой и текстовый ассистент для macOS (MacBook M1). Python + Tkinter, распознавание речи через SpeechRecognition, озвучка через pyttsx3, «мозг» — локальная LLM в LM Studio (OpenAI-совместимый API).

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

- `main.py` — окно Tkinter (`VoiceApp`), фоновый цикл прослушивания `background_loop`, логика подтверждения команд.
- `core/engine.py` — `SpeechEngine`: STT через `recognize_google` (ru-RU, онлайн), TTS через pyttsx3 с драйвером `nsss`.
- `core/ai.py` — белый список действий `ACTIONS` (`open_app`, `quit_app`, `set_volume`, `open_url`) и `LocalAIClient`: системный промпт, запрос к LM Studio, парсинг JSON, `split_actions` / `execute_actions`.
- `modules/mac_control.py` — `MacControl`: быстрые команды через `osascript` / `open` (subprocess со списком аргументов).
- `test_json_mode.py` — рабочие unit-тесты: парсинг JSON, белый список, проверка аргументов, подтверждение по целым словам (с моками).
- Устаревшее, не работает: `verify_local_ai.py` (ожидает строку, а `ask` возвращает dict), `test_gemini_fix.py`, `test_model_latest.py`, `list_models.py` (ссылаются на удалённый `GeminiClient` / Gemini API), `check_env.py` (отладочный скрипт).

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
python -m unittest test_json_mode.py
```

Тесты мокают `openai.OpenAI` и `subprocess.run` — LM Studio и микрофон не нужны.

## Правила при изменении кода

- **Безопасность команд.** LLM не выполняет shell — только действия из `ACTIONS` в `core/ai.py`. Новое действие = отдельная функция, которая проверяет аргументы (бросает `ActionError`) и вызывает `_run([...])` со списком аргументов; добавить её в `ACTIONS` с флагом подтверждения и описать в системном промпте. Никогда не использовать `shell=True` и не подставлять пользовательские строки в текст AppleScript (передавать через `argv`). Всё, что закрывает, удаляет или открывает внешнее, — с подтверждением.
- **Потоки и Tkinter.** Обработка идёт в фоновых `Thread`. Виджеты Tkinter менять только из главного потока — через `root.after(0, ...)`, а не напрямую из потока.
- **TTS.** `pyttsx3.runAndWait()` на macOS не потокобезопасен: не вызывать `speak` одновременно из нескольких потоков (нужен Lock или `say` через subprocess).
- **Сопоставление слов.** В `MacControl` и при подтверждении («да»/«нет») сравнивать целые слова, а не подстроки: `"да" in text` срабатывает на «когда», «надо».
- **Язык.** Интерфейс, ответы ассистента, системный промпт и комментарии — на русском.
- **Конфигурация и секреты.** Настройки читаются из `.env` через `python-dotenv`. `.env` не коммитить.

## План проекта

План развития — в [ROADMAP.md](ROADMAP.md).

- Работай строго по этапам из ROADMAP.md, по одному этапу за раз, и отмечай выполненные пункты галочками [x].
- После каждого этапа пиши подробный отчёт: что сделано по шагам, какие файлы изменены или созданы, что проверено и чем, что осталось и какие есть риски.
