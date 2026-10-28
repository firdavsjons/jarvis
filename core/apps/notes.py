"""Приложение «Заметки»: create, append, list_titles, read_last (первые 300 символов).

Текст и название — только через argv, в HTML (тело заметки) они экранируются в Python. Нужны:
Автоматизация → Заметки. Порядок заметок не гарантирован, поэтому последняя определяется по дате изменения.
"""
import html

from core.apps import common
from core.apps.registry import SAFE, AppTool
from core.errors import ActionError

APP = "Notes"
TIMEOUT = 20
READ_LIMIT = 300

_CREATE = ["on run argv",
           'tell application "Notes" to make new note with properties {body:(item 1 of argv)}',
           "end run"]

_APPEND = ["on run argv",
           'tell application "Notes"',
           "set found to (notes whose name is (item 1 of argv))",
           "if (count of found) is 0 then return \"0\"",
           "set n to item 1 of found",
           "set body of n to (body of n) & (item 2 of argv)",
           "return \"1\"",
           "end tell",
           "end run"]

# «секунд назад<TAB>название» для каждой заметки: дату сравниваем как разницу с текущей, без локали
_TITLES = ["on run argv",
           "set maxCount to (item 1 of argv) as integer",
           "set nowDate to current date",
           'tell application "Notes"',
           "set names to name of every note",
           "set mods to modification date of every note",
           "end tell",
           "set out to {}",
           "repeat with i from 1 to (count of names)",
           "set end of out to ((nowDate - (item i of mods)) as integer as text) & tab & (item i of names)",
           "end repeat",
           "set AppleScript's text item delimiters to linefeed",
           "return out as text",
           "end run"]

_READ_LAST = ["on run argv",
              "set nowDate to current date",
              'tell application "Notes"',
              "set mods to modification date of every note",
              "if (count of mods) is 0 then return \"\"",
              "set best to 1",
              "repeat with i from 2 to (count of mods)",
              "if (item i of mods) > (item best of mods) then set best to i",
              "end repeat",
              "set n to item best of notes",
              "return (name of n) & linefeed & (plaintext of n)",
              "end tell",
              "end run"]


def _html(title, text):
    parts = []
    if title:
        parts.append(f"<div><h1>{html.escape(title)}</h1></div>")
    for line in text.splitlines() or [""]:
        parts.append(f"<div>{html.escape(line) if line.strip() else '<br>'}</div>")
    return "".join(parts)


def _titles(limit):
    out = common.osascript(APP, _TITLES, 0, timeout=TIMEOUT)
    rows = []
    for line in out.splitlines():
        age, _, name = line.partition("\t")
        try:
            rows.append((int(age), name.strip()))
        except ValueError:
            continue
    return [name for _, name in sorted(rows) if name][:limit]


def run(action, title, text, phrase=""):
    if action == "create":
        title = common.clean_text(title, "название", common.MAX_TITLE)
        body = common.clean_text(text, "текст", common.MAX_TEXT, required=False, multiline=True)
        common.osascript(APP, _CREATE, _html(title, body), timeout=TIMEOUT)
        return common.done(f"Заметка «{title}» создана")
    if action == "append":
        title = common.clean_text(title, "название", common.MAX_TITLE)
        body = common.clean_text(text, "текст", common.MAX_TEXT, multiline=True)
        out = common.osascript(APP, _APPEND, title, _html("", body), timeout=TIMEOUT)
        if out.strip() != "1":
            raise ActionError(f"заметка {title!r} не найдена", "не нашёл такую заметку")
        return common.done(f"Добавил в заметку «{title}»")
    if action == "list_titles":
        names = _titles(common.MAX_LINES)
        if not names:
            return common.done("Заметок нет", external=True)
        return common.done("Заметки: " + "; ".join(names), external=True)
    if action == "read_last":
        out = common.osascript(APP, _READ_LAST, timeout=TIMEOUT)
        if not out.strip():
            raise ActionError("заметок нет", "заметок нет")
        name, _, body = out.partition("\n")
        content = common.limit_text(body, READ_LIMIT)
        return common.done(f"Последняя заметка «{name.strip()}»: {content}" if content
                           else f"Последняя заметка «{name.strip()}» пустая", external=True)
    raise ActionError(f"notes: неизвестное действие {action!r}", "неизвестное действие")


TOOLS = [AppTool(
    name="notes",
    description="Заметки: create — новая, append — дописать в заметку по названию, list_titles, read_last — последняя.",
    topic="personal",
    keywords=("заметк", "заметок", "записк"),
    actions={"create": SAFE, "append": SAFE, "list_titles": SAFE, "read_last": SAFE},
    params={"title": {"type": "string", "description": "Название заметки (create, append); иначе пустая строка"},
            "text": {"type": "string", "description": "Текст (create, append); иначе пустая строка"}},
    run=run,
    external=frozenset({"list_titles", "read_last"}),
    text_params=("title", "text"),
    creates=frozenset({"create", "append"}),
)]
