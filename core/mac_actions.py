"""Управление Mac тремя группами инструментов: system_info, display, media.

Каждая группа — один инструмент с параметром action из фиксированного набора (меньше схем — меньше токенов).
Все действия SAFE: только чтение или обратимые безобидные изменения. Команды запускаются списком аргументов
(без shell), с timeout; из ответа системы наружу идут только разобранные поля. Имя приложения и числа в
AppleScript передаются через argv, а не вставляются в текст скрипта.
"""
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from core import installed_apps
from core.errors import ActionError, Done

log = logging.getLogger(__name__)

TIMEOUT = 10
MEDIA_APPS = ("Music", "Spotify")
BRIGHTNESS_STEP = 10     # «ярче» / «темнее» — на столько процентов (как ±10 у громкости)
VOLUME_STEP = 10
LIST_LIMIT = 15

# Разрешения macOS: текст для голоса — без стрелок, для окна и журнала — со стрелками
PERMISSION_VOICE = ("Нужно разрешение: Системные настройки, Конфиденциальность и безопасность, {name}, "
                    "для приложения, из которого запущен Jarvis.")
PERMISSION_WINDOW = ("Нужно разрешение: Системные настройки → Конфиденциальность и безопасность → {name} "
                     "для приложения, из которого запущен Jarvis.")
AUTOMATION = "Автоматизация"
ACCESSIBILITY = "Универсальный доступ"
# Коды ошибок osascript: нет разрешения на Автоматизацию / нет Универсального доступа
_AUTOMATION_CODES = ("-1743",)
_ACCESSIBILITY_CODES = ("-25211", "-1719", "(1002)")  # 1002 — «osascript не разрешено нажимать клавиши»

BRIGHTNESS_UNAVAILABLE = "регулировка яркости недоступна"


# --- Запуск команд ---

def _permission_error(stderr):
    """ActionError про разрешение, если osascript ответил кодом отказа; иначе None."""
    for codes, name in ((_AUTOMATION_CODES, AUTOMATION), (_ACCESSIBILITY_CODES, ACCESSIBILITY)):
        if any(code in stderr for code in codes):
            voice = PERMISSION_VOICE.format(name=name)
            return ActionError(PERMISSION_WINDOW.format(name=name), voice, voice)
    return None


def run_capture(args, timeout=TIMEOUT):
    """Запускает команду списком аргументов, возвращает stdout. Нет прав / ошибка / таймаут — ActionError."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ActionError(f"таймаут {timeout} с", "команда не ответила вовремя")
    except OSError as e:
        raise ActionError(str(e), "команда не запустилась")
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise _permission_error(stderr) or ActionError(stderr or f"код возврата {result.returncode}")
    return result.stdout or ""


def osascript(lines, *argv):
    """Многострочный AppleScript (по одной строке на -e); значения — через argv."""
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    return run_capture(args + [str(a) for a in argv]).strip()


def _done(text, message=None):
    """text — фраза без точки: в шаблон нескольких результатов и в лог; озвучка — с точкой."""
    return Done(message or text, f"{text}.", for_model=text)


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _count(n, one, few, many):
    return f"{n} {_plural(n, one, few, many)}"


def _number(x):
    """11.2 -> «11,2», 16.0 -> «16»."""
    text = f"{x:.1f}"
    return text[:-2] if text.endswith(".0") else text.replace(".", ",")


def _gigabytes(x):
    """16 -> «16 гигабайт», 11.2 -> «11,2 гигабайта» (дробным — всегда родительный)."""
    whole = round(x) if abs(x - round(x)) < 0.05 else None
    return f"{whole} {_plural(whole, 'гигабайт', 'гигабайта', 'гигабайт')}" if whole is not None \
        else f"{_number(x)} гигабайта"


def _duration(seconds):
    """Две старшие ненулевые единицы: «5 дней 3 часа», «12 минут»."""
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    parts = [p for p in (
        _count(days, "день", "дня", "дней") if days else "",
        _count(hours, "час", "часа", "часов") if hours else "",
        _count(minutes, "минута", "минуты", "минут") if minutes else "",
    ) if p]
    return " ".join(parts[:2]) or "меньше минуты"


# --- system_info: разбор вывода ---

def parse_battery(output):
    """pmset -g batt -> (процент, состояние, минут до конца или None). Состояние: charging, discharging,
    charged, finishing, ac (питание от сети, не заряжается). Нет батареи — ActionError."""
    for line in output.splitlines():
        match = re.search(r"(\d+)%;\s*([^;]+)", line)
        if not match:
            continue
        percent, raw = int(match.group(1)), match.group(2).strip().lower()
        remaining = re.search(r"(\d+):(\d{2}) remaining", line)
        minutes = int(remaining.group(1)) * 60 + int(remaining.group(2)) if remaining else None
        if raw.startswith("charging"):
            state = "charging"
        elif raw.startswith("finishing"):
            state = "finishing"
        elif raw.startswith("charged"):
            state = "charged"
        elif raw.startswith("discharging"):
            state = "discharging"
        else:
            state = "ac"  # «AC attached; not charging»
        return percent, state, minutes if minutes else None
    raise ActionError("pmset: батареи нет", "у этого Mac нет батареи")


def battery_text(output):
    percent, state, minutes = parse_battery(output)
    status = {"charging": "заряжается", "finishing": "почти заряжен", "charged": "заряжен",
              "discharging": "не заряжается", "ac": "не заряжается"}[state]
    text = f"Заряд {_count(percent, 'процент', 'процента', 'процентов')}, {status}"
    if minutes:
        left = _duration(minutes * 60)
        text += f", до полной зарядки {left}" if state in ("charging", "finishing") else f", хватит на {left}"
    return text


def parse_vm_stat(output):
    """vm_stat -> (размер страницы, {название: страниц})."""
    page = re.search(r"page size of (\d+) bytes", output)
    pages = {m.group(1).strip(): int(m.group(2)) for m in re.finditer(r"^(.+?):\s+(\d+)\.?\s*$", output, re.M)}
    if not page or not pages:
        raise ActionError("vm_stat: неожиданный вывод", "не удалось узнать память")
    return int(page.group(1)), pages


def memory_text(vm_output, memsize):
    """Занято = активная + закреплённая + сжатая память (как «Использовано» в мониторинге системы)."""
    page, pages = parse_vm_stat(vm_output)
    used = (pages.get("Pages active", 0) + pages.get("Pages wired down", 0)
            + pages.get("Pages occupied by compressor", 0)) * page
    gib = 1024 ** 3
    return f"Занято {_gigabytes(used / gib)} памяти из {_gigabytes(memsize / gib)}"


def parse_boottime(output):
    match = re.search(r"sec\s*=\s*(\d+)", output)
    if not match:
        raise ActionError("kern.boottime: неожиданный вывод", "не удалось узнать время работы")
    return int(match.group(1))


def parse_wifi_name(output):
    """networksetup -getairportnetwork -> имя сети или None (нет сети; в новых macOS имя скрыто)."""
    match = re.match(r"Current Wi-Fi Network:\s*(.+)", output.strip())
    name = match.group(1).strip() if match else ""
    return None if not name or "redacted" in name.lower() else name


def wifi_device(output):
    """networksetup -listallhardwareports -> устройство Wi-Fi (en0)."""
    match = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\w+)", output)
    return match.group(1) if match else None


# --- system_info: действия ---

def _battery():
    return _done(battery_text(run_capture(["pmset", "-g", "batt"])))


def _storage():
    usage = shutil.disk_usage("/")
    return _done(f"Свободно {round(usage.free / 1e9)} гигабайт из {round(usage.total / 1e9)}")


def _memory():
    memsize = int(run_capture(["sysctl", "-n", "hw.memsize"]).strip())
    return _done(memory_text(run_capture(["vm_stat"]), memsize))


def _cpu():
    cores = os.cpu_count() or 1
    percent = min(100, round(os.getloadavg()[0] / cores * 100))
    # после «около» — родительный падеж: «около 21 процента», «около 43 процентов»
    word = "процента" if percent % 10 == 1 and percent % 100 != 11 else "процентов"
    return _done(f"Нагрузка процессора около {percent} {word}")


def _uptime():
    boot = parse_boottime(run_capture(["sysctl", "-n", "kern.boottime"]))
    return _done(f"Mac работает {_duration(max(0, time.time() - boot))}")


def _wifi():
    ports = run_capture(["networksetup", "-listallhardwareports"])
    device = wifi_device(ports)
    if device is None:
        raise ActionError("нет порта Wi-Fi", "на этом Mac нет Wi-Fi")
    try:
        name = parse_wifi_name(run_capture(["networksetup", "-getairportnetwork", device]))
    except ActionError:
        name = None
    if name:
        return _done(f"Wi-Fi подключён к сети {name}")
    try:
        connected = bool(run_capture(["ipconfig", "getifaddr", device]).strip())
    except ActionError:
        connected = False  # ipconfig: код 1, если у интерфейса нет адреса
    # macOS 14.4+ прячет имя сети от программ без разрешения на геолокацию
    return _done("Wi-Fi подключён, имя сети недоступно" if connected else "Wi-Fi не подключён")


def _ip():
    """Локальный адрес: UDP-«соединение» ничего не отправляет, только выбирает маршрут."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            address = s.getsockname()[0]
        except OSError:
            raise ActionError("нет маршрута", "нет подключения к сети")
    # Для голоса адрес — «172 точка 20 точка 10 точка 6» (озвучка прочтёт числа словами), в окне — с точками
    return _done(f"Локальный адрес {address.replace('.', ' точка ')}", f"Локальный адрес {address}")


def _macos():
    version = platform.mac_ver()[0]
    return _done(f"macOS {version.replace('.', ' точка ')}", f"macOS {version}")


def _spoken_names(names):
    shown, rest = names[:LIST_LIMIT], len(names) - LIST_LIMIT
    text = ", ".join(shown)
    return f"{text} и ещё {rest}" if rest > 0 else text


def _running_apps():
    out = osascript(['tell application "System Events"', "set names to name of (processes whose visible is true)",
                     "end tell", "set AppleScript's text item delimiters to linefeed", "return names as text"])
    names = [n.strip() for n in out.splitlines() if n.strip()]
    if not names:
        return _done("Видимых приложений нет")
    return _done(f"Открыты: {_spoken_names(names)}", f"Открыты: {', '.join(names)}")


def _frontmost_app():
    name = osascript(['tell application "System Events" to return name of first process whose frontmost is true'])
    return _done(f"Сейчас активно приложение {name}")


SYSTEM_INFO = {
    "battery": _battery, "storage": _storage, "memory": _memory, "cpu": _cpu, "uptime": _uptime,
    "wifi": _wifi, "ip": _ip, "macos": _macos, "running_apps": _running_apps, "frontmost_app": _frontmost_app,
}


def system_info(what):
    func = SYSTEM_INFO.get(what) if isinstance(what, str) else None
    if func is None:
        raise ActionError(f"system_info: неизвестное what {what!r}", "неизвестный запрос")
    return func()


# --- display ---

def _brightness_value(value):
    if isinstance(value, bool):
        raise ActionError(f"яркость должна быть числом: {value!r}", "яркость должна быть числом от 0 до 100")
    try:
        level = int(str(value).strip().rstrip("%").strip())
    except ValueError:
        raise ActionError(f"яркость должна быть числом: {value!r}", "яркость должна быть числом от 0 до 100")
    if not 0 <= level <= 100:
        raise ActionError(f"яркость вне 0–100: {level}", "яркость должна быть числом от 0 до 100")
    return level


# Яркость: встроенной команды в macOS нет, программы brightness нет, а клавиши (key code 144/145) на M1 с macOS 26
# проверены живьём и не меняют яркость. Работает системный DisplayServices (его же использует утилита brightness):
# вызывается в отдельном процессе Python, чтобы сбой системной библиотеки не уронил Jarvis. Установки и разрешений
# не нужно. Режимы: delta — сдвиг в процентах, set — абсолютное значение; печатает итоговый процент.
_BRIGHTNESS_SCRIPT = """
import ctypes, sys
try:
    ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
    ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    display = cg.CGMainDisplayID()
    mode, amount = sys.argv[1], int(sys.argv[2])
    current = ctypes.c_float()
    if ds.DisplayServicesGetBrightness(display, ctypes.byref(current)) != 0:
        sys.exit(3)
    target = amount / 100 if mode == "set" else current.value + amount / 100
    target = max(0.0, min(1.0, target))
    if ds.DisplayServicesSetBrightness(display, ctypes.c_float(target)) != 0:
        sys.exit(3)
    print(round(target * 100))
except (OSError, AttributeError, ValueError):
    sys.exit(3)
"""


def _set_brightness(mode, amount):
    """Возвращает итоговую яркость в процентах. Нет доступа к экрану (внешний монитор, Mac без экрана) —
    «регулировка яркости недоступна»."""
    try:
        out = run_capture([sys.executable, "-I", "-c", _BRIGHTNESS_SCRIPT, mode, str(amount)])
        return int(out.strip())
    except (ActionError, ValueError) as e:
        raise ActionError(f"яркость: {e}", BRIGHTNESS_UNAVAILABLE)


def _brightness_done(percent):
    return _done(f"Яркость {_count(percent, 'процент', 'процента', 'процентов')}")


def _brightness_up(value):
    return _brightness_done(_set_brightness("delta", BRIGHTNESS_STEP))


def _brightness_down(value):
    return _brightness_done(_set_brightness("delta", -BRIGHTNESS_STEP))


def _brightness_set(value):
    return _brightness_done(_set_brightness("set", _brightness_value(value)))


def _dark_mode(script_value, text):
    def run(value):
        osascript(['tell application "System Events" to tell appearance preferences to '
                   f"set dark mode to {script_value}"])
        return _done(text)
    return run


def _lock_screen(value):
    osascript(['tell application "System Events" to keystroke "q" using {control down, command down}'])
    return _done("Экран заблокирован")


def _sleep_display(value):
    run_capture(["pmset", "displaysleepnow"])
    return _done("Экран выключен")


# Куда можно сохранять скриншот: значение value -> (папка в домашней, как сказать вслух). Путь только из этой таблицы
SCREENSHOT_FOLDERS = {
    "": ("Desktop", "на рабочем столе"), "desktop": ("Desktop", "на рабочем столе"),
    "documents": ("Documents", "в документах"), "downloads": ("Downloads", "в загрузках"),
}
SCREENSHOT_FOLDER_HINT = "Могу сохранить на рабочий стол, в документы или в загрузки"
SCREEN_RECORDING = "Запись экрана"
SCREENSHOT_RETRY_DELAY = 1.0   # screencapture сразу после разблокировки иногда отвечает «could not create image»
_NO_IMAGE = "could not create image from display"


def screenshot_path(folder, now=None):
    """<папка>/Снимок экрана ГГГГ-ММ-ДД ЧЧ.ММ.СС.png; существующий файл не перезаписывается (суффикс 2, 3…)."""
    now = now or datetime.now()
    base = f"Снимок экрана {now:%Y-%m-%d %H.%M.%S}"
    path, n = Path(folder) / f"{base}.png", 1
    while path.exists():
        n += 1
        path = Path(folder) / f"{base} ({n}).png"
    return path


def _capture(path):
    """screencapture; «could not create image from display» — повтор через секунду, потом причина про права."""
    for attempt in (1, 2):
        try:
            run_capture(["screencapture", "-x", str(path)])
            return
        except ActionError as e:
            if _NO_IMAGE not in str(e).lower():
                raise
            if attempt == 1:
                time.sleep(SCREENSHOT_RETRY_DELAY)
    voice = PERMISSION_VOICE.format(name=SCREEN_RECORDING)
    raise ActionError(PERMISSION_WINDOW.format(name=SCREEN_RECORDING), voice, voice)


def _screenshot(value, home=None):
    key = value.strip().casefold() if isinstance(value, str) else None
    if key not in SCREENSHOT_FOLDERS:
        raise ActionError(f"screenshot: папка {value!r} не поддерживается", SCREENSHOT_FOLDER_HINT,
                          SCREENSHOT_FOLDER_HINT + ".")
    folder, where = SCREENSHOT_FOLDERS[key]
    path = screenshot_path(Path(home or Path.home()) / folder)
    _capture(path)
    if not path.exists() or path.stat().st_size == 0:
        raise ActionError(f"screencapture не создал {path}", "снимок не получился, проверьте разрешение «Запись экрана»")
    return _done(f"Скриншот сохранён {where}", f"Скриншот сохранён: {path}")


DISPLAY = {
    "brightness_up": _brightness_up, "brightness_down": _brightness_down, "brightness_set": _brightness_set,
    "dark_mode_on": _dark_mode("true", "Тёмная тема включена"),
    "dark_mode_off": _dark_mode("false", "Тёмная тема выключена"),
    "dark_mode_toggle": _dark_mode("not dark mode", "Тема переключена"),
    "lock_screen": _lock_screen, "sleep_display": _sleep_display, "screenshot": _screenshot,
}


def display(action, value=""):
    func = DISPLAY.get(action) if isinstance(action, str) else None
    if func is None:
        raise ActionError(f"display: неизвестное action {action!r}", "неизвестное действие")
    return func(value)


# --- media ---

_USING_MUSIC = "using terms from application \"Music\""  # Music и Spotify понимают одни и те же команды плеера


def _media_script(body):
    """Команда плеера для приложения из argv: на компиляции нужен словарь, поэтому using terms."""
    return ["on run argv", _USING_MUSIC, "tell application (item 1 of argv)", *body, "end tell",
            "end using terms from", "end run"]


_PLAYER_COMMANDS = {"play": "play", "pause": "pause", "toggle": "playpause", "next": "next track",
                    "previous": "previous track"}
_PLAYER_SPEECH = {"play": "Играю", "pause": "Пауза", "toggle": "Переключил", "next": "Следующий трек",
                  "previous": "Предыдущий трек"}


def canonical_media_app(app):
    for name in MEDIA_APPS:
        if isinstance(app, str) and app.strip().casefold() == name.casefold():
            return name
    raise ActionError(f"media: неизвестное приложение {app!r}", "поддерживаются Music и Spotify")


def is_running(app):
    """Запущено ли приложение — без запуска (tell application запустило бы его само)."""
    out = osascript(["on run argv", 'tell application "System Events" to return (name of processes) contains '
                     "(item 1 of argv)", "end run"], app)
    return out.strip().lower() == "true"


def player_state(app):
    """(состояние, название, исполнитель); состояние: playing / paused / stopped."""
    out = osascript(_media_script(["set pst to player state as text", 'if pst is "stopped" then return pst',
                                   "return pst & linefeed & (name of current track) & linefeed & "
                                   "(artist of current track)"]), app)
    state, *rest = out.split("\n")
    return state.strip(), (rest[0].strip() if rest else ""), (rest[1].strip() if len(rest) > 1 else "")


def pick_media_app():
    """Для быстрых команд без названия: играющее приложение, иначе любое запущенное, иначе None."""
    running = []
    for app in MEDIA_APPS:
        if installed_apps.resolve_app_name(app) is None:
            continue
        try:
            if is_running(app):
                running.append(app)
        except ActionError:
            continue
    for app in running:
        try:
            if player_state(app)[0] == "playing":
                return app
        except ActionError:
            continue
    return running[0] if running else None


def _now_playing(app):
    state, name, artist = player_state(app)
    if state == "stopped" or not name:
        return _done("Сейчас ничего не играет")
    track = f"{name} — {artist}" if artist else name
    return _done(f"{'Играет' if state == 'playing' else 'На паузе'} трек {track}")


def _change_volume(app, delta):
    out = osascript(_media_script([
        "set v to (sound volume) + (item 2 of argv as integer)", "if v > 100 then set v to 100",
        "if v < 0 then set v to 0", "set sound volume to v", "return v"]), app, delta)
    level = int(out.strip() or 0)
    return _done(f"Громкость {app} {level}")


def media(action, app):
    app = canonical_media_app(app)
    if action not in {*_PLAYER_COMMANDS, "now_playing", "volume_up", "volume_down"}:
        raise ActionError(f"media: неизвестное action {action!r}", "неизвестное действие")
    if installed_apps.resolve_app_name(app) is None:
        raise ActionError(f"{app} не найден среди установленных", f"{app} не установлен")
    # Кроме play: не запускать плеер ради «следующий трек»
    if action != "play" and not is_running(app):
        raise ActionError(f"{app} не запущен", f"{app} не запущен")
    if action == "now_playing":
        return _now_playing(app)
    if action in ("volume_up", "volume_down"):
        return _change_volume(app, VOLUME_STEP if action == "volume_up" else -VOLUME_STEP)
    osascript(_media_script([_PLAYER_COMMANDS[action]]), app)
    return _done(_PLAYER_SPEECH[action])


# Имя инструмента -> функция; значения action — ключи таблиц (их же берут schemas и safety)
TOOL_FUNCS = {"system_info": system_info, "display": display, "media": media}
SYSTEM_INFO_VALUES = tuple(SYSTEM_INFO)
DISPLAY_VALUES = tuple(DISPLAY)
MEDIA_VALUES = ("play", "pause", "toggle", "next", "previous", "now_playing", "volume_up", "volume_down")
