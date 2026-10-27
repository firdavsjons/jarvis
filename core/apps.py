# Русские названия (нижний регистр) -> имена приложений в macOS
APP_ALIASES = {
    "калькулятор": "Calculator",
    "сафари": "Safari",
    "телеграм": "Telegram",
    "хром": "Google Chrome",
    "заметки": "Notes",
    "музыка": "Music",
    "почта": "Mail",
    "календарь": "Calendar",
    "настройки": "System Settings",
    "терминал": "Terminal",
    "фото": "Photos",
    "сообщения": "Messages",
    "файндер": "Finder",
    "финдер": "Finder",
    "spotify": "Spotify",
    "спотифай": "Spotify",
}
ALIAS_PREFIX_MIN = 5  # ключи короче (например «хром») — только точное совпадение


def resolve_app(app):
    """Русское название -> имя в macOS. Падежи: «калькулятора» начинается с «калькулятор»."""
    name = app.lower()
    if name in APP_ALIASES:
        return APP_ALIASES[name]
    prefixes = [k for k in APP_ALIASES if len(k) >= ALIAS_PREFIX_MIN and name.startswith(k)]
    if prefixes:
        return APP_ALIASES[max(prefixes, key=len)]
    return app
