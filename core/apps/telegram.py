"""Telegram: открыть чат и отправить сообщение через приложение на Mac (клиент не скриптуется: ссылки tg:// и System Events).

Отправка двухфазная и всегда с подтверждением:
  фаза 1 (prepare, ДО вопроса): проверки, чат по ссылке, текст в поле ввода как черновик через буфер обмена
          (прежний буфер возвращается всегда), пользователь видит черновик на экране;
  фаза 2 (run, ТОЛЬКО после «да»): сверка с черновиком, Telegram в фокусе, Enter. Любая ошибка — без повторов.
«Нет» или таймаут: черновик остаётся в чате (клавиши не нажимаются), пользователю об этом говорится.
Доставку проверить нельзя (ответ Telegram не читается). Контакты — contacts.json (core/contacts.py), только руками.
"""
import hashlib
import logging
import os
import plistlib
import re
import threading
import time
from pathlib import Path

from core import contacts as contacts_store
from core.apps import common
from core.apps.registry import CONFIRM, SAFE, AppTool, words_of
from core.errors import ActionError
from core.memory import contains_secret

log = logging.getLogger(__name__)

SYSTEM_EVENTS = "System Events"
CLIENT_PATHS = [Path("/Applications/Telegram.app"), Path("/Applications/Telegram Desktop.app"),
                Path.home() / "Applications" / "Telegram.app", Path.home() / "Applications" / "Telegram Desktop.app"]
KNOWN_BUNDLES = ("ru.keepcoder.Telegram", "org.telegram.desktop")   # «Telegram» для macOS, Telegram Desktop

MAX_TEXT = 500
VOICE_FULL = 200            # до скольки символов вопрос озвучивается целиком
OVERLAP_MIN = 0.6           # доля слов текста, которые есть в исходной фразе
STATE_TTL = 120.0           # черновик живёт дольше 20 с подтверждения с запасом
WINDOW_WAIT_TENTHS = 50     # ждать окно Telegram до 5 секунд
PASTE_DELAY = 0.6           # чат успевает загрузиться и получить фокус
AFTER_PASTE_DELAY = 0.4     # Cmd+V успевает вставить текст до возврата буфера
BEFORE_ENTER_DELAY = 0.4

UNKNOWN_CONTACT = "Не знаю такого контакта. Добавьте его в contacts.json, инструкция в docs/telegram.md."
UNKNOWN_SELF = "Не знаю такого контакта. Добавьте «себе» в contacts.json, инструкция в docs/telegram.md."

# «Себе»: слова пользователя, которые ведут на контакт «себе» из contacts.json (и только на него)
SELF_KEY = "себе"
SELF_SPOKEN = {"себе", "мне", "самому себе", "в избранное", "себе в избранное"}
# Что модель иногда подставляет вместо сказанного пользователем; работает, только если во фразе есть слово из SELF_SPOKEN
MODEL_SELF_NAMES = {"saved messages", "saved", "избранное", "self", "me", "myself"}
_SELF_PHRASE_WORDS = (("себе",), ("мне",), ("в", "избранное"))
NOT_IN_PHRASE = "Этого получателя нет в вашей фразе"
NOT_ASKED = "Не уверен, что вы просили отправить именно это"
SECRET_REFUSAL = "Такое я отправлять не буду."
CLIPBOARD_NOT_TEXT = "В буфере обмена не текст, я его не сохраню. Скопируйте текст или очистите буфер."
DISCARD_NOTE = "Отменил, черновик остался в чате, можете стереть его вручную."
NOT_FOCUSED = "Telegram не в фокусе"

# Типы буфера обмена из `clipboard info`: есть текст и нет ничего «кроме текста» — можно сохранить и вернуть
_TEXT_TYPES = {"«class utf8»", "«class ut16»", "string", "unicode text", "«class rtf »", "«class html»", "text",
               "«class sdat»"}
_NON_TEXT_MARKERS = ("pngf", "tiff", "jpeg", "gif", "furl", "pdf", "bmp", "8bps", "icns", "picture", "file", "mov",
                     "sound", "movie")

_FRONT = ["on run argv",
          'tell application "System Events" to return bundle identifier of (first process whose frontmost is true)',
          "end run"]
_WAIT_WINDOW = ["on run argv",
                "set bid to item 1 of argv",
                "set tenths to (item 2 of argv) as integer",
                'tell application "System Events"',
                "repeat tenths times",
                "set found to (every process whose bundle identifier is bid)",
                "if (count of found) > 0 then",
                "set p to item 1 of found",
                "if (frontmost of p) and ((count of windows of p) > 0) then return \"ok\"",
                "end if",
                "delay 0.1",
                "end repeat",
                "end tell",
                "return \"timeout\"",
                "end run"]
_PASTE = ["on run argv", 'tell application "System Events" to keystroke "v" using command down', "end run"]
_ENTER = ["on run argv", 'tell application "System Events" to key code 36', "end run"]
_CLIPBOARD_INFO = ["clipboard info"]

_lock = threading.Lock()
_state = None   # единственный черновик: {"contact", "text_hash", "client", "created"}


# --- Клиент и ссылки ---

def detect_client(paths=None):
    """Bundle id установленного клиента: «Telegram» для macOS, иначе Telegram Desktop. Telegram не запускается."""
    for path in (CLIENT_PATHS if paths is None else paths):
        try:
            with open(Path(path) / "Contents" / "Info.plist", "rb") as f:
                bundle = plistlib.load(f).get("CFBundleIdentifier")
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if bundle in KNOWN_BUNDLES:
            return bundle
    raise ActionError("Telegram не найден среди установленных", "Telegram не установлен")


def chat_url(contact):
    """tg://resolve по username, иначе по телефону. Строится только из полей contacts.json, проверенных регулярками."""
    if contact.username:
        return f"tg://resolve?domain={contact.username}"
    if contact.phone:
        return f"tg://resolve?phone={contact.phone}"
    raise ActionError("у контакта нет username и телефона", "у контакта не указан username или телефон")


def _open_chat(client, contact):
    common.run_cmd(["open", "-b", client, chat_url(contact)])


def _wait_window(client):
    """Telegram впереди и с окном (до 5 с). False — не дождались."""
    out = common.osascript(SYSTEM_EVENTS, _WAIT_WINDOW, client, WINDOW_WAIT_TENTHS, timeout=10)
    return out.strip() == "ok"


def _frontmost():
    return common.osascript(SYSTEM_EVENTS, _FRONT).strip()


# --- Буфер обмена (только текст: он возвращается точно) ---

def _utf8_env():
    return {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}


def _save_clipboard():
    """(есть ли что возвращать, текст). Не текст (картинка, файл) — отказ: его не вернуть."""
    info = common.osascript(SYSTEM_EVENTS, _CLIPBOARD_INFO).strip()
    names = [part.strip().lower() for part in info.split(", ")[0::2]] if info else []
    if not names:
        return False, ""
    if any(marker in name for name in names for marker in _NON_TEXT_MARKERS) or not (set(names) & _TEXT_TYPES):
        raise ActionError("в буфере обмена не текст", "в буфере обмена не текст", CLIPBOARD_NOT_TEXT)
    return True, common.run_cmd(["pbpaste"], env=_utf8_env())


def _restore_clipboard(saved):
    had, text = saved
    common.run_cmd(["pbcopy"], stdin=text if had else "", env=_utf8_env())


# --- Проверки ---

def _overlap(text, phrase):
    """Доля слов текста, которые есть во фразе пользователя (по основам)."""
    phrase_words = words_of(phrase)
    words = words_of(text)
    if not words:
        return 0.0

    def present(word):
        for pw in phrase_words:
            if pw == word:
                return True
            if len(word) > 3 and len(pw) > 3 and (pw.startswith(word[:5]) or word.startswith(pw[:5])):
                return True
        return False
    return sum(1 for w in words if present(w)) / len(words)


def phrase_has_self_word(phrase):
    """Во фразе пользователя есть «себе», «мне» или «в избранное» (слова подряд, без падежей и нечёткости)."""
    words = words_of(phrase)
    return any(all(words[i + j] == w for j, w in enumerate(seq))
               for seq in _SELF_PHRASE_WORDS for i in range(len(words) - len(seq) + 1))


def is_self_request(to, phrase):
    """`to` — это «себе» (как сказал пользователь или как переименовала модель: Saved Messages, Избранное, Me…) И во
    фразе есть слово «себе». Единственное исключение из правила «получатель назван во фразе»."""
    name = " ".join(words_of(to))
    return (phrase is not None and phrase_has_self_word(phrase)
            and (name in SELF_SPOKEN or name in MODEL_SELF_NAMES))


def resolve_contact(to, phrase=None):
    """Контакт по имени, как его слышит модель. Неизвестный, неоднозначный или не из фразы — ActionError."""
    to = common.clean_text(to, "получатель", common.MAX_LABEL)
    book = contacts_store.load_contacts()
    if is_self_request(to, phrase):
        # Только контакт с ключом «себе», напрямую: ни падежи, ни нечёткий поиск сюда не ведут
        if SELF_KEY not in book:
            raise ActionError("контакта «себе» нет в contacts.json", "не знаю такого контакта", UNKNOWN_SELF)
        return book[SELF_KEY]
    found = contacts_store.find(to, book)
    if not found:
        raise ActionError(f"контакт {to!r} не найден", "не знаю такого контакта", UNKNOWN_CONTACT)
    if len(found) > 1:
        options = ", ".join(c.key for c in found[:4])
        raise ActionError(f"контакт {to!r}: {len(found)} кандидатов", f"Кому именно: {options}?", f"Кому именно: {options}?")
    contact = found[0]
    if phrase is not None and not (contacts_store.matches_phrase(to, phrase)
                                   or contacts_store.matches_phrase(contact.key, phrase)):
        raise ActionError(f"получатель {to!r} не из фразы пользователя", NOT_IN_PHRASE, NOT_IN_PHRASE + ".")
    return contact


def check_text(text, phrase):
    text = common.clean_text(text, "текст", MAX_TEXT, multiline=True)
    if contains_secret(text):
        raise ActionError("telegram: секрет в тексте", SECRET_REFUSAL.rstrip("."), SECRET_REFUSAL)
    if _overlap(text, phrase) < OVERLAP_MIN:
        raise ActionError("telegram: текст не из фразы пользователя", NOT_ASKED, NOT_ASKED + ".")
    return text


def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _question(contact, text):
    full = f"отправить {contact.title} сообщение: «{text}»"
    if len(text) <= VOICE_FULL:
        return full, full
    head = text[:VOICE_FULL].rstrip()
    return (f"отправить {contact.title} сообщение: «{head}», и ещё {len(text) - len(head)} символов, "
            f"полный текст в окне"), full


# --- Фаза 1 ---

def prepare(action, to, text, phrase=""):
    """Все проверки и черновик в Telegram ДО вопроса. Возвращает (вопрос для голоса, вопрос для окна)."""
    global _state
    if action != "send":
        raise ActionError(f"telegram.prepare: {action!r}", "неизвестное действие")
    contact = resolve_contact(to, phrase)
    text = check_text(text, phrase)
    client = detect_client()
    chat_url(contact)    # username или телефон есть
    saved = _save_clipboard()
    pasted = False
    try:
        _open_chat(client, contact)
        if not _wait_window(client):
            raise ActionError("Telegram не открылся за 5 с", "Telegram не открылся")
        time.sleep(PASTE_DELAY)
        common.run_cmd(["pbcopy"], stdin=text, env=_utf8_env())
        common.osascript(SYSTEM_EVENTS, _PASTE)
        pasted = True
        time.sleep(AFTER_PASTE_DELAY)
    finally:
        try:
            _restore_clipboard(saved)
        except ActionError as e:
            log.warning("Не удалось вернуть прежний буфер обмена: %s", e.short)
    with _lock:
        _state = {"contact": contact.key, "text_hash": _text_hash(text), "client": client,
                  "created": time.monotonic(), "pasted": pasted}
    return _question(contact, text)


def discard(action, to, text, phrase=""):
    """«Нет» или таймаут: клавиши НЕ нажимаются (Cmd+A, Delete могли бы выделить и удалить сообщения). Черновик
    остаётся в чате, снимок стирается; пользователю — фраза. Её же пишет в журнал reject_actions."""
    global _state
    with _lock:
        had, _state = _state is not None, None
    return DISCARD_NOTE if had else None


# --- Фаза 2 и открытие чата ---

def _take_state(contact, text):
    global _state
    with _lock:
        state, _state = _state, None    # черновик одноразовый: повторной отправки не будет
    if (state is None or state["contact"] != contact.key or state["text_hash"] != _text_hash(text)
            or time.monotonic() - state["created"] > STATE_TTL):
        raise ActionError("telegram: черновик не найден или изменился", "черновик устарел, повторите команду")
    return state


def _send(to, text, phrase):
    contact = resolve_contact(to, phrase)
    state = _take_state(contact, common.clean_text(text, "текст", MAX_TEXT, multiline=True))
    client = state["client"]
    _open_chat(client, contact)         # тот же чат ещё раз: за 20 с вы могли переключиться
    if not _wait_window(client) or _frontmost() != client:
        raise ActionError("Telegram не в фокусе перед отправкой, Enter не нажат", NOT_FOCUSED)
    time.sleep(BEFORE_ENTER_DELAY)
    common.osascript(SYSTEM_EVENTS, _ENTER)
    return common.done(f"Нажал отправку в чате с {contact.title}")


def run(action, to, text, phrase=""):
    if action == "send":
        return _send(to, text, phrase)    # сюда попадает только подтверждённое действие (уровень CONFIRM)
    if action == "open_chat":
        contact = resolve_contact(to, phrase)
        client = detect_client()
        _open_chat(client, contact)
        if not _wait_window(client):
            raise ActionError("Telegram не открылся за 5 с", "Telegram не открылся")
        return common.done(f"Открыл чат с {contact.title}")
    raise ActionError(f"telegram: неизвестное действие {action!r}", "неизвестное действие")


# --- Быстрые правила ---

_SEND_RE = re.compile(r"^\s*(?:пожалуйста[,\s]+)?(?:напиши|напишите|отправь|отправьте)\s+(?P<name>\S+(?:\s+\S+)?)\s+"
                      r"в\s+(?P<app>телеграм[е]?|telegram)\b[,:]?\s+(?P<text>\S.*?)\s*$", re.IGNORECASE | re.DOTALL)
_OPEN_RE = re.compile(r"^\s*(?:пожалуйста[,\s]+)?(?:открой|открыть|покажи)\s+чат\s+с\s+(?P<name>\S+(?:\s+\S+)?)\s*[.!]?$",
                      re.IGNORECASE)


def _sure_contact(name, phrase):
    """Имя однозначно и уверенно разрешается; иначе None (разбор неуверенный — отдаём модели)."""
    try:
        book = contacts_store.load_contacts()
    except OSError:
        return None
    if is_self_request(name, phrase):
        return book.get(SELF_KEY)
    found = contacts_store.find(name, book)
    return found[0] if len(found) == 1 else None


def _quick(phrase):
    match = _SEND_RE.match(phrase)
    if match:
        name, text = match.group("name"), match.group("text")
        if match.group("app").lower() == "телеграме" and re.match(r"что\s+\S", text, re.IGNORECASE):
            text = re.sub(r"^что\s+", "", text, flags=re.IGNORECASE)    # «в телеграме что …» — союз, не часть текста
        if _sure_contact(name, phrase) is None:
            return None
        return {"args": {"action": "send", "to": name, "text": text.strip()}}
    match = _OPEN_RE.match(phrase)
    if match and _sure_contact(match.group("name"), phrase) is not None:
        return {"args": {"action": "open_chat", "to": match.group("name"), "text": ""}}
    return None


TOOLS = [AppTool(
    name="telegram",
    description="Telegram: open_chat — открыть чат; send — отправить сообщение (всегда с подтверждением пользователя).",
    topic="messaging",
    keywords=("телеграм", "telegram", "сообщени", "напиши", "напишите", "отправь", "отправьте", "чат"),
    actions={"open_chat": SAFE, "send": CONFIRM},
    action_description="open_chat или send",
    params={"to": {"type": "string", "description": "Имя получателя ровно так, как оно сказано в фразе пользователя "
                                                    "(например «маме», «себе»), не переводи и не заменяй"},
            "text": {"type": "string", "description": "Текст сообщения слово в слово; для open_chat пустая строка"}},
    run=run,
    destructive=frozenset({"send"}),
    prepare=prepare,
    discard=discard,
    private=("text",),
    quick=_quick,
)]
