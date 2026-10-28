"""Приложение «Напоминания»: add, list (до 10 невыполненных), complete, delete (с подтверждением).

Всё через osascript, строки и числа — только через argv. Дата собирается в AppleScript из частей (год, месяц, день,
часы, минуты), без разбора локализованных строк. Нужны: Автоматизация → Напоминания и доступ к Напоминаниям.
"""
from datetime import datetime

from core.apps import common
from core.apps.registry import CONFIRM, SAFE, AppTool
from core.errors import ActionError

APP = "Reminders"

_ADD = [*common.MAKE_DATE,
        "on run argv",
        "set theTitle to item 1 of argv",
        "set hasDate to (item 2 of argv) is \"1\"",
        "set listName to item 8 of argv",
        'tell application "Reminders"',
        "if listName is \"\" then",
        "set theList to default list",
        "else",
        "set theList to list listName",
        "end if",
        "tell theList to set r to make new reminder with properties {name:theTitle}",
        "if hasDate then",
        "set d to my makeDate((item 3 of argv) as integer, (item 4 of argv) as integer, (item 5 of argv) as integer, "
        "(item 6 of argv) as integer, (item 7 of argv) as integer)",
        "set due date of r to d",
        "set remind me date of r to d",
        "end if",
        "end tell",
        "end run"]

_LIST = [*common.SPLIT_DATE,
         "on run argv",
         "set maxCount to (item 1 of argv) as integer",
         "set out to {}",
         'tell application "Reminders"',
         "set pending to (reminders whose completed is false)",
         "repeat with r in pending",
         "if (count of out) >= maxCount then exit repeat",
         "set d to \"\"",
         "try",
         "set d to my splitDate(due date of r)",
         "end try",
         "set end of out to (name of r) & tab & d",
         "end repeat",
         "end tell",
         "set AppleScript's text item delimiters to linefeed",
         "return out as text",
         "end run"]

# Совпадения по названию: «count» — сколько невыполненных с таким названием; action: "check" | "complete" | "delete"
_MATCH = ["on run argv",
          "set theTitle to item 1 of argv",
          "set mode to item 2 of argv",
          'tell application "Reminders"',
          "set found to (reminders whose name is theTitle and completed is false)",
          "set n to count of found",
          "if n is 1 then",
          "set r to item 1 of found",
          "if mode is \"complete\" then",
          "set completed of r to true",
          "else if mode is \"delete\" then",
          "delete r",
          "end if",
          "end if",
          "end tell",
          "return n as text",
          "end run"]


def _title(value):
    return common.clean_text(value, "название", common.MAX_TITLE)


def add_reminder(title, when, list_name=""):
    """when — datetime или None. Используется и будильником «разбуди меня»."""
    title = _title(title)
    list_name = common.clean_text(list_name, "список", common.MAX_LABEL, required=False)
    parts = common.when_components(when) if when else ["0"] * 5
    common.osascript(APP, _ADD, title, "1" if when else "0", *parts, list_name)


def _when_or_none(value):
    if value is None or not str(value).strip():
        return None
    return common.parse_when(value, field="when")


def _list():
    out = common.osascript(APP, _LIST, common.MAX_LINES)
    rows = []
    for line in out.splitlines():
        title, _, date = line.partition("\t")
        if title.strip():
            rows.append((title.strip(), common.parse_split_date(date.strip())))
    return rows


def _count(title, mode):
    out = common.osascript(APP, _MATCH, title, mode)
    try:
        return int(out.strip())
    except ValueError:
        raise ActionError(f"Reminders: неожиданный ответ {out!r}", "не удалось найти напоминание")


def _unique(title, mode):
    n = _count(title, mode)
    if n == 0:
        raise ActionError(f"напоминание {title!r} не найдено", "не нашёл")
    if n > 1:
        raise ActionError(f"напоминаний {title!r}: {n}", "таких напоминаний несколько, назовите точнее")


def run(action, title, when, list_name, phrase=""):
    if action == "add":
        due = _when_or_none(when)
        add_reminder(title, due, list_name)
        text = f"Напоминание «{_title(title)}» создано" + (f" на {common.when_text(due)}" if due else "")
        return common.done(text)
    if action == "list":
        rows = _list()
        if not rows:
            return common.done("Невыполненных напоминаний нет", external=True)
        items = [t + (f" ({common.when_text(d)})" if d else "") for t, d in rows]
        return common.done(f"Невыполненных напоминаний {len(items)}: " + "; ".join(items), external=True)
    if action == "complete":
        title = _title(title)
        _unique(title, "complete")
        return common.done(f"Напоминание «{title}» выполнено")
    if action == "delete":
        title = _title(title)
        _unique(title, "delete")
        return common.done(f"Напоминание «{title}» удалено")
    raise ActionError(f"reminders: неизвестное действие {action!r}", "неизвестное действие")


def preview(action, title, when, list_name, phrase=""):
    """Читающий поиск цели до вопроса «Удалить напоминание …?»: не нашёл или несколько — вопроса не будет."""
    title = _title(title)
    _unique(title, "check")
    return f"удалить напоминание «{title}»"


TOOLS = [AppTool(
    name="reminders",
    description="Напоминания: add — создать, list — невыполненные, complete — отметить, delete — удалить.",
    topic="personal",
    keywords=("напомн", "напомин", "reminder"),
    actions={"add": SAFE, "list": SAFE, "complete": SAFE, "delete": CONFIRM},
    params={"title": {"type": "string", "description": "Текст напоминания; для list пустая строка"},
            "when": {"type": "string", "description": "ГГГГ-ММ-ДД ЧЧ:ММ по строке «Сейчас» или пустая строка"},
            "list_name": {"type": "string", "description": "Имя списка или пустая строка (основной)"}},
    run=run,
    external=frozenset({"list"}),
    destructive=frozenset({"delete"}),
    text_params=("title",),
    creates=frozenset({"add"}),
    preview=preview,
)]
