import logging
import re
import subprocess

log = logging.getLogger(__name__)

OPEN_VERBS = {"открой", "открыть", "запусти", "включи"}
CLOSE_VERBS = {"закрой", "закрыть", "выйди", "выключи"}

# (слова приложения, команда, ответ)
APPS = [
    ({"калькулятор"}, ["open", "-a", "Calculator"], "Калькулятор открыт"),
    ({"спотифай", "музыка", "музыку"}, ["open", "-a", "Spotify"], "Открываю музыку"),
    # open с URL открывает браузер по умолчанию
    ({"браузер", "интернет"}, ["open", "https://www.google.com"], "Открываю браузер"),
]


def _run(args):
    """Запускает команду списком аргументов, без shell."""
    try:
        subprocess.run(args, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        log.error("❌ Ошибка команды %s: %s", args, e)


class MacControl:
    def execute(self, command):
        # Сравниваем целые слова: «закрой калькулятор» не должно открывать калькулятор
        words = set(re.findall(r"\w+", command.lower()))

        if words & {"громче", "погромче"}:
            # AppleScript: set volume output volume (current + 10)
            _run(["osascript", "-e", "set volume output volume (output volume of (get volume settings) + 10)"])
            return "Сделал громче"

        elif words & {"тише", "потише"}:
            _run(["osascript", "-e", "set volume output volume (output volume of (get volume settings) - 10)"])
            return "Сделал тише"

        elif {"выключи", "звук"} <= words:
            _run(["osascript", "-e", "set volume output muted true"])
            return "Звук выключен"

        elif {"включи", "звук"} <= words:
            _run(["osascript", "-e", "set volume output muted false"])
            return "Звук включен"

        for app_words, args, reply in APPS:
            if not words & app_words:
                continue
            # Закрытие — через нейросеть (quit_app с подтверждением); без глагола открытия — тоже не перехватываем
            if words & CLOSE_VERBS or not words & OPEN_VERBS:
                return None
            _run(args)
            return reply

        return None
