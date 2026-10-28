import logging
import re
import subprocess

from core import journal, mac_actions, safety
from core.errors import ActionError

log = logging.getLogger(__name__)

OPEN_VERBS = {"открой", "открыть", "запусти", "включи"}
CLOSE_VERBS = {"закрой", "закрыть", "выйди", "выключи"}
FILLER_WORDS = {"пожалуйста", "мне", "а"}
# Громкость перехватываем, только если во фразе нет ничего, кроме этих слов
VOLUME_WORDS = {"сделай", "звук", "громкость", "громче", "погромче", "тише", "потише", "немного", "чуть",
                "ещё", "еще", "выключи", "включи"} | FILLER_WORDS

# Быстрые правила без модели: фраза целиком (после «пожалуйста», «мне», «а») совпадает с одной из записей.
# Лишнее слово — не наше, решает нейросеть. «ё» приравнена к «е».
# (фразы, инструмент, аргументы); app="" — приложение выбирается среди запущенных (Spotify / Music)
QUICK_RULES = [
    (["какой заряд", "сколько заряда", "процент зарядки"], "system_info", {"what": "battery"}),
    (["поставь на паузу", "пауза"], "media", {"action": "pause", "app": ""}),
    (["продолжи", "играй"], "media", {"action": "play", "app": ""}),
    (["следующий трек", "следующая песня"], "media", {"action": "next", "app": ""}),
    (["предыдущий трек", "предыдущая песня"], "media", {"action": "previous", "app": ""}),
    (["что играет"], "media", {"action": "now_playing", "app": ""}),
    (["сделай скриншот", "скриншот"], "display", {"action": "screenshot", "value": ""}),
    (["заблокируй экран", "заблокируй компьютер"], "display", {"action": "lock_screen", "value": ""}),
    (["включи тёмную тему"], "display", {"action": "dark_mode_on", "value": ""}),
    (["выключи тёмную тему"], "display", {"action": "dark_mode_off", "value": ""}),
]


def _normalize(text):
    return " ".join(w for w in re.findall(r"\w+", text.lower().replace("ё", "е")) if w not in FILLER_WORDS)


QUICK_PHRASES = {_normalize(phrase): (name, args) for phrases, name, args in QUICK_RULES for phrase in phrases}

# (слова приложения, команда, ответ, действие для журнала)
APPS = [
    ({"калькулятор"}, ["open", "-a", "Calculator"], "Калькулятор открыт",
     {"name": "open_app", "args": {"app": "Calculator"}}),
    ({"спотифай", "музыка", "музыку"}, ["open", "-a", "Spotify"], "Открываю музыку",
     {"name": "open_app", "args": {"app": "Spotify"}}),
    # open с URL открывает браузер по умолчанию
    ({"браузер", "интернет"}, ["open", "https://www.google.com"], "Открываю браузер",
     {"name": "open_url", "args": {"url": "https://www.google.com"}}),
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


def _journal(phrase, action, ok, result, level=None):
    """Быстрые команды идут мимо модели и run_actions — пишем в журнал действий сами."""
    journal.record(phrase, action, level or safety.classify(action)[0], None, ok, result)


class MacControl:
    def execute(self, command):
        """Только короткие простые фразы. Всё остальное (и любая ошибка старых команд) — None: решает нейросеть."""
        quick = self._quick(command)
        if quick is not None:
            return quick

        # Сравниваем целые слова: «закрой калькулятор» не должно открывать калькулятор
        words = set(re.findall(r"\w+", command.lower()))

        volume_command = None
        if words & {"громче", "погромче"}:
            # AppleScript: set volume output volume (current + 10)
            volume_command = (["osascript", "-e", "set volume output volume (output volume of (get volume settings) + 10)"],
                              "Сделал громче", "up")
        elif words & {"тише", "потише"}:
            volume_command = (["osascript", "-e", "set volume output volume (output volume of (get volume settings) - 10)"],
                              "Сделал тише", "down")
        elif {"выключи", "звук"} <= words:
            volume_command = (["osascript", "-e", "set volume output muted true"], "Звук выключен", "mute")
        elif {"включи", "звук"} <= words:
            volume_command = (["osascript", "-e", "set volume output muted false"], "Звук включен", "unmute")

        if volume_command:
            if not words <= VOLUME_WORDS:
                return None  # «открой спотифай и выключи звук» — целиком в нейросеть
            args, reply, direction = volume_command
            ok = _run(args)
            _journal(command, {"name": "quick_volume", "args": {"direction": direction}}, ok,
                     reply if ok else "не удалось", level=safety.SAFE)
            return reply if ok else None

        for app_words, args, reply, action in APPS:
            if not words & app_words:
                continue
            # Закрытие — через нейросеть (quit_app с подтверждением); без глагола открытия — тоже не перехватываем
            if words & CLOSE_VERBS or not words & OPEN_VERBS:
                return None
            # Только «глагол + приложение (+ пожалуйста/мне/а)»: «открой YouTube и найди музыка» — не наше
            if words - OPEN_VERBS - app_words - FILLER_WORDS:
                return None
            ok = _run(args)
            _journal(command, action, ok, reply if ok else "не удалось")
            return reply if ok else None

        return None

    def _quick(self, command):
        """Новые быстрые правила (система, экран, музыка). Фраза однозначная, поэтому при сбое озвучиваем
        причину (нет разрешения, не запущено), а не отдаём фразу модели: она бы упёрлась в то же самое."""
        rule = QUICK_PHRASES.get(_normalize(command))
        if rule is None:
            return None
        name, args = rule
        args = dict(args)
        try:
            if name == "media":
                app = mac_actions.pick_media_app()
                if app is None:
                    return None  # плеер не запущен — пусть решает нейросеть (какое приложение включить)
                args["app"] = app
            action = {"name": name, "args": args}
            level, reason = safety.classify(action)
            if level != safety.SAFE:
                _journal(command, action, False, f"не выполнено: {reason}", level=level)
                return None
            try:
                done = mac_actions.TOOL_FUNCS[name](**args)
            except ActionError as e:
                _journal(command, action, False, f"⛔ Отклонено: {e}", level=level)
                return e.reply or f"Не получилось: {e.short}."
            _journal(command, action, True, done.message, level=level)
            return done.speech
        except ActionError as e:  # pick_media_app не должен падать, но голосом лучше не молчать
            log.warning("Быстрая команда %s: %s", name, e)
            return None
