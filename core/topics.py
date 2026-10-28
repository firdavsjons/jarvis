"""Выбор набора инструментов под запрос (бюджет токенов Groq): схемы только подходящих тем + базовый набор.

Ключевые слова — основы русских слов: слово фразы подходит, если начинается с основы. Инструменты реестра
(core/apps/) приносят свои слова и тему; для старых инструментов таблица LEGACY ниже.
Не нашлось ничего — базовый набор + веб + more_tools(area): модель сама просит нужную тему (один лишний запрос).
"""
import json

from core.apps import registry
from core.apps.registry import keyword_hits, words_of

BASE = ("open_app", "quit_app", "set_volume", "get_time", "find_apps")
MORE_TOOLS = "more_tools"
MAX_REQUEST_CHARS = 6500      # схемы одного запроса
TYPICAL_REQUEST_CHARS = 3500  # типичный запрос по одной теме
CHARS_PER_TOKEN = 3.5

# имя -> (тема, ключевые слова). Новые инструменты сюда не добавлять: у них есть файл в core/apps/
LEGACY = {
    "list_apps": ("apps", ("приложен", "установлен", "программ")),
    "open_url": ("web", ("сайт", "ссылк", "http", "ютуб", "youtube", "гугл", "google", "яндекс", "браузер", "url")),
    "trash_file": ("files", ("файл", "папк", "корзин", "удали", "убери", "документ")),
    "web_search": ("web", ("найди", "найти", "поищи", "поиск", "загугли", "новост", "интернет", "узнай", "расскажи")),
    "get_weather": ("web", ("погод", "температур", "дожд", "снег", "прогноз", "холодно", "жарко", "градус")),
    "get_exchange_rate": ("web", ("курс", "валют", "доллар", "евро", "рубл", "сомони", "юан")),
    "read_page": ("web", ("прочита", "прочти", "перескажи", "страниц", "ссылк", "сайт")),
    "remember": ("memory", ("запомни", "запиши", "запиш", "учти")),
    "remember_alias": ("memory", ("запомни", "запиши", "запиш", "учти", "называю")),
    "list_memory": ("memory", ("помнишь", "знаешь", "памят", "помнить", "обо")),
    "forget_memory": ("memory", ("забудь", "забыть")),
    "clear_memory": ("memory", ("забудь", "очисти", "сотри")),
    "system_info": ("system", ("заряд", "батаре", "аккумулят", "диск", "памят", "оператив", "процессор", "нагрузк",
                               "аптайм", "wifi", "вайфай", "вай", "айпи", "ip", "macos", "версия", "компьютер",
                               "запущен", "активн", "сеть", "сети", "место")),
    "display": ("system", ("яркост", "ярче", "темнее", "тёмн", "темн", "светл", "тема", "тему", "заблок", "скриншот",
                           "снимок", "экран", "дисплей", "погаси")),
    "media": ("media", ("музык", "пауз", "трек", "песн", "играй", "играет", "плеер", "spotify", "спотифай", "music",
                        "следующ", "предыдущ", "продолж")),
}


def catalog():
    """имя -> (тема, ключевые слова) для всех инструментов, кроме базовых."""
    items = dict(LEGACY)
    for tool in registry.tools().values():
        items[tool.name] = (tool.topic, tool.keywords)
    return items


def size(tools):
    return len(json.dumps(tools, ensure_ascii=False))


def estimate_tokens(chars):
    return round(chars / CHARS_PER_TOKEN)


def topic_tools(topic):
    return [name for name, (t, _) in catalog().items() if t == topic]


def select(phrase, all_tools, extra_topics=(), carry=()):
    """Схемы для запроса: (список схем, подробности). all_tools — полный список схем (core.tools.TOOLS).

    extra_topics — темы, которые модель попросила через more_tools; carry — инструменты прошлой реплики
    («а завтра?» без ключевых слов). Больше MAX_REQUEST_CHARS — отбрасываются инструменты с наименьшим числом
    совпадений, и добавляется more_tools: недостающее модель запросит сама."""
    by_name = {t["function"]["name"]: t for t in all_tools}
    words = words_of(phrase)
    items = catalog()
    scores = {}
    for name, (_, keywords) in items.items():
        score = keyword_hits(keywords, words) + (0.5 if name in carry else 0)
        if score > 0:
            scores[name] = score
    chosen = sorted(scores, key=lambda n: (-scores[n], n))
    for topic in extra_topics:
        chosen += [n for n in topic_tools(topic) if n not in chosen]
    fallback = not chosen
    if fallback:
        chosen = topic_tools("web")

    def build(names, with_more):
        ordered = [*BASE, *names, *([MORE_TOOLS] if with_more else [])]
        return [by_name[n] for n in ordered if n in by_name]

    with_more = fallback
    tools = build(chosen, with_more)
    dropped = []
    while size(tools) > MAX_REQUEST_CHARS and chosen:
        dropped.append(chosen.pop())   # меньше всего совпадений — последним
        with_more = True
        tools = build(chosen, with_more)
    return tools, {"topics": sorted({items[n][0] for n in chosen}), "fallback": fallback, "dropped": dropped,
                   "chosen": list(chosen)}
