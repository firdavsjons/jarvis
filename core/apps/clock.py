"""Таймеры и будильники: внутренние (core/scheduler.py) и в приложении «Часы» через Быстрые команды.

Приложение «Часы» не скриптуется, поэтому будильник и таймер в нём — только через Быстрые команды
«Jarvis Alarm» и «Jarvis Timer» (docs/shortcuts.md). Внутренние срабатывают, только пока Jarvis запущен;
«разбуди меня» — надёжный будильник: напоминание в приложении «Напоминания», оно сработает и без Jarvis.
"""
import re
from datetime import datetime

from core import durations, scheduler
from core.apps import common, reminders, shortcuts
from core.apps.registry import SAFE, AppTool, words_of
from core.errors import ActionError

ALARM_VOICE_NOTE = "Он сработает, только если Jarvis запущен."
CLOCK_SETUP = ("В приложении Часы это делается через Быстрые команды. Создайте команду {name}, "
               "инструкция в файле docs/shortcuts.md.")
INTERNAL_OFFER = "Могу поставить внутренний {what} Jarvis, он сработает, только если Jarvis запущен."
CLOCK_APP_RE = re.compile(r"(?:\b(?:в|во)\s+(?:приложени\w+\s+)?час(?:ах|ы)\b|\bприложени\w+\s+час(?:ы|ов|ах)\b)",
                          re.IGNORECASE)
WAKE_WORDS = ("разбуди", "разбудите", "подними")
_MAX_CLOCK_HOURS = 24


def wants_clock_app(phrase):
    return bool(CLOCK_APP_RE.search(str(phrase)))


def wants_reliable_alarm(phrase):
    return any(w.startswith(WAKE_WORDS) for w in words_of(phrase))


def _clock_shortcut(name, what, text):
    """Запуск Jarvis Alarm / Jarvis Timer. Нет в списке SAFE или в приложении — честный отказ и предложение."""
    reasons, installed = [], None
    if shortcuts.load_allowed().get(name) != SAFE:
        reasons.append("нет в списке разрешённых")
    else:
        try:
            installed = shortcuts.installed_shortcuts()
        except ActionError:
            reasons.append("список недоступен")
        else:
            if name not in installed:
                reasons.append("нет в приложении")
    if reasons:
        reply = CLOCK_SETUP.format(name=name) + " " + INTERNAL_OFFER.format(what=what)
        raise ActionError(f"{name}: {reasons[0]}", CLOCK_SETUP.format(name=name).rstrip("."), reply)
    shortcuts.run_shortcut(name, text, installed=installed)


# --- timer ---

def _check_seconds(seconds):
    if isinstance(seconds, bool):
        raise ActionError(f"seconds: {seconds!r}", "секунды должны быть числом")
    try:
        value = int(str(seconds).strip())
    except (TypeError, ValueError):
        raise ActionError(f"seconds: {seconds!r}", "секунды должны быть числом")
    if not 1 <= value <= durations.MAX_SECONDS:
        raise ActionError(f"seconds вне 1–{durations.MAX_SECONDS}: {value}", "таймер от 1 секунды до 24 часов")
    return value


def _timer_label(item):
    return f"«{item['label']}»" if item["label"] else "без названия"


def _timer_start(sched, seconds, label, phrase):
    seconds = _check_seconds(seconds)
    label = common.clean_text(label, "название", common.MAX_LABEL, required=False)
    if wants_clock_app(phrase):
        _clock_shortcut("Jarvis Timer", "таймер", str(seconds))
        return common.done(f"Таймер в приложении Часы на {durations.duration_phrase(seconds)} запущен")
    sched.add("timer", sched.now() + seconds, label)
    name = f"Таймер «{label}»" if label else "Таймер"
    return common.done(f"{name} на {durations.duration_phrase(seconds)} запущен")


def _timers_text(sched, items):
    if not items:
        return "Активных таймеров нет"
    if len(items) == 1:
        item = items[0]
        left = durations.duration_phrase(round(sched.remaining(item)))
        return f"Осталось {left}" + (f", таймер «{item['label']}»" if item["label"] else "")
    shown = [f"{_timer_label(i)} — {durations.duration_phrase(round(sched.remaining(i)))}" for i in items[:3]]
    rest = len(items) - len(shown)
    return f"Таймеров {len(items)}: " + "; ".join(shown) + (f" и ещё {rest}" if rest else "")


def _timer_cancel(sched, label):
    label = common.clean_text(label, "название", common.MAX_LABEL, required=False)
    items = sched.items("timer")
    if not items:
        raise ActionError("нет таймеров", "активных таймеров нет")
    if label:
        key = label.casefold()
        found = [i for i in items if i["label"].casefold() == key] or [i for i in items if key in i["label"].casefold()]
        if not found:
            raise ActionError(f"таймер {label!r} не найден", "не нашёл такой таймер")
        if len(found) > 1:
            raise ActionError("несколько таймеров с таким названием", "таких таймеров несколько, уточните")
        target = found[0]
    elif len(items) == 1:
        target = items[0]
    else:
        raise ActionError("несколько таймеров", "таймеров несколько, назовите название")
    sched.remove(target["id"])
    return common.done("Таймер отменён" + (f": {target['label']}" if target["label"] else ""))


def timer(action, seconds, label, phrase=""):
    sched = scheduler.get_scheduler()
    if action == "start":
        return _timer_start(sched, seconds, label, phrase)
    if action in ("status", "list"):
        return common.done(_timers_text(sched, sched.items("timer")))
    if action == "cancel":
        return _timer_cancel(sched, label)
    raise ActionError(f"timer: неизвестное действие {action!r}", "неизвестное действие")


def _timer_quick(phrase):
    """«поставь таймер на пять минут», «таймер на полчаса», «сколько осталось», «отмени таймер»."""
    words = [w for w in words_of(phrase) if w not in {"пожалуйста", "мне", "а"}]
    text = " ".join(words)
    if text in ("сколько осталось", "сколько времени осталось", "сколько осталось до таймера"):
        return {"args": {"action": "status", "seconds": 0, "label": ""}}
    if text in ("отмени таймер", "отмените таймер", "останови таймер", "отмена таймера"):
        # Несколько таймеров — какой отменить, решает модель по названию
        return {"args": {"action": "cancel", "seconds": 0, "label": ""}} \
            if len(scheduler.get_scheduler().items("timer")) <= 1 else None
    verbs = {"поставь", "поставить", "запусти", "включи", "сделай", "установи", "заведи"}
    rest = [w for w in words if w not in verbs]
    if "таймер" not in rest:
        return None
    rest.remove("таймер")
    if rest and rest[0] == "на":
        rest = rest[1:]
    seconds = durations.parse_duration(" ".join(rest)) if rest else None
    return {"args": {"action": "start", "seconds": seconds, "label": ""}} if seconds else None


# --- alarm ---

def _alarm_set(sched, when, label, phrase):
    when_dt = common.parse_when(when, field="when")
    label = common.clean_text(label, "название", common.MAX_LABEL, required=False)
    spoken = common.when_text(when_dt)
    if wants_clock_app(phrase):
        if (when_dt - datetime.now()).total_seconds() > _MAX_CLOCK_HOURS * 3600:
            raise ActionError("будильник Часов дальше 24 часов", "в приложении Часы будильник ставится на ближайшие сутки")
        _clock_shortcut("Jarvis Alarm", "будильник", f"{common.time_text(when_dt)} {label}".strip())
        return common.done(f"Будильник в приложении Часы на {spoken} создан")
    if wants_reliable_alarm(phrase):
        # «Разбуди меня» — надёжно: напоминание в приложении «Напоминания», сработает и без Jarvis
        reminders.add_reminder(label or "Пора вставать", when_dt, "")
        return common.done(f"Создал напоминание на {common.time_text(when_dt)}, оно сработает и без меня",
                           f"Создал напоминание на {spoken}, оно сработает и без меня.")
    sched.add("alarm", when_dt.timestamp(), label)
    return common.done(f"Будильник на {spoken}. {ALARM_VOICE_NOTE}")


def alarm(action, when, label, phrase=""):
    sched = scheduler.get_scheduler()
    if action == "set":
        return _alarm_set(sched, when, label, phrase)
    if action == "list":
        items = sched.items("alarm")
        if not items:
            return common.done("Активных будильников нет")
        shown = [common.when_text(datetime.fromtimestamp(i["due"])) + (f" «{i['label']}»" if i["label"] else "")
                 for i in items[:5]]
        return common.done(f"Будильников {len(items)}: " + "; ".join(shown))
    if action == "cancel":
        label = common.clean_text(label, "название", common.MAX_LABEL, required=False)
        items = sched.items("alarm")
        if not items:
            raise ActionError("нет будильников", "активных будильников нет")
        key = label.casefold()
        found = [i for i in items if key in i["label"].casefold()] if label else items
        if not found:
            raise ActionError(f"будильник {label!r} не найден", "не нашёл такой будильник")
        if len(found) > 1:
            raise ActionError("несколько будильников", "будильников несколько, назовите название или время")
        sched.remove(found[0]["id"])
        return common.done("Будильник отменён")
    raise ActionError(f"alarm: неизвестное действие {action!r}", "неизвестное действие")


TOOLS = [
    AppTool(
        name="timer",
        description="Таймер на секунды. start — запустить, status — сколько осталось, cancel — отменить, list — все.",
        topic="personal",
        keywords=("таймер", "засеки", "секундомер", "осталось", "через"),
        actions={"start": SAFE, "status": SAFE, "cancel": SAFE, "list": SAFE},
        params={"seconds": {"type": "integer", "description": "start: секунды 1–86400; иначе 0"},
                "label": {"type": "string", "description": "Название таймера или пустая строка"}},
        run=timer,
        text_params=("label",),
        creates=frozenset({"start"}),
        quick=_timer_quick,
    ),
    AppTool(
        name="alarm",
        description="Будильник Jarvis (работает, пока Jarvis запущен). «Разбуди» тоже сюда: создаст напоминание.",
        topic="personal",
        keywords=("будильн", "разбуди", "разбудите", "проснуться", "подъём", "подъем"),
        actions={"set": SAFE, "cancel": SAFE, "list": SAFE},
        params={"when": {"type": "string", "description": "set: ГГГГ-ММ-ДД ЧЧ:ММ по строке «Сейчас»; иначе пусто"},
                "label": {"type": "string", "description": "Подпись или пустая строка"}},
        run=alarm,
        text_params=("label",),
        creates=frozenset({"set"}),
    ),
]
