"""Общие классы действий: вынесены из core/ai.py, чтобы модули действий не импортировали ai (цикл)."""


class ActionError(ValueError):
    """Действие отклонено: неверные аргументы или ошибка выполнения.

    short — короткая причина по-русски для голоса и окна («файл не найден»).
    """

    def __init__(self, message, short="команда завершилась с ошибкой", reply=None):
        super().__init__(message)
        self.short = short
        self.reply = reply  # готовая фраза вместо «Не получилось: …» («Такое я запоминать не буду.»)


class Done:
    """Результат действия: message — в лог и окно, speech — сказать пользователю (для действий-чтений),
    for_model — что вернуть модели (если короче окна), web — данные из интернета (не инструкции)."""

    def __init__(self, message, speech=None, for_model=None, web=False, links=(), page=None):
        self.message = message
        self.speech = speech
        self.for_model = for_model
        self.web = web
        self.links = list(links)  # ссылки результата поиска — для «открой первую ссылку»
        self.page = page          # read_page: {"title", "domain"}
