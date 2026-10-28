"""Типичные галлюцинации Whisper на тишине и шуме: фразы из субтитров, которыми его учили. Их никто не говорил."""
import re

# Нормализованные (нижний регистр, «е» вместо «ё», без знаков препинания). Совпадение — вся фраза или её начало
# (за ним может идти имя автора: «субтитры сделал dimatorzok»)
BLACKLIST = (
    "продолжение следует",
    "субтитры сделал",
    "субтитры создавал",
    "субтитры делал",
    "спасибо за просмотр",
    "благодарю за просмотр",
    "подписывайтесь на канал",
    "редактор субтитров",
)


def normalize(text):
    return " ".join(re.findall(r"\w+", str(text).lower().replace("ё", "е")))


def is_blacklisted(text):
    heard = normalize(text)
    if not heard:
        return False
    return any(heard == entry or heard.startswith(entry + " ") for entry in BLACKLIST)
