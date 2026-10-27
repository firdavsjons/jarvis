import logging
import re
import subprocess

log = logging.getLogger(__name__)

OPEN_VERBS = {"открой", "открыть", "запусти", "включи"}
CLOSE_VERBS = {"закрой", "закрыть", "выйди", "выключи"}
FILLER_WORDS = {"пожалуйста", "мне", "а"}
# Громкость перехватываем, только если во фразе нет ничего, кроме этих слов
VOLUME_WORDS = {"сделай", "звук", "громкость", "громче", "погромче", "тише", "потише", "немного", "чуть",
                "ещё", "еще", "выключи", "включи"} | FILLER_WORDS

# (слова приложения, команда, ответ)
APPS = [
    ({"калькулятор"}, ["open", "-a", "Calculator"], "Калькулятор открыт"),
    ({"спотифай", "музыка", "музыку"}, ["open", "-a", "Spotify"], "Открываю музыку"),
    # open с URL открывает браузер по умолчанию
    ({"браузер", "интернет"}, ["open", "https://www.google.com"], "Открываю браузер"),
]


def _run(args):
    """Запускает команду списком аргументов, без shell. True — если команда завершилась успешно."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        log.error("❌ Ошибка команды %s: %s", args, e)
        return False
    if result.returncode != 0:
        # stderr — в лог (например, «Unable to find application named 'Spotify'»), не прямо в терминал
        log.warning("Команда %s завершилась с кодом %s: %s", args, result.returncode, (result.stderr or "").strip())
        return False
    return True


class MacControl:
    def execute(self, command):
        """Только короткие простые фразы. Всё остальное (и любая ошибка) — None: решает нейросеть."""
        # Сравниваем целые слова: «закрой калькулятор» не должно открывать калькулятор
        words = set(re.findall(r"\w+", command.lower()))

        volume_command = None
        if words & {"громче", "погромче"}:
            # AppleScript: set volume output volume (current + 10)
            volume_command = (["osascript", "-e", "set volume output volume (output volume of (get volume settings) + 10)"],
                              "Сделал громче")
        elif words & {"тише", "потише"}:
            volume_command = (["osascript", "-e", "set volume output volume (output volume of (get volume settings) - 10)"],
                              "Сделал тише")
        elif {"выключи", "звук"} <= words:
            volume_command = (["osascript", "-e", "set volume output muted true"], "Звук выключен")
        elif {"включи", "звук"} <= words:
            volume_command = (["osascript", "-e", "set volume output muted false"], "Звук включен")

        if volume_command:
            if not words <= VOLUME_WORDS:
                return None  # «открой спотифай и выключи звук» — целиком в нейросеть
            args, reply = volume_command
            return reply if _run(args) else None

        for app_words, args, reply in APPS:
            if not words & app_words:
                continue
            # Закрытие — через нейросеть (quit_app с подтверждением); без глагола открытия — тоже не перехватываем
            if words & CLOSE_VERBS or not words & OPEN_VERBS:
                return None
            # Только «глагол + приложение (+ пожалуйста/мне/а)»: «открой YouTube и найди музыка» — не наше
            if words - OPEN_VERBS - app_words - FILLER_WORDS:
                return None
            return reply if _run(args) else None

        return None
