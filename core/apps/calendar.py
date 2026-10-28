"""Приложение «Календарь»: add, list_today / list_tomorrow / list_week (до 10 событий), delete (с подтверждением).

Приглашений других людей нет и не будет: событие создаётся только со своими названием, началом и концом.
Нужны: Автоматизация → Календарь и доступ к Календарям. Запросы по диапазону дат, timeout 30 с: большой календарь
может отвечать медленно — тогда честно «не ответило вовремя».
"""
from datetime import datetime, timedelta

from core.apps import common
from core.apps.registry import CONFIRM, SAFE, AppTool
from core.errors import ActionError

APP = "Calendar"
TIMEOUT = 30
MIN_MINUTES, MAX_MINUTES = 5, 1440
MAX_SCAN = 60   # событий из AppleScript до сортировки и отбора первых 10

_ADD = [*common.MAKE_DATE,
        "on run argv",
        "set theTitle to item 1 of argv",
        "set d1 to my makeDate((item 2 of argv) as integer, (item 3 of argv) as integer, (item 4 of argv) as integer, "
        "(item 5 of argv) as integer, (item 6 of argv) as integer)",
        "set d2 to d1 + ((item 7 of argv) as integer) * minutes",
        'tell application "Calendar"',
        "set theCal to missing value",
        "repeat with c in calendars",
        "if writable of c then",
        "set theCal to c",
        "exit repeat",
        "end if",
        "end repeat",
        "if theCal is missing value then error \"нет календаря для записи\" number 9001",
        "tell theCal to make new event with properties {summary:theTitle, start date:d1, end date:d2}",
        "end tell",
        "end run"]

_LIST = [*common.MAKE_DATE, *common.SPLIT_DATE,
         "on run argv",
         "set d1 to my makeDate((item 1 of argv) as integer, (item 2 of argv) as integer, (item 3 of argv) as integer, "
         "(item 4 of argv) as integer, (item 5 of argv) as integer)",
         "set d2 to my makeDate((item 6 of argv) as integer, (item 7 of argv) as integer, (item 8 of argv) as integer, "
         "(item 9 of argv) as integer, (item 10 of argv) as integer)",
         "set maxCount to (item 11 of argv) as integer",
         "set out to {}",
         'tell application "Calendar"',
         "repeat with c in calendars",
         "set evs to (every event of c whose start date < d2 and end date > d1)",
         "repeat with e in evs",
         "if (count of out) >= maxCount then exit repeat",
         "set end of out to (summary of e) & tab & (my splitDate(start date of e)) & tab & ((allday event of e) as text)",
         "end repeat",
         "end repeat",
         "end tell",
         "set AppleScript's text item delimiters to linefeed",
         "return out as text",
         "end run"]

# Поиск события по названию в диапазоне; mode check — только найти, delete — удалить, если событие одно
_FIND = [*common.MAKE_DATE, *common.SPLIT_DATE,
         "on run argv",
         "set theTitle to item 1 of argv",
         "set mode to item 2 of argv",
         "set d1 to my makeDate((item 3 of argv) as integer, (item 4 of argv) as integer, (item 5 of argv) as integer, "
         "(item 6 of argv) as integer, (item 7 of argv) as integer)",
         "set d2 to my makeDate((item 8 of argv) as integer, (item 9 of argv) as integer, (item 10 of argv) as integer, "
         "(item 11 of argv) as integer, (item 12 of argv) as integer)",
         "set n to 0",
         "set startText to \"\"",
         "set target to missing value",
         'tell application "Calendar"',
         "repeat with c in calendars",
         "set evs to (every event of c whose summary is theTitle and start date >= d1 and start date < d2)",
         "repeat with e in evs",
         "set n to n + 1",
         "set target to e",
         "set startText to my splitDate(start date of e)",
         "end repeat",
         "end repeat",
         "if n is 1 and mode is \"delete\" then delete target",
         "end tell",
         "return (n as text) & tab & startText",
         "end run"]


def _day_start(day):
    return datetime(day.year, day.month, day.day)


def _parts(*moments):
    out = []
    for moment in moments:
        out += common.when_components(moment)
    return out


def _title(value):
    return common.clean_text(value, "название", common.MAX_TITLE)


def _events(first, last):
    out = common.osascript(APP, _LIST, *_parts(first, last), MAX_SCAN, timeout=TIMEOUT)
    events = []
    for line in out.splitlines():
        title, _, rest = line.partition("\t")
        date, _, all_day = rest.partition("\t")
        start = common.parse_split_date(date)
        if title.strip() and start:
            events.append((start, title.strip(), all_day.strip().lower() == "true"))
    return sorted(events)[:common.MAX_LINES]


def _format(events, label, multi_day, now):
    if not events:
        return f"{label.capitalize()} событий нет"
    items = []
    for start, title, all_day in events:
        at = "весь день" if all_day else f"в {common.time_text(start)}"
        items.append(f"{title} {common.day_text(start, now) + ' ' if multi_day else ''}{at}")
    count = f"{len(events)} {common.plural(len(events), 'событие', 'события', 'событий')}"
    return f"{label.capitalize()} {count}: " + "; ".join(items)


def _find(title, start, mode):
    """(сколько найдено, начало найденного). start — «ГГГГ-ММ-ДД ЧЧ:ММ» (ищем в этот день) или пусто (год вперёд)."""
    title = _title(title)
    if start and str(start).strip():
        day = _day_start(common.parse_when(start, field="start", allow_past=True))
        first, last = day, day + timedelta(days=1)
    else:
        first = _day_start(datetime.now())
        last = first + common.MAX_AHEAD
    out = common.osascript(APP, _FIND, title, mode, *_parts(first, last), timeout=TIMEOUT)
    count, _, date = out.partition("\t")
    try:
        return int(count), common.parse_split_date(date.strip())
    except ValueError:
        raise ActionError(f"Calendar: неожиданный ответ {out!r}", "не удалось найти событие")


def run(action, title, start, minutes, phrase=""):
    now = datetime.now()
    if action == "add":
        title = _title(title)
        begin = common.parse_when(start, field="start")
        try:
            length = int(minutes)
        except (TypeError, ValueError):
            raise ActionError(f"minutes: {minutes!r}", "длительность должна быть числом минут")
        if isinstance(minutes, bool) or not MIN_MINUTES <= length <= MAX_MINUTES:
            raise ActionError(f"minutes вне {MIN_MINUTES}–{MAX_MINUTES}: {minutes!r}",
                              f"длительность события от {MIN_MINUTES} минут до 24 часов")
        try:
            common.osascript(APP, _ADD, title, *common.when_components(begin), length, timeout=TIMEOUT)
        except ActionError as e:
            if "9001" in str(e):
                raise ActionError(str(e), "нет календаря, в который можно записывать")
            raise
        return common.done(f"Событие «{title}» создано на {common.when_text(begin, now)}")
    if action in ("list_today", "list_tomorrow", "list_week"):
        today = _day_start(now)
        first = today + timedelta(days=1) if action == "list_tomorrow" else today
        last = first + timedelta(days=7 if action == "list_week" else 1)
        label = {"list_today": "сегодня", "list_tomorrow": "завтра", "list_week": "на неделе"}[action]
        events = _events(first, last)
        return common.done(_format(events, label, action == "list_week", now), external=True)
    if action == "delete":
        count, _ = _find(title, start, "delete")
        if count == 0:
            raise ActionError("событие не найдено", "не нашёл")
        if count > 1:
            raise ActionError(f"событий {count}", "таких событий несколько, назовите время")
        return common.done(f"Событие «{_title(title)}» удалено")
    raise ActionError(f"calendar: неизвестное действие {action!r}", "неизвестное действие")


def preview(action, title, start, minutes, phrase=""):
    """Читающий поиск до вопроса: в вопросе названо найденное событие; не нашёл или несколько — вопроса нет."""
    count, found = _find(title, start, "check")
    if count == 0:
        raise ActionError("событие не найдено", "не нашёл")
    if count > 1:
        raise ActionError(f"событий {count}", "таких событий несколько, назовите время")
    return f"удалить событие «{_title(title)}»" + (f" {common.when_text(found)}" if found else "")


def _quick(phrase):
    from core.apps.registry import words_of
    text = " ".join(w for w in words_of(phrase) if w not in {"пожалуйста", "мне", "а"})
    if text in ("что у меня сегодня", "какие у меня дела сегодня", "какие сегодня события", "что у меня на сегодня"):
        return {"args": {"action": "list_today", "title": "", "start": "", "minutes": 0}}
    return None


TOOLS = [AppTool(
    name="calendar",
    description="Календарь: add — создать событие, list_today/list_tomorrow/list_week — что запланировано, delete — удалить.",
    topic="personal",
    keywords=("календар", "событи", "встреч", "расписан", "запланирован", "совещан", "что у меня", "мои дела",
              "мои планы", "на неделе"),
    actions={"add": SAFE, "list_today": SAFE, "list_tomorrow": SAFE, "list_week": SAFE, "delete": CONFIRM},
    params={"title": {"type": "string", "description": "Название события; для list пустая строка"},
            "start": {"type": "string", "description": "ГГГГ-ММ-ДД ЧЧ:ММ (add; у delete — день события или пусто)"},
            "minutes": {"type": "integer", "description": "add: длительность 5–1440 минут; иначе 0"}},
    run=run,
    external=frozenset({"list_today", "list_tomorrow", "list_week"}),
    destructive=frozenset({"delete"}),
    text_params=("title",),
    creates=frozenset({"add"}),
    preview=preview,
    quick=_quick,
)]
