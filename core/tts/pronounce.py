"""Произношение английских названий для Silero (Этап 9.4).

Silero v5 читает по-русски: латиницу он пропускает молча («Запустил Safari» звучит как «Запустил»). Поэтому перед синтезом
названия приложений и терминов заменяются русской записью. Замена только для движка silero (say и Piper латиницу читают
сами), после `prepare_for_speech` (он уже вызывается в SpeechEngine.speak). Время «17:12» остаётся как есть: на слух
проверено, что читается правильно.

Замена по целым словам без учёта регистра; длинные ключи идут первыми («Apple Music» раньше «Music»). Слова, часть которых
— буквы или цифры («Gmail», «Word2», «Notes2»), не меняются.
"""
import re

PRONUNCIATION = {
    "Telegram": "Телеграм", "WhatsApp": "Ватсап", "Safari": "Сафари", "Chrome": "Хром",
    "Visual Studio Code": "Вижуал Студио Код", "YouTube": "Ютуб", "Spotify": "Спотифай",
    "Calculator": "Калькулятор", "Notes": "Заметки", "Mail": "Почта", "App Store": "Апп Стор", "macOS": "мак о эс",
    "Apple Music": "Эппл Мьюзик",
    "Finder": "Файндер", "Terminal": "Терминал", "Music": "Музыка", "Photos": "Фото", "Messages": "Сообщения",
    "Reminders": "Напоминания", "Calendar": "Календарь", "Preview": "Превью", "TextEdit": "Текстэдит",
    "Claude": "Клод", "ChatGPT": "Чат джи пи ти", "Open-Meteo": "Опен метео", "Wi-Fi": "Вай фай",
    "Bluetooth": "Блютуз", "Excel": "Эксель", "Word": "Ворд",
}

_PATTERNS = [(re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE), spoken)
             for name, spoken in sorted(PRONUNCIATION.items(), key=lambda item: -len(item[0]))]


def apply(text):
    """Текст с русской записью известных английских названий."""
    for pattern, spoken in _PATTERNS:
        text = pattern.sub(spoken, text)
    return text
