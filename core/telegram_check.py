"""Диагностика вставки черновика в Telegram: python -m core.telegram_check (запускает пользователь, не тесты).

Что делает: проверяет разрешения, открывает чат «себе» из contacts.json, ждёт Telegram на переднем плане и дважды
вставляет ФИКСИРОВАННЫЙ тест «проверка Jarvis (черновик)» через буфер обмена и Cmd+V:
  А) прежний буфер возвращается сразу после Cmd+V;
  Б) прежний буфер возвращается через 1,0 с после Cmd+V.
После каждого варианта спрашивает, появился ли текст в поле ввода, и по ответам делает вывод и подсказывает значение
TELEGRAM_PASTE_WAIT для .env.

Чего НЕ делает никогда: не нажимает Enter (и вообще ничего, кроме Cmd+V), не пишет никому, кроме чата «себе», не
стирает черновик, не выводит username и телефоны. Черновики стираете вы, вручную.
"""
import logging
import sys
import time

from core import contacts as contacts_store
from core.apps import common
from core.apps import telegram as tg
from core.errors import ActionError

TEST_TEXT = "проверка Jarvis (черновик)"
VARIANTS = (("А", "прежний буфер возвращается сразу после Cmd+V", 0.0),
            ("Б", "прежний буфер возвращается через 1,0 с после Cmd+V", 1.0))
YES, NO = {"д", "да", "y", "yes"}, {"н", "нет", "n", "no"}

_PROBE_AUTOMATION = ['tell application "System Events" to return name of first process whose frontmost is true']
_PROBE_ACCESSIBILITY = ['tell application "System Events" to return count of windows of '
                        '(first process whose frontmost is true)']

CONCLUSIONS = {
    (False, True): ("Гонка с возвратом буфера: Telegram читает буфер не сразу. Работает вариант Б.",
                    "TELEGRAM_PASTE_WAIT=1.0"),
    (True, True): ("Работают оба варианта: задержка не обязательна, но запас безопаснее.", "TELEGRAM_PASTE_WAIT=1.0"),
    (False, False): ("Не работает ни один вариант: причина не в возврате буфера. Проверьте: (1) Универсальный доступ "
                     "для этого терминала (шаг 1); (2) что Telegram был впереди (шаг 3); (3) что фокус в поле ввода "
                     "чата — кликните в поле и повторите; (4) не открыт ли в Telegram режим, где поле скрыто.", None),
    (True, False): ("Нестабильно: вариант А сработал, Б нет. Повторите запуск; если повторится — оставьте "
                    "TELEGRAM_PASTE_WAIT=1.0 и сообщите результат.", "TELEGRAM_PASTE_WAIT=1.0"),
}


class Report:
    def __init__(self, out):
        self.out = out
        self.failed = False

    def say(self, text=""):
        print(text, file=self.out)

    def step(self, title, func):
        """Выполняет шаг, печатает [ok]/[ошибка] и время. Возвращает результат функции; ошибка — None."""
        started = time.monotonic()
        try:
            result = func()
        except ActionError as e:
            self.failed = True
            self.say(f"  [ошибка] {title} — {e.reply or e.short} ({time.monotonic() - started:.1f} с)")
            return None
        self.say(f"  [ok] {title} ({time.monotonic() - started:.1f} с)")
        return True if result is None else result


def conclude(a_ok, b_ok):
    """(вывод, строка для .env или None)."""
    return CONCLUSIONS[(bool(a_ok), bool(b_ok))]


def ask_yes_no(question, input_fn, out):
    for _ in range(3):
        answer = input_fn(question).strip().lower()
        if answer in YES:
            return True
        if answer in NO:
            return False
        print("Ответьте «д» или «н».", file=out)
    return False


def run_variant(report, client, contact, label, description, wait, input_fn):
    report.say(f"\nВариант {label}: {description}")
    input_fn("Если в поле ввода Telegram есть текст, сотрите его вручную, затем нажмите Enter здесь (в терминале): ")
    try:
        saved = tg._save_clipboard()
    except ActionError as e:
        report.say(f"  [ошибка] буфер обмена — {e.reply or e.short}")
        return None
    pasted = []

    def paste():
        tg.paste_draft(client, contact, TEST_TEXT, wait)
        pasted.append(True)

    try:
        report.step(f"открыть чат, Telegram впереди, Cmd+V, пауза {wait:.1f} с", paste)
    finally:
        report.step("вернуть прежний буфер обмена", lambda: tg._restore_clipboard(saved))
    if not pasted:
        return None
    return ask_yes_no("Появился ли текст в поле ввода? (д/н): ", input_fn, report.out)


def main(argv=None, input_fn=input, out=sys.stdout):
    report = Report(out)
    report.say("Диагностика вставки черновика в Telegram. Enter в Telegram не нажимается.")
    report.say(f"В чат «себе» вставляется только фиксированный тест: «{TEST_TEXT}».\n")

    report.say("1. Разрешения macOS")
    automation = report.step("Автоматизация → System Events", lambda: common.osascript(tg.SYSTEM_EVENTS, _PROBE_AUTOMATION))
    accessibility = automation and report.step(
        "Универсальный доступ (Cmd+V работает только с ним)",
        lambda: common.osascript(tg.SYSTEM_EVENTS, _PROBE_ACCESSIBILITY))
    if not (automation and accessibility):
        report.say("\nВыдайте разрешение для приложения, из которого запущен Jarvis (Терминал, VS Code…), и повторите.")
        return 1

    report.say("\n2. Контакт «себе» и клиент Telegram")
    contact = report.step("контакт «себе» в contacts.json", lambda: _self_contact())
    client = contact and report.step("клиент Telegram установлен", tg.detect_client)
    if not (contact and client):
        return 1

    report.say("\n3. Открыть чат «себе» и дождаться Telegram на переднем плане (до 5 с)")
    opened = report.step("открыть чат по ссылке", lambda: tg._open_chat(client, contact))
    started = time.monotonic()
    ready = opened and report.step("Telegram впереди, окно есть", lambda: _require(tg._wait_window(client),
                                                                                  "Telegram не вышел вперёд за 5 с"))
    if ready:
        report.say(f"     время ожидания: {time.monotonic() - started:.1f} с")
    else:
        report.say("\nСначала исправьте шаг 3 (Telegram должен выходить на передний план), потом повторите.")
        return 1

    report.say("\n4. Вставка черновика")
    results = {}
    for label, description, wait in VARIANTS:
        results[label] = run_variant(report, client, contact, label, description, wait, input_fn)
    if None in results.values():
        report.say("\nНе удалось выполнить оба варианта, вывод сделать нельзя.")
        return 1

    verdict, env_line = conclude(results["А"], results["Б"])
    report.say("\nРЕЗУЛЬТАТ")
    report.say(f"  Вариант А (сразу): {'текст появился' if results['А'] else 'текста нет'}")
    report.say(f"  Вариант Б (через 1,0 с): {'текст появился' if results['Б'] else 'текста нет'}")
    report.say(f"  Вывод: {verdict}")
    if env_line:
        report.say(f"  Строка для .env: {env_line}")
    report.say("\nНапоминание: в чате «Избранное» остался черновик «" + TEST_TEXT + "». Он не отправлен — "
               "сотрите его вручную.")
    return 0


def _self_contact():
    book = contacts_store.load_contacts()
    contact = book.get(tg.SELF_KEY)
    if contact is None:
        raise ActionError("нет контакта «себе»", "нет контакта «себе»", tg.UNKNOWN_SELF)
    return contact


def _require(condition, message):
    if not condition:
        raise ActionError(message, message)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="     · %(message)s")   # шаги фазы 1 (без текста) видны на экране
    sys.exit(main())
