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
    "часы": "Clock",
    "фото": "Photos",
    "сообщения": "Messages",
    "файндер": "Finder",
    "финдер": "Finder",
    "spotify": "Spotify",
    "спотифай": "Spotify",
}
ALIAS_PREFIX_MIN = 5  # ключи короче (например «хром») — только точное совпадение

# Как говорят вслух приложения, которых нет в APP_ALIASES (открытие приложений они не меняют). Используются подсказкой
# Whisper (core/stt/prompt.py) и разбором фраз-перечислений («Остальное я не сделал: Ватсап», core/ai.py)
SPOKEN_EXTRAS = (("ватсап", "WhatsApp"), ("вотсап", "WhatsApp"), ("вацап", "WhatsApp"), ("ютуб", "YouTube"),
                 ("хром", "Google Chrome"), ("вайбер", "Viber"), ("зум", "zoom.us"), ("слак", "Slack"),
                 ("спотифай", "Spotify"))
# Варианты названий, которые Whisper пишет латиницей или с лишней буквой
SPOKEN_VARIANTS = {**dict(SPOKEN_EXTRAS), "whatsapp": "WhatsApp", "телеграм": "Telegram", "телеграмм": "Telegram",
                   "telegram": "Telegram", "youtube": "YouTube"}


def spoken_variant(word):
    """«ватсап» / «вацап» / «whatsapp» -> WhatsApp, «телеграмм» -> Telegram, «ютуб» -> YouTube; иначе None."""
    return SPOKEN_VARIANTS.get(" ".join(str(word).lower().replace("ё", "е").split()))


def resolve_app(app):
    """Русское название -> имя в macOS. Падежи: «калькулятора» начинается с «калькулятор»."""
    name = app.lower()
    if name in APP_ALIASES:
        return APP_ALIASES[name]
    prefixes = [k for k in APP_ALIASES if len(k) >= ALIAS_PREFIX_MIN and name.startswith(k)]
    if prefixes:
        return APP_ALIASES[max(prefixes, key=len)]
    return app
