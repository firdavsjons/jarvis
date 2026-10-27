"""Текст для озвучки: без ссылок, путей, Markdown и эмодзи. В окне и логе остаётся исходный текст."""
import os
import re
import unicodedata
from urllib.parse import urlparse

DOMAIN_WORDS = {
    "youtube": "ютуб",
    "google": "гугл",
    "github": "гитхаб",
    "wikipedia": "википедия",
    "com": "ком",
    "ru": "ру",
    "org": "орг",
}

# Куда указывает путь: «с рабочего стола», «из документов»
FOLDER_PLACES = {
    "desktop": "с рабочего стола",
    "documents": "из документов",
    "downloads": "из загрузок",
}

EXTENSIONS = {
    "txt": "тэ-экс-тэ",
    "pdf": "пэ-дэ-эф",
    "docx": "ворд",
    "doc": "ворд",
    "xlsx": "эксель",
    "xls": "эксель",
    "jpg": "джипег",
    "jpeg": "джипег",
    "png": "пэ-эн-гэ",
    "mp3": "эм-пэ-три",
    "mp4": "эм-пэ-четыре",
    "zip": "зип",
    "py": "пэ-игрек",
    "json": "джейсон",
    "html": "эйч-ти-эм-эль",
    "csv": "цэ-эс-вэ",
    "md": "эм-дэ",
    "pptx": "пауэрпойнт",
    "ppt": "пауэрпойнт",
    "mov": "мов",
    "wav": "вав",
    "heic": "хейк",
    "gif": "гиф",
    "rtf": "эр-тэ-эф",
    "dmg": "дэ-эм-гэ",
    "js": "джей-эс",
}

# Латинские буквы по-русски — для расширений не из словаря
LETTERS = {
    "a": "а", "b": "бэ", "c": "цэ", "d": "дэ", "e": "е", "f": "эф", "g": "гэ", "h": "аш", "i": "и",
    "j": "жи", "k": "ка", "l": "эль", "m": "эм", "n": "эн", "o": "о", "p": "пэ", "q": "ку", "r": "эр",
    "s": "эс", "t": "тэ", "u": "у", "v": "вэ", "w": "дабл-ю", "x": "экс", "y": "игрек", "z": "зэт",
}

KEEP_SYMBOLS = {"№", "°"}  # категория So, но читаются нормально

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:https?://)?[^)\s]*\)")  # [текст](ссылка) -> текст
_PAREN_URL_RE = re.compile(r"\s*\(\s*https?://[^)\s]*\s*\)")        # (https://...) -> ""
_URL_RE = re.compile(r"https?://[^\s<>\"«»()\[\]]+")
_EXT_RE = "|".join(sorted(EXTENSIONS, key=len, reverse=True))
_SEG = r"[^/\s,;:!?«»\"()\[\]]+"
_NAME = r"[^/\s,;:!?«»\"()\[\].]+"
# Папки в пути могут содержать пробелы (их ограничивает «/»): /Users/x/Projects All/a.txt.
# Последняя часть — с пробелами, только если заканчивается известным расширением: «мой отчёт.pdf»
_PATH_RE = re.compile(
    rf"(?<![\w/~.])~?/(?:{_SEG}(?: {_SEG})*/)*"
    rf"(?:{_NAME}(?: {_NAME})*\.(?:{_EXT_RE})(?!\w)|{_SEG})",
    re.IGNORECASE,
)
# Имя файла без пути: «test.txt» -> «test точка тэ-экс-тэ». Только известные расширения: youtube.com не трогаем
_FILE_NAME_RE = re.compile(rf"(?<![\w/.\-])([\w\-]+)\.({_EXT_RE})(?![\w])", re.IGNORECASE)
_MD_CHARS_RE = re.compile(r"[*#`]+")
_MD_UNDERSCORE_RE = re.compile(r"(?<!\w)_+|_+(?!\w)")  # _курсив_ — да, my_file — нет
_SYMBOL_RUN_RE = re.compile(r"[^\w\s]{3,}")                        # «=====», «-->», «...»
_LONG_TOKEN_RE = re.compile(r"\b(?=\w*\d)(?=\w*[^\W\d])\w{16,}\b")  # хеши, токены, id
_TRAILING_PUNCT = ".,;:!?"


def domain_for_speech(host):
    """www.youtube.com -> «ютуб точка ком»."""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return " точка ".join(DOMAIN_WORDS.get(part, part) for part in host.split(".") if part)


def url_for_speech(url):
    try:
        host = urlparse(url).hostname
    except ValueError:
        host = None
    return domain_for_speech(host) if host else "ссылка"


def extension_for_speech(ext):
    ext = ext.lower()
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    return "-".join(LETTERS.get(c, c) for c in ext)


def place_for_speech(path):
    """Папка, в которой лежит файл: «с рабочего стола», «из папки Проекты»."""
    parent = os.path.dirname(os.path.expanduser(path).rstrip("/"))
    if not parent or parent == "/":
        return ""
    if os.path.realpath(parent) == os.path.realpath(os.path.expanduser("~")):
        return "из домашней папки"
    folder = os.path.basename(parent)
    return FOLDER_PLACES.get(folder.lower(), f"из папки {folder}")


def split_file_name(path):
    name = os.path.basename(path.rstrip("/"))
    stem, ext = os.path.splitext(name)
    if not stem:  # «.zshrc»
        return name, ""
    return stem, ext.lstrip(".")


def path_for_speech(path):
    """/Users/x/Desktop/отчёт.txt -> «отчёт точка тэ-экс-тэ, с рабочего стола»."""
    stem, ext = split_file_name(path)
    spoken = f"{stem} точка {extension_for_speech(ext)}" if ext else stem
    place = place_for_speech(path)
    return f"{spoken}, {place}" if place else spoken


def _replace_keeping_tail(func):
    """Хвостовую пунктуацию («…/отчёт.txt.») оставляем в тексте, а не в пути."""
    def replace(match):
        raw = match.group(0)
        core = raw.rstrip(_TRAILING_PUNCT)
        return func(core) + raw[len(core):]
    return replace


def _strip_emoji(text):
    return "".join(
        c for c in text
        if c in KEEP_SYMBOLS or not (unicodedata.category(c) in ("So", "Sk") or c in "‍️")
    )


def prepare_for_speech(text):
    if not text:
        return ""
    s = _MD_LINK_RE.sub(r"\1", text)
    s = _PAREN_URL_RE.sub("", s)
    s = _URL_RE.sub(_replace_keeping_tail(url_for_speech), s)
    s = _PATH_RE.sub(_replace_keeping_tail(path_for_speech), s)
    s = _FILE_NAME_RE.sub(lambda m: f"{m.group(1)} точка {extension_for_speech(m.group(2))}", s)
    s = _strip_emoji(s)
    s = _MD_CHARS_RE.sub(" ", s)
    s = _MD_UNDERSCORE_RE.sub(" ", s)
    s = _SYMBOL_RUN_RE.sub(" ", s)
    s = _LONG_TOKEN_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s+([,.!?;:])", r"\1", s)


# --- Вопросы подтверждения для голоса ---

def action_for_speech(action):
    """Короткое описание действия для вопроса: без полных путей и ссылок."""
    args = action.get("args") or {}
    name = action.get("name")
    if name == "trash_file":
        path = str(args.get("path", ""))
        kind = "папку" if os.path.isdir(os.path.expanduser(path)) else "файл"
        stem, _ = split_file_name(path)
        place = place_for_speech(path)
        return " ".join(p for p in ["переместить в Корзину", kind, stem, place] if p)
    if name == "open_url":
        return f"открыть сайт {url_for_speech(str(args.get('url', '')))}"
    if name == "quit_app":
        return f"закрыть {args.get('app')}"
    if name == "clear_memory":
        return "удалить всё, что я о вас помню"
    return str(name)


def confirmation_question(actions):
    """«Открыть сайт ютуб точка ком?» или «Нужно подтверждение: …. Выполнить?»."""
    parts = [action_for_speech(a) for a in actions]
    if len(parts) == 1:
        return parts[0][:1].upper() + parts[0][1:] + "?"
    return f"Нужно подтверждение: {', '.join(parts)}. Выполнить?"
