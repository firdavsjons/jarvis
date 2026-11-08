# Jarvis — голосовой ассистент для macOS

Голосовой и текстовый ассистент для Mac на Python и Tkinter. Вы говорите «Джарвис, открой калькулятор» или «Джарвис, какая сегодня погода», а он выполняет команду и отвечает вслух. Распознавание речи и озвучка работают локально, «мозг» — облачные LLM с запасным вариантом.

> Проект для macOS (разрабатывался на MacBook M1). Язык интерфейса и команд — русский.

## Возможности

- **Голос и текст.** Окно с кнопками «Старт» и «Стоп» и полем текстового ввода.
- **Слово-активатор «Джарвис».** Без обращения фразы молча отбрасываются. После ответа открывается короткое окно диалога, в нём слово не нужно.
- **Локальное распознавание речи.** Whisper через `whisper.cpp` (основной), `faster-whisper` (запасной) или Google. Звук остаётся на Mac, если выбрана локальная модель.
- **Озвучка.** Системный голос `say` (по умолчанию), нейронные голоса Piper (sherpa-onnx) и Silero в отдельном процессе.
- **Несколько LLM-провайдеров.** Gemini и Groq через OpenAI-совместимый API; при ошибке или исчерпанном лимите включается следующий. Локальная модель в LM Studio — по желанию.
- **Управление Mac.** Открытие и закрытие приложений, громкость, Spotify и медиа, скриншот, блокировка экрана, тёмная тема, таймеры, будильники, напоминания, календарь, заметки.
- **Интернет без ключей.** Погода (Open-Meteo), курс валют, поиск (ddgs), чтение страниц.
- **Сообщения в Telegram.** Jarvis открывает чат, вставляет черновик, спрашивает подтверждение и отправляет только после «да».
- **Память и кэш команд.** Факты о пользователе, история диалога, кэш повторяющихся команд.

## Безопасность

Каждое действие относится к одному из трёх уровней риска:

| Уровень | Что происходит | Примеры |
|---|---|---|
| `SAFE` | Выполняется сразу | открыть приложение, громкость, погода, время |
| `CONFIRM` | Выполняется после ответа «да» | удаление файла в корзину, сайты не из `trusted_domains.txt`, отправка сообщения |
| `BLOCKED` | Не выполняется никогда | любые действия вне белого списка |

Команды запускаются списком аргументов с таймаутом, без `shell=True`. Ключи и пароли скрываются в логах. Журнал действий пишется в `~/Library/Logs/Jarvis/actions.jsonl`. Данные из интернета и заметок считаются данными, а не инструкциями: после них опасные действия блокируются.

## Стек

- Python, Tkinter
- SpeechRecognition + PyAudio (запись), Whisper (`whisper.cpp` / `faster-whisper`) для распознавания
- OpenAI-совместимый клиент (Gemini, Groq, LM Studio)
- `httpx`, `ddgs`, `python-dotenv`
- Озвучка: `say`, Piper (sherpa-onnx), Silero
- Тесты: `unittest`

## Установка и запуск

Нужен Mac с Python 3 и Homebrew.

```bash
git clone https://github.com/firdavsjons/jarvis.git
cd jarvis

python3 -m venv .venv
source .venv/bin/activate

brew install portaudio                     # нужен для pyaudio
python -m pip install -r requirements.txt

cp .env.example .env                       # впишите GEMINI_API_KEY и GROQ_API_KEY
```

Ключи получите бесплатно: [Gemini](https://aistudio.google.com/apikey) и [Groq](https://console.groq.com/keys). Файл `.env` в репозиторий не попадает.

Проверка связи с провайдерами и запуск:

```bash
python -m core.llm.check                   # проверка связи с LLM
python main.py                             # запуск приложения
```

Разрешения macOS: для терминала или IDE, из которых запускается Jarvis, включите «Микрофон» и «Автоматизация». Без них запись звука и `osascript` молча не работают.

Локальное распознавание речи и нейронные голоса ставятся вручную. Пока они не установлены, Jarvis работает на Google и `say`:

- [docs/stt.md](docs/stt.md) — Whisper, приватность звука, проверка `python -m core.stt.check`
- [docs/tts.md](docs/tts.md) — озвучка, Piper и Silero
- [docs/telegram.md](docs/telegram.md) — контакты и сообщения в Telegram
- [docs/shortcuts.md](docs/shortcuts.md) — быстрые команды

## Настройки (`.env`)

Все настройки описаны в [.env.example](.env.example). Основные:

| Переменная | Назначение |
|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | ключи облачных моделей |
| `LLM_PROVIDERS` | порядок провайдеров, например `gemini,groq` |
| `STT_ENGINE` | `whisper_cpp`, `faster_whisper` или `google` |
| `TTS_ENGINE` | `say`, `sherpa` или `silero` |
| `WAKE_WORD` | `on` / `off` — слово-активатор |
| `DEFAULT_CITY` | город для погоды |

## Примеры команд

- «Джарвис, открой Safari»
- «Джарвис, сделай громкость пятьдесят»
- «Джарвис, какая погода в Душанбе»
- «Джарвис, поставь таймер на десять минут»
- «Джарвис, напиши маме в телеграм, что задерживаюсь» — Jarvis повторит текст и дождётся «да»

## Структура проекта

```text
main.py            окно Tkinter, цикл прослушивания, подтверждение команд
core/
  ai.py            AIClient: правила, цикл инструментов, ответы
  llm/             провайдеры (Gemini, Groq, LM Studio), резервирование
  stt/             распознавание речи
  tts/             озвучка
  apps/            приложения Mac: календарь, заметки, напоминания, Telegram…
  safety.py        уровни риска SAFE / CONFIRM / BLOCKED
  wake.py          слово-активатор
  web.py           погода, курсы, поиск
  memory.py        память о пользователе
modules/           быстрые команды управления Mac
docs/              подробные инструкции
test_*.py          unit-тесты
```

Подробное описание архитектуры — в [CLAUDE.md](CLAUDE.md), план развития — в [ROADMAP.md](ROADMAP.md).

## Тесты

```bash
python -m unittest                         # находит test_*.py
python -m unittest verify_local_ai.py      # отдельная проверка
```

В репозитории 44 файла с тестами. Тесты не запускают настоящие процессы и не пишут в домашние папки.

## Лицензии моделей

Модели голоса Silero распространяются по CC BY-NC-SA 4.0, только для личного некоммерческого использования. Сами модели в репозиторий не входят.

---

## English summary

**Jarvis** is a voice and text assistant for macOS written in Python (Tkinter). It listens for the wake word "Джарвис", transcribes speech locally with Whisper, asks cloud LLMs (Gemini, Groq, or a local LM Studio model) what to do, and speaks the answer. Actions are classified as `SAFE`, `CONFIRM` or `BLOCKED`, so risky commands need an explicit "yes". It can control apps, volume and media, set timers and reminders, fetch weather and search results, and draft Telegram messages.

Quick start: `brew install portaudio`, `python -m pip install -r requirements.txt`, copy `.env.example` to `.env`, add your Gemini and Groq API keys, then run `python main.py`. Run tests with `python -m unittest`.
