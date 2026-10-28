"""Системные настройки: открыть панель по названию из таблицы. Строку от модели в URL не подставляем."""
import re
import subprocess

from core.apps import common
from core.apps.registry import SAFE, AppTool, words_of
from core.errors import ActionError

# панель -> (идентификатор в схеме x-apple.systempreferences:, как сказать вслух, слова во фразе пользователя)
# Идентификаторы сверены по /System/Library/ExtensionKit/Extensions (macOS 26.4); «general» — панель самого приложения.
# passwords намеренно нет.
PANES = {
    "wifi": ("com.apple.wifi-settings-extension", "Wi-Fi", ("wifi", "вайфай")),
    "bluetooth": ("com.apple.BluetoothSettings", "Bluetooth", ("bluetooth", "блютуз", "блютус", "блютуза")),
    "sound": ("com.apple.Sound-Settings.extension", "звука", ("звука", "звук", "звуков")),
    "display": ("com.apple.Displays-Settings.extension", "экрана", ("экрана", "экран", "дисплея", "монитора")),
    "battery": ("com.apple.Battery-Settings.extension", "батареи", ("батареи", "батарея", "аккумулятора")),
    "notifications": ("com.apple.Notifications-Settings.extension", "уведомлений", ("уведомлений", "уведомления")),
    "focus": ("com.apple.Focus-Settings.extension", "фокуса", ("фокуса", "фокус")),
    "keyboard": ("com.apple.Keyboard-Settings.extension", "клавиатуры", ("клавиатуры", "клавиатура")),
    "privacy": ("com.apple.settings.PrivacySecurity.extension", "конфиденциальности",
                ("конфиденциальности", "приватности", "безопасности")),
    "accessibility": ("com.apple.Accessibility-Settings.extension", "универсального доступа",
                      ("универсального доступа", "доступности")),
    "storage": ("com.apple.settings.Storage", "хранилища", ("хранилища", "хранилище", "диска", "памяти")),
    "general": ("com.apple.systempreferences.GeneralSettings", "основных", ("основные", "основных", "общие", "общих")),
    "network": ("com.apple.Network-Settings.extension", "сети", ("сети", "сеть", "интернета")),
}
URL_PREFIX = "x-apple.systempreferences:"
OPEN_WORDS = {"открой", "открыть", "покажи", "зайди", "перейди", "в", "на"}
FILLERS = {"пожалуйста", "мне", "а"}
_SETTINGS_RE = re.compile(r"^настройк\w*$")


def pane_url(pane):
    if pane not in PANES:
        raise ActionError(f"settings: неизвестная панель {pane!r}", "такой панели нет в списке")
    return URL_PREFIX + PANES[pane][0]


def pane_from_phrase(phrase):
    """«открой настройки Wi-Fi» -> "wifi"; иначе None. Фраза целиком: глагол, «настройки», название панели."""
    words = [w for w in words_of(phrase) if w not in FILLERS]
    # «Wi-Fi» распознаётся как «wi fi», «вай фай»
    text = " ".join(words).replace("wi fi", "wifi").replace("вай фай", "вайфай")
    words = text.split()
    if not words or words[0] not in OPEN_WORDS:
        return None
    rest = [w for w in words if w not in OPEN_WORDS]
    settings = [w for w in rest if _SETTINGS_RE.match(w)]
    names = [w for w in rest if w not in settings]
    if len(settings) != 1 or not names:
        return None
    name = " ".join(names)
    for pane, (_, _, keywords) in PANES.items():
        if name in keywords:
            return pane
    return None


def run(pane, phrase=""):
    url = pane_url(pane)
    try:
        result = subprocess.run(["open", url], capture_output=True, text=True, timeout=common.TIMEOUT)
    except (subprocess.SubprocessError, OSError) as e:
        raise ActionError(str(e), "не удалось открыть настройки")
    if result.returncode != 0:
        raise ActionError((result.stderr or "").strip() or f"open: код {result.returncode}",
                          "не удалось открыть настройки")
    return common.done(f"Открыл настройки {PANES[pane][1]}")


def _quick(phrase):
    pane = pane_from_phrase(phrase)
    return {"args": {"pane": pane}} if pane else None


def _cache_check(args, phrase):
    return pane_from_phrase(phrase) == args.get("pane")


TOOLS = [AppTool(
    name="settings",
    description="Открыть панель Системных настроек.",
    topic="system",
    keywords=("настройк", "панел"),
    actions={name: SAFE for name in PANES},
    action_param="pane",
    action_description="Панель",
    run=run,
    quick=_quick,
    cache_check=_cache_check,
)]
