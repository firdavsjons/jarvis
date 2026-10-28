"""Диагностика вставки черновика в Telegram: python -m core.telegram_check [--to <имя>] [--repeat N] [--methods …]

Запускает ПОЛЬЗОВАТЕЛЬ (тесты — только на моках). Вставляет ФИКСИРОВАННЫЙ тест «проверка Jarvis (черновик)» в чат из
contacts.json (по умолчанию «себе»). Enter не нажимается нигде; черновики пользователь стирает вручную (скрипт не стирает).

РЕЖИМ ПО УМОЛЧАНИЮ — повторы боевой фазы 1 (Этап 7.1.5):
  для каждого способа вставки (--methods keystroke,keycode,menu) N попыток подряд (--repeat N, по умолчанию 3): боевой
  путь (режим и паузы из .env), перед каждой попыткой пользователь стирает черновик, после — «Появился ли текст? (д/н/п)».
  В каждой попытке печатается состояние пункта «Вставить» меню Telegram (доступен / недоступен / не прочитано) — для
  сопоставления с ответом; --open-menu дополнительно один раз за попытку открывает меню «Изменить» кликом по строке меню и
  закрывает Escape (по пунктам не кликает), чтобы сравнить состояние до и после открытия. Итог — таблица «способ × попытка»
  и строка TELEGRAM_PASTE_METHOD=… для .env.

ПРЕЖНИЕ ВАРИАНТЫ (--only Е,З1,А,Б,Ж,З2,Д,Г,В,И, Этап 7.1.3): Е — проверка буфера; З1 — key code 9; А/Б — боевой путь с
возвратом буфера сразу / через паузу из .env; Ж — латиница; З2 — меню; Д — чат по ссылке дважды; Г — пробел + Delete и
В — клик (рискованные, только если безопасные не помогли); И — ручная проверка.
Краткий итог (без текста, username и телефонов) сохраняется в telegram_check_last.json и сравнивается со следующим
запуском: так видно, влияет ли раскладка клавиатуры.
"""
import argparse
import logging
import re
import sys
import time
from datetime import datetime
from types import SimpleNamespace

from core import contacts as contacts_store
from core.apps import common
from core.apps import telegram as tg
from core.errors import ActionError
from core.storage import DATA_DIR, atomic_write_json, read_json

TEST_TEXT = "проверка Jarvis (черновик)"
ASCII_TEXT = "jarvis test"
LAST_FILE = DATA_DIR / "telegram_check_last.json"

REOPEN_WAIT = 1.0          # вариант Д: пауза после второго открытия чата
ONE_WAIT = 1.0             # пауза после Cmd+V в вариантах Б, Ж, З, Д, Г, В
KEY_STEP_DELAY = 0.2       # между пробелом, Delete и Cmd+V (вариант Г)
CLICK_X_FRACTION = 0.62    # вариант В: x = левый край окна + 0.62 · ширина
CLICK_BOTTOM_OFFSET = 38   # вариант В: y = нижний край окна − 38 пикселей
CLICK_MIN_WIDTH = 700      # окно уже — вариант В пропускается

STATE_TEXT = {True: "доступен", False: "недоступен", None: "не прочитано"}
REPEAT_DEFAULT = 3
REPEAT_MAX = 10
METHOD_PREFERENCE = ("keystroke", "keycode", "menu")      # при равенстве предпочитаем самый простой
OPEN_MENU_DELAY = 0.3
UNRELIABLE_NOTE = ("Состояние пункта «Вставить» не является надёжным индикатором фокуса в вашем Telegram, "
                   "не используйте режим menu как проверку")
ERASE_PROMPT = ("Сотрите черновик в Telegram вручную (скрипт его не стирает), затем нажмите Enter здесь "
                "(или «п» — пропустить попытку): ")

SAFE_VARIANTS = ("З1", "А", "Б", "Ж", "З2", "Д")
RISKY_VARIANTS = ("Г", "В")
ORDER = ("Е", "З1", "А", "Б", "Ж", "З2", "Д", "Г", "В", "И")
YES, NO = {"д", "да", "y", "yes"}, {"н", "нет", "n", "no"}
SKIP = {"п", "пропустить", "s", "skip"}
SKIP_RISKY_NOTE = "не нужно, безопасный вариант сработал"
LAYOUT_WARNING = "Для сравнения запустите диагностику дважды: с английской и с русской раскладкой."

_PROBE_AUTOMATION = ['tell application "System Events" to return name of first process whose frontmost is true']
_PROBE_ACCESSIBILITY = ['tell application "System Events" to return count of windows of '
                        '(first process whose frontmost is true)']
_PASTE_KEYCODE = tg._PASTE_KEYCODE
_TYPE_SPACE = ["on run argv", 'tell application "System Events" to keystroke " "', "end run"]
_DELETE = ["on run argv", 'tell application "System Events" to key code 51', "end run"]
_CLIPBOARD_AS = ["on run argv",
                 "set the clipboard to (item 1 of argv)",
                 "set back to (the clipboard as text)",
                 "return ((back = (item 1 of argv)) as text) & \"|\" & (length of back)",
                 "end run"]
_WINDOW_GEOMETRY = ["on run argv",
                    'tell application "System Events"',
                    "set found to (every process whose bundle identifier is (item 1 of argv))",
                    'if (count of found) is 0 then return "no-process"',
                    "set p to item 1 of found",
                    'if (count of windows of p) is 0 then return "no-window"',
                    "set w to window 1 of p",
                    "set {x, y} to position of w",
                    "set {ww, hh} to size of w",
                    "return (x as text) & \" \" & (y as text) & \" \" & (ww as text) & \" \" & (hh as text)",
                    "end tell",
                    "end run"]
_CLICK = ["on run argv",
          'tell application "System Events" to click at {(item 1 of argv) as integer, (item 2 of argv) as integer}',
          "end run"]
# Скрипты меню общие с боевым кодом (core/apps/telegram.py): диагностика проверяет именно их
_MENU_BAR, _MENU_ITEMS, _MENU_ENABLED, _MENU_CLICK = tg._MENU_BAR, tg._MENU_ITEMS, tg._MENU_ENABLED, tg._MENU_CLICK
_MENU_OPEN = ["on run argv",       # только строка меню: клик по названию меню, не по его пунктам
              'tell application "System Events"',
              "tell (first process whose bundle identifier is (item 1 of argv))",
              "click menu bar item (item 2 of argv) of menu bar 1",
              "end tell",
              "end tell",
              "end run"]
_ESCAPE = ["on run argv", 'tell application "System Events" to key code 53', "end run"]
ALL_SCRIPTS = (_PROBE_AUTOMATION, _PROBE_ACCESSIBILITY, _PASTE_KEYCODE, _TYPE_SPACE, _DELETE, _CLIPBOARD_AS,
               _WINDOW_GEOMETRY, _CLICK, _MENU_BAR, _MENU_ITEMS, _MENU_ENABLED, _MENU_CLICK, _MENU_OPEN, _ESCAPE)


class Skipped(Exception):
    """Вариант не выполнялся (причина — в тексте)."""


class Report:
    def __init__(self, out):
        self.out = out

    def say(self, text=""):
        print(text, file=self.out)

    def step(self, title, func):
        """Шаг с [ok]/[ошибка] и временем. Возвращает результат функции (True, если она вернула None) или None."""
        started = time.monotonic()
        try:
            result = func()
        except ActionError as e:
            self.say(f"  [ошибка] {title} — {e.reply or e.short} ({time.monotonic() - started:.1f} с)")
            return None
        self.say(f"  [ok] {title} ({time.monotonic() - started:.1f} с)")
        return True if result is None else result


# --- Раскладка ---

def current_layout():
    """(название, вид: ru / en / other / ?) по `defaults read com.apple.HIToolbox AppleSelectedInputSources`."""
    try:
        out = common.run_cmd(["defaults", "read", "com.apple.HIToolbox", "AppleSelectedInputSources"])
    except ActionError:
        return "не удалось определить", "?"
    found = re.findall(r'KeyboardLayout Name"?\s*=\s*"?([^";\n]+)', out)
    if not found:
        return "не удалось определить", "?"
    name = found[0].strip()
    if re.search(r"russian|рус", name, re.IGNORECASE):
        return name, "ru"
    if re.match(r"(abc|u\.?s\.?\b|us\b|british|australian|canadian|english)", name, re.IGNORECASE):
        return name, "en"
    return name, "other"


# --- Ввод ---

def ask_choice(question, input_fn, out):
    """True — «д», False — «н», None — «п» (пропустить) или три неверных ответа."""
    for _ in range(3):
        answer = input_fn(question).strip().lower()
        if answer in YES:
            return True
        if answer in NO:
            return False
        if answer in SKIP:
            return None
        print("Ответьте «д», «н» или «п» (пропустить).", file=out)
    return None


def confirm_risk(ctx, text):
    ctx.report.say(text)
    answer = ask_choice("Продолжить? (д/н/п): ", ctx.input_fn, ctx.out)
    if answer is not True:
        raise Skipped("отказ пользователя")


# --- Шаги вариантов ---

def _wait_chat(ctx):
    tg._open_chat(ctx.client, ctx.contact)
    if not tg._wait_window(ctx.client):
        raise ActionError("Telegram не открылся за 5 с", "Telegram не открылся")


def _paste_keystroke(ctx):
    common.osascript(tg.SYSTEM_EVENTS, tg._PASTE)


def _paste_keycode(ctx):
    common.osascript(tg.SYSTEM_EVENTS, _PASTE_KEYCODE)


def _paste_menu(ctx):
    """Меню «Правка / Изменить / Edit → Вставить»: сначала только чтение названий и состояния, потом клик."""
    try:
        state = tg.read_paste_item(ctx.client)
    except ActionError as e:
        raise Skipped(f"{e.short}: {e}")
    ctx.report.say(f"     пункты «{state['menu']}»: {', '.join(state['items']) or '(пусто)'}")
    ctx.report.say(f"     «{state['item']}»: {STATE_TEXT[state['enabled']]}")
    common.osascript(tg.SYSTEM_EVENTS, _MENU_CLICK, ctx.client, state["menu"], state["item"])


def _type_space_and_delete(ctx):
    """Один пробел (Telegram переводит фокус в поле ввода при наборе символа) и Delete ровно один раз — только если
    пробел набран этим скриптом. Не удался набор — Delete не нажимается."""
    common.osascript(tg.SYSTEM_EVENTS, _TYPE_SPACE)
    time.sleep(KEY_STEP_DELAY)
    common.osascript(tg.SYSTEM_EVENTS, _DELETE)
    time.sleep(KEY_STEP_DELAY)


def _click_input(ctx):
    raw = common.osascript(tg.SYSTEM_EVENTS, _WINDOW_GEOMETRY, ctx.client).strip()
    if raw in ("no-process", "no-window"):
        raise Skipped("окно Telegram не найдено")
    try:
        x, y, width, height = (int(float(v)) for v in raw.split())
    except ValueError:
        raise Skipped("не удалось прочитать размер окна Telegram")
    ctx.report.say(f"     окно Telegram: x={x}, y={y}, ширина={width}, высота={height}")
    if width <= CLICK_MIN_WIDTH:
        raise Skipped(f"окно уже {CLICK_MIN_WIDTH} пикселей (ширина {width})")
    point_x, point_y = round(x + CLICK_X_FRACTION * width), y + height - CLICK_BOTTOM_OFFSET
    ctx.report.say(f"     клик: x = левый край + {CLICK_X_FRACTION} · ширина = {point_x}, "
                   f"y = нижний край − {CLICK_BOTTOM_OFFSET} = {point_y}")
    common.osascript(tg.SYSTEM_EVENTS, _CLICK, point_x, point_y)


def _flow(ctx, text, wait, *, reopen=False, before_paste=None, paste=_paste_keystroke):
    """Открыть чат, дождаться Telegram впереди, текст в буфер (как в боевом коде), проверить фокус, подготовка,
    Cmd+V выбранным способом, пауза. Буфер возвращает вызывающий."""
    _wait_chat(ctx)
    if reopen:
        tg._open_chat(ctx.client, ctx.contact)
        time.sleep(REOPEN_WAIT)
        if not tg._wait_window(ctx.client):
            raise ActionError("Telegram не открылся за 5 с", "Telegram не открылся")
    time.sleep(tg.PASTE_DELAY)
    common.run_cmd(["pbcopy"], stdin=text, env=tg._utf8_env())
    if tg._frontmost() != ctx.client:
        raise ActionError("Telegram не в фокусе перед вставкой", tg.NOT_FOCUSED)
    if before_paste is not None:
        before_paste(ctx)
    paste(ctx)
    time.sleep(wait)


def run_variant(ctx, vid, title, action, *, risk=None):
    """Один вариант: просьба стереть черновик → (предупреждение) → действие с возвратом буфера → вопрос.
    Возвращает True / False / None (пропущен)."""
    ctx.report.say(f"\nВариант {vid}: {title}")
    answer = ctx.input_fn("Если в поле ввода Telegram есть текст, сотрите его вручную, затем нажмите Enter здесь "
                          "(или «п» — пропустить вариант): ").strip().lower()
    if answer in SKIP:
        ctx.report.say("  пропущен по вашему выбору")
        return None
    try:
        if risk:
            confirm_risk(ctx, risk)
        saved = tg._save_clipboard()
    except Skipped as e:
        ctx.report.say(f"  пропущен: {e}")
        return None
    except ActionError as e:
        ctx.report.say(f"  [ошибка] буфер обмена — {e.reply or e.short}")
        return None
    done = []

    def run():
        action()
        done.append(True)

    try:
        try:
            ctx.report.step(title, run)
        except Skipped as e:
            ctx.report.say(f"  пропущен: {e}")
            return None
    finally:
        ctx.report.step("вернуть прежний буфер обмена", lambda: tg._restore_clipboard(saved))
    if not done:
        return None      # ошибка шага — это не «текста нет»: вопрос не задаём, в вывод не попадает
    return ask_choice("Появился ли текст в поле ввода? (д/н/п): ", ctx.input_fn, ctx.out)


# --- Проверка буфера (вариант Е) ---

def check_clipboard(ctx):
    """Е: текст в буфер двумя способами и сразу обратно. Печатает «совпадает / не совпадает» и длины, не текст."""
    ctx.report.say("\nВариант Е: проверка буфера (без вопросов, текст не выводится)")
    try:
        saved = tg._save_clipboard()
    except ActionError as e:
        ctx.report.say(f"  [ошибка] буфер обмена — {e.reply or e.short}")
        return {"applescript": None, "pbcopy": None}
    result = {"applescript": None, "pbcopy": None}
    try:
        def via_applescript():
            out = common.osascript(tg.SYSTEM_EVENTS, _CLIPBOARD_AS, TEST_TEXT)
            same, _, length = out.partition("|")
            result["applescript"] = same.strip() == "true"
            return f"{'совпадает' if result['applescript'] else 'не совпадает'}: в буфере {length.strip()} симв., нужно {len(TEST_TEXT)}"

        def via_pbcopy():
            common.run_cmd(["pbcopy"], stdin=TEST_TEXT, env=tg._utf8_env())
            back = common.run_cmd(["pbpaste"], env=tg._utf8_env())
            result["pbcopy"] = back == TEST_TEXT
            return f"{'совпадает' if result['pbcopy'] else 'не совпадает'}: в буфере {len(back)} симв., нужно {len(TEST_TEXT)}"

        for title, func in (("AppleScript: set the clipboard to (item 1 of argv)", via_applescript),
                            ("pbcopy → pbpaste (способ боевого кода)", via_pbcopy)):
            outcome = ctx.report.step(title, func)
            if isinstance(outcome, str):
                ctx.report.say(f"     {outcome}")
    finally:
        ctx.report.step("вернуть прежний буфер обмена", lambda: tg._restore_clipboard(saved))
    return result


# --- Ручная проверка (вариант И) ---

def manual_check(ctx, clipboard):
    ctx.report.say("\nВариант И: ручная проверка (ничего не сработало автоматически)")
    try:
        saved = tg._save_clipboard()
    except ActionError as e:
        ctx.report.say(f"  [ошибка] буфер обмена — {e.reply or e.short}")
        return None
    try:
        if clipboard.get("applescript"):
            ctx.report.step("тестовый текст в буфере (AppleScript)",
                            lambda: common.osascript(tg.SYSTEM_EVENTS, _CLIPBOARD_AS, TEST_TEXT))
        else:
            ctx.report.step("тестовый текст в буфере (pbcopy)",
                            lambda: common.run_cmd(["pbcopy"], stdin=TEST_TEXT, env=tg._utf8_env()))
        ctx.input_fn("Стерев черновик, нажмите Enter здесь, затем в Telegram: ")
        return ask_choice("Кликните в поле ввода и нажмите Cmd+V руками. Появился ли текст? (д/н/п): ",
                          ctx.input_fn, ctx.out)
    finally:
        ctx.report.step("вернуть прежний буфер обмена", lambda: tg._restore_clipboard(saved))


# --- Вывод ---

def _worked(results, *ids):
    return [vid for vid in ids if results.get(vid) is True]


def analyse(results, clipboard):
    """(строки вывода, строки для .env). results: вариант → True / False / None."""
    lines, env = [], []
    safe = _worked(results, *SAFE_VARIANTS)
    plain = _worked(results, "А", "Б", "Ж")
    if clipboard.get("applescript") is True and clipboard.get("pbcopy") is False:
        lines.append("Буфер: через pbcopy текст не совпадает, через AppleScript совпадает — причина в кодировке/способе "
                     "записи в буфер. Боевую вставку нужно перевести на AppleScript (отдельным шагом).")
        env.append("TELEGRAM_CLIPBOARD_METHOD=applescript")
    elif clipboard.get("applescript") is False:
        lines.append("Буфер: даже через AppleScript текст не совпадает — что-то перехватывает буфер обмена (менеджер "
                     "буфера, Universal Clipboard). Отключите их на время проверки.")
    elif clipboard.get("applescript") is True and clipboard.get("pbcopy") is True:
        lines.append("Буфер: оба способа кладут текст верно — дело не в буфере и не в кодировке.")
    if results.get("Ж") is True and results.get("Б") is False:
        lines.append("Латиница вставилась, кириллица нет — это кодировка/буфер, а не нажатие клавиш.")
    elif results.get("Ж") is False and results.get("Б") is False:
        lines.append("Латиница тоже не вставилась — дело не в кодировке.")
    if results.get("З1") is True and not plain:
        lines.append("Cmd+V по физической клавише (key code 9) сработал, а keystroke «v» — нет: вероятна зависимость от "
                     "раскладки. Боевую вставку нужно перевести на key code 9 (отдельным шагом).")
        env.append("TELEGRAM_PASTE_METHOD=keycode")
    elif results.get("З2") is True and not plain and results.get("З1") is not True:
        lines.append("Сработал только пункт меню «Правка → Вставить». Боевую вставку нужно перевести на меню.")
        env.append("TELEGRAM_PASTE_METHOD=menu")
    elif plain:
        lines.append(f"Штатная вставка сработала (вариант {', '.join(plain)}).")
        env.append("TELEGRAM_PASTE_METHOD=keystroke")
    for vid, mode, text in (("Г", "type", "Telegram переводит фокус в поле ввода при наборе символа"),
                            ("Д", "reopen", "второе открытие чата по ссылке переводит фокус в поле"),
                            ("В", "click", "помог клик в поле ввода")):
        if results.get(vid) is True:
            lines.append(f"Фокус: сработал вариант {vid} ({text}).")
            env.append(f"TELEGRAM_FOCUS_MODE={mode}" + ("" if mode in tg.FOCUS_MODES else
                                                       "   # боевой код пока поддерживает только reopen и direct"))
            break
    for vid in RISKY_VARIANTS:
        if results.get(vid) == "skip-safe":
            lines.append(f"Вариант {vid}: {SKIP_RISKY_NOTE}.")
    if results.get("И") is True:
        lines.append("Руками текст вставляется: буфер в порядке, не работает именно автоматическое нажатие или фокус.")
    elif results.get("И") is False:
        lines.append("Руками Cmd+V тоже не вставляет: проблема в самом поле/чате (бот без поля ввода? профиль вместо "
                     "чата?) или в буфере обмена.")
    return lines, env


def _nothing_worked_help(heading=True):
    return [("Ни один вариант не сработал. " if heading else "") + "Опишите, пожалуйста, что видно в окне Telegram: есть "
            "ли внизу поле ввода сообщения, открыт чат или профиль, мигает ли курсор, есть ли кнопки вместо поля "
            "(START, Mute, Join).",
            "Следующие три проверки:",
            "  1) переключите раскладку на английскую и запустите диагностику ещё раз, затем с русской (сравнение "
            "покажет, влияет ли раскладка);",
            "  2) откройте тот же чат вручную, кликните в поле, нажмите Cmd+V руками и посмотрите, вставляется ли "
            "что-либо; попробуйте другой контакт (--to);",
            "  3) проверьте меню «Правка / Изменить» в строке меню Telegram (пункт «Вставить») и версию Telegram; при "
            "возможности — тот же сценарий в Telegram Desktop."]


def compare_with_last(previous, layout_kind, results):
    """Строка о влиянии раскладки по итогу прошлого запуска."""
    if not previous or not isinstance(previous.get("results"), dict):
        return "Раскладка: сравнить не с чем — " + LAYOUT_WARNING
    prev_kind = previous.get("layout_kind")
    if "?" in (prev_kind, layout_kind) or prev_kind == layout_kind:
        same = "та же раскладка" if prev_kind == layout_kind else "раскладка не определена"
        return f"Раскладка: прошлый запуск — {same}, для сравнения переключите раскладку и запустите ещё раз."
    differs = []
    for vid in ("З1", "А", "Б", "Ж", "З2", "Д", "Г", "В"):
        before, now = previous["results"].get(vid), results.get(vid)
        if before in (True, False) and now in (True, False) and before != now:
            differs.append(f"{vid}: {_kind_name(prev_kind)} — {'д' if before else 'н'}, {_kind_name(layout_kind)} — "
                           f"{'д' if now else 'н'}")
    if differs:
        return "Раскладка ВЛИЯЕТ: " + "; ".join(differs)
    return (f"Раскладка не влияет: результаты при {_kind_name(prev_kind)} и {_kind_name(layout_kind)} одинаковы "
            f"(по вариантам, которые удалось сравнить).")


def _kind_name(kind):
    return {"ru": "русской", "en": "английской"}.get(kind, kind)


def summary_for_file(layout, kind, results, clipboard, repeat=None):
    """Итог для telegram_check_last.json: только исходы, раскладка и время — без текста и контактов."""
    data = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "layout": layout, "layout_kind": kind,
            "results": {vid: (value if isinstance(value, bool) else None) for vid, value in results.items()},
            "clipboard": {k: v for k, v in clipboard.items() if isinstance(v, bool)}}
    if repeat is not None:
        data["repeat"] = {m: [a if isinstance(a, bool) else None for a in answers] for m, answers in repeat.items()}
    return data


# --- Основной сценарий ---

# --- Повторы боевой фазы 1 (режим по умолчанию) ---

def parse_methods(raw):
    methods = []
    for part in re.split(r"[,\s]+", (raw or "").strip().lower()):
        if not part:
            continue
        if part not in tg.PASTE_METHODS:
            raise SystemExit(f"неизвестный способ {part!r}; допустимо: {', '.join(tg.PASTE_METHODS)}")
        if part not in methods:
            methods.append(part)
    if not methods:
        raise SystemExit("не указан ни один способ вставки")
    return methods


def parse_repeat(value):
    if not 1 <= value <= REPEAT_MAX:
        raise SystemExit(f"--repeat должен быть от 1 до {REPEAT_MAX}")
    return value


def state_after_open(ctx, menu_name):
    """Один раз открыть меню кликом ПО СТРОКЕ МЕНЮ (по пунктам не кликаем), прочитать состояние «Вставить» при
    открытом меню, закрыть Escape. Меню не открылось — Escape не нажимаем (в Telegram он закрыл бы чат)."""
    try:
        common.osascript(tg.SYSTEM_EVENTS, _MENU_OPEN, ctx.client, menu_name)
    except ActionError:
        return None
    try:
        time.sleep(OPEN_MENU_DELAY)
        try:
            return tg.read_paste_item(ctx.client)["enabled"]
        except ActionError:
            return None
    finally:
        try:
            common.osascript(tg.SYSTEM_EVENTS, _ESCAPE)
        except ActionError:
            pass
        time.sleep(OPEN_MENU_DELAY)


def probe_state(ctx, rec, open_menu):
    """Перед Cmd+V (вызывается из боевой фазы 1): состояние пункта «Вставить» только чтением; с --open-menu — ещё и после
    открытия меню."""
    try:
        info = tg.read_paste_item(ctx.client)
    except ActionError as e:
        rec["menu_error"] = e.short
        return
    rec["state"] = info["enabled"]
    if not ctx.items_shown:
        ctx.items_shown = True
        ctx.report.say(f"     меню «{info['menu']}», пункты (чтение, без клика): {', '.join(info['items']) or '(пусто)'}")
    if open_menu:
        rec["state_open"] = state_after_open(ctx, info["menu"])


def cell(rec):
    if rec["answer"] is True:
        return "д"
    if rec["answer"] is False:
        return "н"
    return "—" if rec["note"] in (None, "пропущена") else "О"


def run_repeat(ctx, methods, repeat, open_menu):
    """Для каждого способа N попыток боевой фазы 1; скрипт ничего не стирает, Enter не нажимает."""
    attempts = []
    ctx.items_shown = False
    for method in methods:
        ctx.report.say(f"\nСпособ {method}")
        for n in range(1, repeat + 1):
            rec = {"method": method, "n": n, "answer": None, "state": None, "state_open": None, "note": None}
            attempts.append(rec)
            ctx.report.say(f"  Попытка {n}/{repeat}")
            if ctx.input_fn(ERASE_PROMPT).strip().lower() in SKIP:
                rec["note"] = "пропущена"
                ctx.report.say("  пропущена по вашему выбору")
                continue
            try:
                saved = tg._save_clipboard()
            except ActionError as e:
                rec["note"] = e.reply or e.short
                ctx.report.say(f"  [ошибка] буфер обмена — {rec['note']}")
                continue
            started = time.monotonic()
            try:
                try:
                    tg.paste_draft(ctx.client, ctx.contact, TEST_TEXT, tg.paste_wait(), method=method,
                                   probe=lambda: probe_state(ctx, rec, open_menu))
                    ctx.report.say(f"  [ok] боевая фаза 1, способ {method} ({time.monotonic() - started:.1f} с)")
                except ActionError as e:
                    rec["note"] = e.reply or e.short
                    ctx.report.say(f"  [ошибка] боевая фаза 1, способ {method} — {rec['note']} "
                                   f"({time.monotonic() - started:.1f} с)")
            finally:
                ctx.report.step("вернуть прежний буфер обмена", lambda: tg._restore_clipboard(saved))
            shown = f"Вставить перед Cmd+V: {STATE_TEXT[rec['state']]}"
            if open_menu:
                shown += f"; после открытия меню: {STATE_TEXT[rec['state_open']]}"
            ctx.report.say(f"  {shown}")
            if rec["note"] is None:
                rec["answer"] = ask_choice(f"Появился ли текст в поле ввода? (д/н/п или y/n/s) [{shown}]: ",
                                           ctx.input_fn, ctx.out)
    return attempts


def repeat_verdict(attempts, methods):
    """(строки вывода, строки для .env)."""
    lines, env = [], []
    stable, partial = [], []
    for method in methods:
        rows = [r for r in attempts if r["method"] == method and r["note"] != "пропущена"]
        answers = [r["answer"] for r in rows]
        if answers and all(a is True for a in answers):
            stable.append(method)
        elif any(a is True for a in answers):
            partial.append(method)
    best = next((m for m in METHOD_PREFERENCE if m in stable), None)
    if best:
        lines.append(f"Способ «{best}» сработал во всех попытках." + (
            f" Также стабильны: {', '.join(m for m in stable if m != best)}." if len(stable) > 1 else ""))
        env.append(f"TELEGRAM_PASTE_METHOD={best}")
    else:
        if partial:
            lines.append("Ни один способ не сработал во всех попытках; частично сработали: " + ", ".join(partial) + ".")
        else:
            lines.append("Ни один способ не сработал.")
        lines.extend(_nothing_worked_help(heading=False))
    answered = [r for r in attempts if r["answer"] in (True, False) and r["state"] in (True, False)]
    if any(r["method"] in ("keystroke", "keycode") and r["state"] is False and r["answer"] is True for r in attempts):
        lines.append(UNRELIABLE_NOTE + ".")
    elif answered and any(r["answer"] for r in answered) and any(not r["answer"] for r in answered) and all(
            r["answer"] == r["state"] for r in answered):
        lines.append("Состояние «Вставить» совпало с результатом (доступен — текст появился, недоступен — нет): "
                     "режим menu можно использовать как проверку фокуса.")
    refused = [r for r in attempts if r["note"] == tg.FIELD_NOT_FOCUSED]
    if refused:
        lines.append(f"Способ menu отказался нажимать {len(refused)} раз(а): «Вставить» был недоступен.")
    return lines, env


def render_repeat(ctx, attempts, methods):
    ctx.report.say("\nРЕЗУЛЬТАТ")
    ctx.report.say("  Способ × попытка (д — текст появился, н — нет, О — ошибка/отказ, — пропуск):")
    for method in methods:
        row = "  ".join(f"{r['n']}:{cell(r)}" for r in attempts if r["method"] == method)
        ctx.report.say(f"    {method:<10} {row}")
    ctx.report.say("  Подробно: способ | попытка | текст появился | Вставить: доступен/недоступен/не прочитано"
                   + (" | после открытия меню" if any(r["state_open"] is not None for r in attempts) else ""))
    for r in attempts:
        extra = f" | {STATE_TEXT[r['state_open']]}" if r["state_open"] is not None else ""
        note = f" ({r['note']})" if r["note"] and r["note"] != "пропущена" else ""
        ctx.report.say(f"    {r['method']:<10} | {r['n']} | {cell(r)} | {STATE_TEXT[r['state']]}{extra}{note}")


def repeat_summary(attempts, methods):
    return {m: [r["answer"] for r in attempts if r["method"] == m] for m in methods}


def compare_repeat(previous, layout_kind, repeat_results):
    """Влияет ли раскладка: сравнение исходов по способам с прошлым запуском."""
    if not previous or not isinstance(previous.get("repeat"), dict):
        return "Раскладка: сравнить не с чем — " + LAYOUT_WARNING
    prev_kind = previous.get("layout_kind")
    if "?" in (prev_kind, layout_kind) or prev_kind == layout_kind:
        same = "та же раскладка" if prev_kind == layout_kind else "раскладка не определена"
        return f"Раскладка: прошлый запуск — {same}, для сравнения переключите раскладку и запустите ещё раз."

    def outcome(answers):
        answers = [a for a in (answers or []) if a in (True, False)]
        return None if not answers else ("все «д»" if all(answers) else "есть «н»")

    differs = []
    for method, now in repeat_results.items():
        before = outcome(previous["repeat"].get(method))
        after = outcome(now)
        if before and after and before != after:
            differs.append(f"{method}: {_kind_name(prev_kind)} — {before}, {_kind_name(layout_kind)} — {after}")
    if differs:
        return "Раскладка ВЛИЯЕТ: " + "; ".join(differs)
    return (f"Раскладка не влияет: исходы при {_kind_name(prev_kind)} и {_kind_name(layout_kind)} одинаковы "
            f"(по способам, которые удалось сравнить).")


def parse_only(raw):
    if not raw:
        return None
    chosen = set()
    for part in re.split(r"[,\s]+", raw.strip().upper()):
        if part == "З":
            chosen |= {"З1", "З2"}
        elif part in ORDER:
            chosen.add(part)
        elif part:
            raise SystemExit(f"неизвестный вариант {part!r}; допустимо: {', '.join(ORDER)}")
    return chosen


def main(argv=None, input_fn=input, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Диагностика вставки черновика в Telegram (запускает пользователь)")
    parser.add_argument("--to", default=tg.SELF_KEY, help="контакт из contacts.json (по умолчанию «себе»)")
    parser.add_argument("--only", help="прежние варианты вместо повторов: например Е,З1,Д (З = З1 и З2); тогда Г и В "
                                       "запускаются, если названы явно")
    parser.add_argument("--variants", action="store_true",
                        help="прежний полный набор вариантов Е, З1, А, Б, Ж, З2, Д, Г, В, И вместо повторов (Этап 7.1.3)")
    parser.add_argument("--repeat", type=int, default=REPEAT_DEFAULT,
                        help=f"попыток на каждый способ (1–{REPEAT_MAX}, по умолчанию {REPEAT_DEFAULT})")
    parser.add_argument("--methods", default=",".join(tg.PASTE_METHODS),
                        help="способы вставки для сравнения: keystroke,keycode,menu (по умолчанию все три)")
    parser.add_argument("--open-menu", action="store_true",
                        help="в каждой попытке один раз открыть меню «Изменить» кликом по строке меню и закрыть Escape "
                             "(по пунктам не кликает), сравнить состояние «Вставить» до и после")
    args = parser.parse_args(argv)
    only = parse_only(args.only)
    methods, repeat = parse_methods(args.methods), parse_repeat(args.repeat)
    report = Report(out)
    ctx = SimpleNamespace(report=report, input_fn=input_fn, out=out, client=None, contact=None)

    layout, kind = current_layout()
    report.say("Диагностика вставки черновика в Telegram. Enter в Telegram не нажимается.")
    report.say(f"Раскладка сейчас: {layout}")
    report.say(LAYOUT_WARNING)
    mode, unsupported = tg.focus_mode_setting()
    report.say(f"Боевой режим из .env: TELEGRAM_FOCUS_MODE={mode}, TELEGRAM_REOPEN_WAIT={tg.reopen_wait():.1f}, "
               f"TELEGRAM_PASTE_WAIT={tg.paste_wait():.1f}, TELEGRAM_PASTE_METHOD={tg.paste_method_setting()[0]}")
    if unsupported is not None:
        report.say(f"  (TELEGRAM_FOCUS_MODE={unsupported} не поддерживается — боевой код использует reopen)")
    if tg.paste_method_setting()[1] is not None:
        report.say(f"  (TELEGRAM_PASTE_METHOD={tg.paste_method_setting()[1]} не поддерживается — боевой код использует {tg.DEFAULT_PASTE_METHOD})")
    report.say(f"В чат «{args.to}» вставляется только фиксированный тест: «{TEST_TEXT}»"
               + (f" (и «{ASCII_TEXT}» в варианте Ж)." if only is not None else ".") + "\n")

    report.say("1. Разрешения macOS")
    automation = report.step("Автоматизация → System Events",
                             lambda: common.osascript(tg.SYSTEM_EVENTS, _PROBE_AUTOMATION))
    accessibility = automation and report.step(
        "Универсальный доступ (нужен для клавиш)", lambda: common.osascript(tg.SYSTEM_EVENTS, _PROBE_ACCESSIBILITY))
    if not (automation and accessibility):
        report.say("\nВыдайте разрешение для приложения, из которого запущен Jarvis (Терминал, VS Code…), и повторите.")
        return 1

    report.say("\n2. Контакт и клиент Telegram")
    contact = report.step(f"контакт «{args.to}» в contacts.json", lambda: tg.resolve_contact(args.to, args.to))
    client = contact and report.step("клиент Telegram установлен", tg.detect_client)
    if not (contact and client):
        return 1
    ctx.client, ctx.contact = client, contact

    report.say("\n3. Открыть чат и дождаться Telegram на переднем плане (до 5 с)")
    opened = report.step("открыть чат по ссылке", lambda: tg._open_chat(client, contact))
    started = time.monotonic()
    ready = opened and report.step("Telegram впереди, окно есть", lambda: _require(
        tg._wait_window(client), "Telegram не вышел вперёд за 5 с"))
    if not ready:
        report.say("\nСначала исправьте шаг 3 (Telegram должен выходить на передний план), потом повторите.")
        return 1
    report.say(f"     время ожидания: {time.monotonic() - started:.1f} с")

    if only is None and not args.variants:
        return _main_repeat(ctx, args, methods, repeat, layout, kind)

    report.say("\n4. Варианты")
    wanted = lambda vid: only is None or vid in only      # noqa: E731
    results, clipboard = {}, {"applescript": None, "pbcopy": None}
    if wanted("Е"):
        clipboard = check_clipboard(ctx)
    plan = (
        ("З1", "Cmd+V по физической клавише (key code 9)", lambda: _flow(ctx, TEST_TEXT, ONE_WAIT, paste=_paste_keycode), None),
        ("А", "боевой путь (режим и паузы из .env), но буфер возвращается сразу после Cmd+V",
         lambda: tg.paste_draft(client, contact, TEST_TEXT, 0.0), None),
        ("Б", "боевой способ: режим и паузы из .env (TELEGRAM_FOCUS_MODE, TELEGRAM_REOPEN_WAIT, TELEGRAM_PASTE_WAIT)",
         lambda: tg.paste_draft(client, contact, TEST_TEXT, tg.paste_wait()), None),
        ("Ж", "латиница «jarvis test», порядок шагов как в Б", lambda: _flow(ctx, ASCII_TEXT, ONE_WAIT), None),
        ("З2", "меню «Правка → Вставить»", lambda: _flow(ctx, TEST_TEXT, ONE_WAIT, paste=_paste_menu), None),
        ("Д", "чат открывается по ссылке второй раз, пауза 1,0 с, потом Cmd+V",
         lambda: _flow(ctx, TEST_TEXT, ONE_WAIT, reopen=True), None),
    )
    for vid, title, action, risk in plan:
        if wanted(vid):
            results[vid] = run_variant(ctx, vid, title, action, risk=risk)

    safe_worked = bool(_worked(results, *SAFE_VARIANTS))
    risky = (
        ("Г", "набрать пробел, стереть его (Delete один раз), потом Cmd+V",
         lambda: _flow(ctx, TEST_TEXT, ONE_WAIT, before_paste=_type_space_and_delete),
         "ВНИМАНИЕ: будет набран один пробел и нажат Delete ровно один раз. Если фокус окажется не в поле ввода, "
         "Delete в Telegram может вызвать диалог удаления выбранного — тогда нажмите «Отмена»."),
        ("В", "один клик левой кнопкой в поле ввода, потом Cmd+V",
         lambda: _flow(ctx, TEST_TEXT, ONE_WAIT, before_paste=_click_input),
         "ВНИМАНИЕ: будет ОДИН клик мышью в нижней части правой панели окна Telegram. Внизу чата должно быть поле "
         "ввода, а не кнопка (START у бота, «Включить звук», «Вступить»): клик по кнопке её нажмёт."),
    )
    for vid, title, action, risk in risky:
        if not wanted(vid):
            continue
        if only is None and safe_worked:
            results[vid] = "skip-safe"
            report.say(f"\nВариант {vid}: пропущен — {SKIP_RISKY_NOTE}")
            continue
        results[vid] = run_variant(ctx, vid, title, action, risk=risk)

    if wanted("И") and not any(v is True for v in results.values()) and any(v is False for v in results.values()):
        results["И"] = manual_check(ctx, clipboard)

    lines, env = analyse(results, clipboard)
    previous = read_json(LAST_FILE, None)
    report.say("\nРЕЗУЛЬТАТ")
    names = {"З1": "З1 key code 9", "А": "А буфер сразу", "Б": "Б буфер через 1,0 с", "Ж": "Ж латиница",
             "З2": "З2 меню", "Д": "Д чат дважды", "Г": "Г пробел+Delete", "В": "В клик", "И": "И вручную"}
    for vid in ("З1", "А", "Б", "Ж", "З2", "Д", "Г", "В", "И"):
        if vid in results:
            value = results[vid]
            shown = {True: "текст появился", False: "текста нет", None: "пропущен", "skip-safe": SKIP_RISKY_NOTE}[value]
            report.say(f"  {names[vid]}: {shown}")
    if clipboard.get("applescript") is not None or clipboard.get("pbcopy") is not None:
        report.say("  Е буфер: AppleScript — %s, pbcopy — %s" % tuple(
            {True: "совпадает", False: "не совпадает", None: "не проверено"}[clipboard.get(k)]
            for k in ("applescript", "pbcopy")))
    report.say("")
    for line in lines:
        report.say(f"  Вывод: {line}")
    if any(v is False for v in results.values()) and not any(v is True for v in results.values()):
        for line in _nothing_worked_help():
            report.say(f"  {line}")
    if env:
        report.say("  Строки для .env (будут подключены отдельным шагом, боевой код их пока не читает):")
        for line in env:
            report.say(f"    {line}")
    report.say(f"  {compare_with_last(previous, kind, results)}")
    try:
        atomic_write_json(LAST_FILE, summary_for_file(layout, kind, results, clipboard))
    except OSError as e:
        report.say(f"  (итог запуска сохранить не удалось: {e.strerror})")
    report.say("\nНапоминание: в чате «" + args.to + "» мог остаться черновик «" + TEST_TEXT + "». Он не отправлен — "
               "сотрите его вручную.")
    return 0


def _main_repeat(ctx, methods_args, methods, repeat, layout, kind):
    report = ctx.report
    args = methods_args
    report.say(f"\n4. Повторы боевой фазы 1: способы {', '.join(methods)}, по {repeat} попытк(и/ок) каждый. "
               f"Черновики стираете вы, Enter не нажимается.")
    if args.open_menu:
        report.say("   --open-menu: в каждой попытке один клик по строке меню «Изменить» и Escape (по пунктам не кликаем).")
    attempts = run_repeat(ctx, methods, repeat, args.open_menu)
    render_repeat(ctx, attempts, methods)
    lines, env = repeat_verdict(attempts, methods)
    previous = read_json(LAST_FILE, None)
    report.say("")
    for line in lines:
        report.say(f"  Вывод: {line}")
    if env:
        report.say("  Строка для .env:")
        for line in env:
            report.say(f"    {line}")
    summary = repeat_summary(attempts, methods)
    report.say(f"  {compare_repeat(previous, kind, summary)}")
    try:
        atomic_write_json(LAST_FILE, summary_for_file(layout, kind, {}, {}, repeat=summary))
    except OSError as e:
        report.say(f"  (итог запуска сохранить не удалось: {e.strerror})")
    report.say("\nНапоминание: в чате «" + args.to + "» мог остаться черновик «" + TEST_TEXT + "». Он не отправлен — "
               "сотрите его вручную.")
    return 0


def _require(condition, message):
    if not condition:
        raise ActionError(message, message)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="     · %(message)s")   # шаги фазы 1 (без текста) видны на экране
    sys.exit(main())
