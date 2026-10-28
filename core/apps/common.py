"""Общие функции приложений: osascript с argv, ограничение длины, проверка строк и дат, ошибки разрешений.

Строки пользователя и модели никогда не вставляются в текст AppleScript — только через argv.
"""
import logging
import re
import subprocess
import unicodedata
from datetime import datetime, timedelta

from core.errors import ActionError, Done

log = logging.getLogger(__name__)

TIMEOUT = 10
MAX_TITLE = 200
MAX_TEXT = 2000
MAX_LABEL = 100
MAX_LINES = 10      # событий, напоминаний, заметок в одном ответе
MAX_OUTPUT = 300    # символов результата Быстрой команды

WHEN_FORMAT = "%Y-%m-%d %H:%M"
_WHEN_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
MAX_AHEAD = timedelta(days=365)

# Коды ошибок osascript
AUTOMATION_CODES = ("-1743", "not authorized", "not allowed to send apple events")
ACCESSIBILITY_CODES = ("-25211", "-1719", "(1002)", "not allowed to send keystrokes", "assistive access")
NOT_FOUND_CODES = ("-1728",)

PERMISSION_WINDOW = ("Нужно разрешение: Системные настройки → Конфиденциальность и безопасность → {section} "
                     "для приложения, из которого запущен Jarvis.")
PERMISSION_VOICE = ("Нужно разрешение: Системные настройки, Конфиденциальность и безопасность, {section}, "
                    "для приложения, из которого запущен Jarvis.")

# Приложения с отдельным доступом к данным (Календари, Напоминания), кроме Автоматизации
DATA_ACCESS = {"Calendar": "Календари", "Reminders": "Напоминания"}
APP_NAMES = {"Calendar": "Календарь", "Reminders": "Напоминания", "Notes": "Заметки", "Shortcuts": "Быстрые команды",
             "System Events": "System Events", "Clock": "Часы"}


def app_title(app):
    return APP_NAMES.get(app, app)


def permission_error(app, stderr):
    """ActionError с понятной фразой и названием приложения, если osascript отказал по правам; иначе None."""
    low = (stderr or "").lower()
    title = app_title(app)
    if any(code in low for code in ACCESSIBILITY_CODES):
        section = "Универсальный доступ"
    elif any(code in low for code in AUTOMATION_CODES):
        section = f"Автоматизация → {title}"
        if app in DATA_ACCESS:
            section += f" (и доступ к разделу «{DATA_ACCESS[app]}»)"
    elif any(code in low for code in NOT_FOUND_CODES):
        text = f"Не нашёл нужный объект в приложении {title}: список, событие или запись с таким именем."
        return ActionError(f"{app}: -1728", text.rstrip("."), text)
    else:
        return None
    voice = PERMISSION_VOICE.format(section=section.replace(" → ", ", "))
    return ActionError(PERMISSION_WINDOW.format(section=section), voice, voice)


def osascript(app, lines, *argv, timeout=TIMEOUT):
    """AppleScript построчно (-e), значения — только через argv. Ошибка прав -> ActionError с названием app."""
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    args += [str(a) for a in argv]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ActionError(f"{app}: таймаут {timeout} с", f"{app_title(app)} не ответило вовремя")
    except OSError as e:
        raise ActionError(str(e), "команда не запустилась")
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise permission_error(app, stderr) or ActionError(f"{app}: {stderr or result.returncode}")
    return (result.stdout or "").strip()


# --- Проверка строк ---

def clean_text(value, field, limit=MAX_TITLE, required=True, multiline=False):
    """Строка без управляющих символов, обрезанная пробелами, не длиннее limit. Пустая — ошибка, если required."""
    if value is None:
        value = ""
    if not isinstance(value, str) or isinstance(value, bool):
        raise ActionError(f"{field}: ожидалась строка, получено {value!r}", f"неверный параметр: {field}")
    text = value.strip()
    allowed = {"\n", "\t"} if multiline else set()
    if any(unicodedata.category(c) == "Cc" and c not in allowed for c in text):
        raise ActionError(f"{field}: управляющие символы", f"неверный параметр: {field}")
    if required and not text:
        raise ActionError(f"{field}: пусто", f"не указано: {field}")
    if len(text) > limit:
        raise ActionError(f"{field}: {len(text)} символов, максимум {limit}", f"слишком длинный текст, максимум {limit}")
    return text


def limit_text(text, n=MAX_OUTPUT):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def parse_when(value, now=None, field="время", allow_past=False):
    """«YYYY-MM-DD HH:MM» местного времени -> datetime. Не в прошлом (если не allow_past) и не дальше года."""
    now = now or datetime.now()
    if not isinstance(value, str) or not _WHEN_RE.match(value.strip()):
        raise ActionError(f"{field}: формат {value!r}", "время нужно в формате ГГГГ-ММ-ДД ЧЧ:ММ")
    try:
        when = datetime.strptime(value.strip(), WHEN_FORMAT)
    except ValueError:
        raise ActionError(f"{field}: несуществующая дата {value!r}", "такой даты не существует")
    if not allow_past and when < now.replace(second=0, microsecond=0):
        raise ActionError(f"{field}: в прошлом {value}", "это время уже прошло")
    if when > now + MAX_AHEAD:
        raise ActionError(f"{field}: дальше года {value}", "дальше чем на год вперёд не ставлю")
    return when


def when_components(when):
    """datetime -> строки для argv: год, месяц, день, часы, минуты (дата собирается в AppleScript по частям,
    без разбора локализованных строк)."""
    return [str(when.year), str(when.month), str(when.day), str(when.hour), str(when.minute)]


# Обработчик AppleScript: дата из частей, локаль не важна. Подставляется первой строкой скрипта
MAKE_DATE = [
    "on makeDate(y, m, d, hh, mm)",
    "set theDate to current date",
    "set day of theDate to 1",
    "set year of theDate to y",
    "set month of theDate to m",
    "set day of theDate to d",
    "set hours of theDate to hh",
    "set minutes of theDate to mm",
    "set seconds of theDate to 0",
    "return theDate",
    "end makeDate",
]

# Дата в виде «год\tмесяц\tдень\tчасы\tминуты» из AppleScript (без локали)
SPLIT_DATE = [
    "on splitDate(d)",
    "return ((year of d) as text) & \"-\" & ((month of d) as integer) & \"-\" & (day of d) & \"-\" & (hours of d) "
    "& \"-\" & (minutes of d)",
    "end splitDate",
]


def parse_split_date(text):
    try:
        y, mo, d, h, mi = (int(x) for x in text.split("-"))
        return datetime(y, mo, d, h, mi)
    except (ValueError, TypeError):
        return None


MONTHS_GENITIVE = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
                   "ноября", "декабря"]


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def time_text(when):
    return f"{when.hour}:{when.minute:02d}"


def day_text(when, today=None):
    """«сегодня», «завтра» или «3 ноября»."""
    today = (today or datetime.now()).date()
    if when.date() == today:
        return "сегодня"
    if when.date() == today + timedelta(days=1):
        return "завтра"
    return f"{when.day} {MONTHS_GENITIVE[when.month - 1]}"


def when_text(when, today=None):
    """«сегодня в 7:00», «3 ноября в 7:00»."""
    return f"{day_text(when, today)} в {time_text(when)}"


def done(text, message=None, external=False):
    """Результат действия: озвучивается с точкой в конце; модели и шаблону (template_speech добавляет точку сам)
    уходит текст без неё."""
    text = text.rstrip()
    spoken = text if text.endswith((".", "!", "?")) else text + "."
    return Done(message or spoken, spoken, for_model=spoken.rstrip("."), web=external)
