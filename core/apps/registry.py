"""Реестр приложений: один файл в core/apps/ = одно приложение или группа инструментов.

Файл экспортирует список TOOLS из объектов AppTool. Реестр сам находит файлы пакета: новое приложение — новый файл,
остальной код (safety, tools, ai, темы, быстрые правила) не меняется.

AppTool описывает всё про инструмент в одном месте: схему для модели, действия и уровень риска каждого,
функцию выполнения, ключевые слова темы, какие действия возвращают чужой текст (external_data) и какие запрещены
после такого текста.
"""
import importlib
import logging
import pkgutil
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

SAFE, CONFIRM, BLOCKED = "SAFE", "CONFIRM", "BLOCKED"

# Темы для выбора набора инструментов под запрос (бюджет токенов): ключ -> название
TOPICS = {
    "personal": "время и личные данные: таймеры, будильники, напоминания, календарь, заметки",
    "system": "система: заряд, диск, память, экран, яркость, настройки",
    "media": "музыка в Music и Spotify",
    "web": "интернет: поиск, погода, курс, страницы",
    "files": "файлы и Корзина",
    "memory": "память ассистента о пользователе",
    "apps": "приложения и Быстрые команды",
}

_SKIP_MODULES = {"common", "registry"}


@dataclass
class AppTool:
    name: str
    description: str                       # коротко: каждый символ — токены в каждом запросе
    topic: str
    keywords: tuple                        # основы русских слов (нижний регистр), по которым выбирается инструмент
    actions: dict                          # действие -> SAFE | CONFIRM | BLOCKED
    run: Callable                          # run(phrase="", **args) -> Done; ошибка — ActionError
    params: dict = field(default_factory=dict)   # остальные параметры: имя -> {"type", "description", ...}
    action_param: str = "action"
    action_description: str = "Действие"
    external: frozenset = frozenset()      # действия, чей результат содержит чужой текст (письма, события, заметки)
    destructive: frozenset = frozenset()   # удаление, отправка, перемещение, запуск: запрещены после чужих данных
    text_params: tuple = ()                # параметры с текстом, который попадёт к пользователю (метка, заголовок)
    creates: frozenset = frozenset()       # действия, создающие запись с этим текстом (таймер, напоминание, заметка)
    preview: Optional[Callable] = None     # preview(**args) -> «удалить событие …»: читающий поиск цели до вопроса
    quick: Optional[Callable] = None       # quick(phrase) -> {"args": {...}} | None — быстрое правило без модели
    level_fn: Optional[Callable] = None    # level_fn(args) -> (уровень, причина) | None: уровень зависит от аргументов
    cache_check: Optional[Callable] = None  # cache_check(args, phrase) -> bool: можно ли кэшировать для этой фразы

    def schema(self):
        properties = {self.action_param: {"type": "string", "enum": list(self.actions),
                                          "description": self.action_description}}
        properties.update(self.params)
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": properties, "required": list(properties)}}}

    def classify(self, args):
        """(уровень, причина). Неизвестное или нестроковое действие — BLOCKED."""
        args = args if isinstance(args, dict) else {}
        action = args.get(self.action_param)
        if not isinstance(action, str) or action not in self.actions:
            return BLOCKED, f"неизвестное значение {self.action_param} у {self.name}"
        if self.level_fn:
            custom = self.level_fn(args)
            if custom is not None:
                return custom
        level = self.actions[action]
        return level, "только чтение или безобидное изменение" if level == SAFE else "нужно подтверждение"

    def is_external(self, args):
        return isinstance(args, dict) and args.get(self.action_param) in self.external

    def is_destructive(self, args):
        return isinstance(args, dict) and args.get(self.action_param) in self.destructive


_tools = {}
_loaded = False


def load_tools(package="core.apps", path=None):
    """Находит файлы пакета и собирает их TOOLS. path=None — путь самого пакета."""
    found = {}
    if path is None:
        path = importlib.import_module(package).__path__
    for info in pkgutil.iter_modules(path):
        if info.name in _SKIP_MODULES or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        for tool in getattr(module, "TOOLS", ()):
            if not isinstance(tool, AppTool):
                continue
            if tool.name in found:
                raise ValueError(f"инструмент {tool.name} объявлен дважды")
            if tool.topic not in TOPICS:
                raise ValueError(f"{tool.name}: неизвестная тема {tool.topic}")
            found[tool.name] = tool
    return found


def tools():
    global _tools, _loaded
    if not _loaded:
        _tools = load_tools()
        _loaded = True
    return _tools


def get(name):
    return tools().get(name)


def names():
    return set(tools())


def schemas():
    return [t.schema() for t in tools().values()]


def quick_actions(phrase):
    """Быстрые правила файлов реестра: (имя инструмента, args) или None. Первое сработавшее."""
    for tool in tools().values():
        if tool.quick is None:
            continue
        found = tool.quick(phrase)
        if found:
            return tool.name, dict(found["args"])
    return None


def words_of(text):
    return re.findall(r"\w+", str(text).lower().replace("ё", "е"))


def keyword_hits(tool_keywords, words):
    """Сколько основ сработало: слово фразы начинается с основы; основа с пробелом («что у меня») — целая
    последовательность слов подряд. «ё» приравнена к «е»."""
    text = " " + " ".join(words)
    hits = 0
    for key in (k.replace("ё", "е") for k in tool_keywords):
        if " " in key:
            hits += 1 if f" {key} " in text + " " else 0
        else:
            hits += sum(1 for w in words if w.startswith(key))
    return hits
