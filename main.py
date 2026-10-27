import logging
import queue
import re
import time
import tkinter as tk
from threading import Thread, Lock, Timer, Event
from dotenv import load_dotenv
from core.engine import SpeechEngine
from core.ai import AIClient, describe_action, summarize_results
from core.journal import setup_journal
from core.llm import build_default_client
from core.logger import LOG_DIR, setup_logging
from core.speech_text import confirmation_question
from modules.mac_control import MacControl

log = logging.getLogger(__name__)

CONFIRM_WORDS = {"да", "подтверждаю", "выполняй"}
CANCEL_WORDS = {"нет", "отмена"}
CONFIRM_TIMEOUT = 20  # секунд на подтверждение после вопроса, потом действия отменяются
BLOCKED_REPLY = "Это слишком опасно, я этого не сделаю."
UI_POLL_MS = 100  # как часто главный поток забирает обновления окна из очереди


def parse_confirmation(text):
    """True — подтверждение, False — отмена, None — ответ не распознан.

    Сравниваются целые слова: «когда» не считается за «да». Если есть и «да», и «нет» — отмена.
    """
    words = set(re.findall(r"\w+", text.lower()))
    if words & CANCEL_WORDS:
        return False
    if words & CONFIRM_WORDS:
        return True
    return None


def normalize_ai_response(data):
    """(текст ответа, действия) из результата ai.ask. Неожиданный формат — пустой ответ."""
    if not isinstance(data, dict):
        log.warning("Ответ AI не словарь: %r", data)
        return "", []
    response_text = data.get("response")
    if not isinstance(response_text, str):
        response_text = ""
    return response_text, data.get("actions", [])


class VoiceApp:
    def __init__(self, root):
        load_dotenv()

        self.root = root
        self.root.title("Jarvis (Mac M1 - Terminal AI)")
        self.root.geometry("400x340")

        self.status_label = tk.Label(root, text="Нажми 'Старт'", font=("Helvetica", 16))
        self.status_label.pack(pady=10)

        # UI для текстового ввода
        self.entry = tk.Entry(root, font=("Helvetica", 14))
        self.entry.pack(pady=5, fill=tk.X, padx=20)
        self.entry.bind("<Return>", lambda event: self.on_submit())

        self.send_btn = tk.Button(root, text="Отправить", command=self.on_submit)
        self.send_btn.pack(pady=5)

        self.log_label = tk.Label(root, text="", font=("Helvetica", 10), fg="gray")
        self.log_label.pack(pady=5)

        self.btn = tk.Button(root, text="Старт (Голос)", command=self.start_listening)
        self.btn.pack(pady=10)

        self.stop_btn = tk.Button(root, text="Стоп", command=self.stop_listening)
        self.stop_btn.pack(pady=5)

        self.engine = SpeechEngine()
        self.ai = AIClient(llm=build_default_client(usage_path=LOG_DIR / "llm_usage.json"))
        self.mac = MacControl()
        self.is_listening = False
        self.stop_event = Event()
        self.listen_thread = None
        self.pending_actions = [] # Действия, ждущие подтверждения
        self.pending_phrase = ""  # исходная фраза — для журнала
        self.pending_timer = None
        self.pending_lock = Lock()

        # Виджеты меняет только главный поток: остальные кладут сообщения в очередь
        self.ui_queue = queue.Queue()
        self.root.after(UI_POLL_MS, self.poll_ui)

    def start_listening(self):
        if self.is_listening:
            return
        if self.listen_thread and self.listen_thread.is_alive():
            # Старый цикл ещё ждёт окончания listen() и держит микрофон
            self.update_status("Подождите, микрофон освобождается...", "gray")
            return
        self.is_listening = True
        self.stop_event.clear()
        self.update_status("Калибрую микрофон...", "gray")
        self.listen_thread = Thread(target=self.background_loop, daemon=True)
        self.listen_thread.start()

    def stop_listening(self):
        self.is_listening = False
        self.stop_event.set()
        self.engine.stop_speaking()
        self.update_status("Остановлено", "black")

    # --- Обновление окна (из любого потока) ---

    def update_status(self, text, color="black"):
        self.ui_queue.put(("status", text, color))

    def set_idle_status(self):
        self.update_status("Слушаю..." if self.is_listening else "Готово", "black")

    def update_log(self, text):
        self.ui_queue.put(("log", text))

    def poll_ui(self):
        """Главный поток: применяет накопленные обновления окна."""
        try:
            while True:
                message = self.ui_queue.get_nowait()
                if message[0] == "status":
                    self.status_label.config(text=message[1], fg=message[2])
                elif message[0] == "log":
                    self.log_label.config(text=message[1])
        except queue.Empty:
            pass
        self.root.after(UI_POLL_MS, self.poll_ui)

    def on_submit(self):
        text = self.entry.get()
        if not text:
            return

        self.entry.delete(0, tk.END)
        # Обработка в отдельном потоке, чтобы не морозить UI
        Thread(target=self.process_command, args=(text, "text"), daemon=True).start()

    # --- Ожидание подтверждения ---

    def has_pending(self):
        with self.pending_lock:
            return bool(self.pending_actions)

    def set_pending(self, actions, phrase):
        """Сохраняет действия до ответа. Таймер — отдельно, после озвучки вопроса."""
        with self.pending_lock:
            if self.pending_timer:
                self.pending_timer.cancel()
                self.pending_timer = None
            self.pending_actions = actions
            self.pending_phrase = phrase

    def start_pending_timer(self, actions):
        with self.pending_lock:
            if self.pending_actions is not actions:
                return # Ответ пришёл, пока звучал вопрос
            self.pending_timer = Timer(CONFIRM_TIMEOUT, self.expire_pending, args=(actions,))
            self.pending_timer.daemon = True
            self.pending_timer.start()

    def take_pending(self):
        """Забирает отложенные действия (и исходную фразу) и останавливает таймер."""
        with self.pending_lock:
            actions, phrase = self.pending_actions, self.pending_phrase
            self.pending_actions, self.pending_phrase = [], ""
            if self.pending_timer:
                self.pending_timer.cancel()
                self.pending_timer = None
            return actions, phrase

    def expire_pending(self, actions):
        with self.pending_lock:
            # Действия уже подтверждены, отменены или заменены новыми
            if self.pending_actions is not actions:
                return
            phrase = self.pending_phrase
            self.pending_actions, self.pending_phrase = [], ""
            self.pending_timer = None
        log.info("⌛ Подтверждение не получено за %s с, отменено: %s", CONFIRM_TIMEOUT, actions)
        self.ai.reject_actions(actions, phrase, f"нет ответа за {CONFIRM_TIMEOUT} с", confirmed=False)
        self.engine.speak("Время вышло, отменено.")
        self.set_idle_status()

    def handle_confirmation(self, text):
        answer = parse_confirmation(text)
        if answer is None:
            self.engine.speak("Скажите «да» или «нет».")
            return

        actions, phrase = self.take_pending()
        if not actions:
            return # Таймаут успел сработать раньше

        if answer:
            self.update_status("Выполняю...", "green")
            self.engine.speak("Выполняю.")
            ok, summary = self.report_result(self.ai.run_actions(actions, phrase=phrase, confirmed=True))
            self.engine.speak(summary if not ok else (summary or "Готово."))
        else:
            self.ai.reject_actions(actions, phrase, "отменено пользователем", confirmed=False)
            self.engine.speak("Отменено.")
        self.set_idle_status()

    def report_result(self, results):
        """Пишет результаты в лог и окно. Возвращает (всё ли успешно, что сказать)."""
        ok, summary = summarize_results(results)
        details = "\n".join(r["message"] for r in results)
        log.info("Результат действий:\n%s", details)
        self.update_log(f"{summary or 'Готово.'}\n{details}" if ok else f"{summary}\n{details}")
        return ok, summary

    def process_command(self, text, source="voice"):
        """Единая точка входа для обработки команд (голос или текст)."""
        log.info("Обработка (%s): %s", source, text)

        # 0. Если ждём подтверждения — это ответ на вопрос
        if self.has_pending():
            self.handle_confirmation(text)
            return

        # 1. Проверяем жесткие системные команды (громкость и т.д.)
        sys_response = self.mac.execute(text)
        if sys_response:
            self.engine.speak(sys_response)
            self.set_idle_status()
            return

        # 2. Спрашиваем AI
        self.update_status("Думаю...", "blue")
        response_text, actions = normalize_ai_response(self.ai.ask(text))
        immediate, to_confirm, blocked = self.ai.split_actions(actions)

        # Хоть одно опасное действие — отклоняем весь ответ, включая текст модели
        if blocked:
            self.ai.reject_actions(actions, text, "в ответе есть опасное действие")
            self.update_log("⛔ Отклонено: " + ", ".join(describe_action(a) if isinstance(a, dict) else str(a)
                                                          for a in blocked))
            self.engine.speak(BLOCKED_REPLY)
            self.set_idle_status()
            return

        # SAFE — сначала выполнить, потом сказать правду. Ответ модели («Открываю Telegram») звучит,
        # только если всё получилось и ничего не ждёт подтверждения: иначе он мог бы быть неправдой
        if immediate:
            self.update_status("Выполняю...", "green")
            ok, summary = self.report_result(self.ai.run_actions(immediate, phrase=text))
            if not ok:
                self.engine.speak(summary)
            elif to_confirm:
                if summary:
                    self.engine.speak(summary)  # результаты find_apps / list_apps
            else:
                self.engine.speak(" ".join(p for p in (response_text, summary) if p) or "Готово.")
        elif response_text and not to_confirm:
            self.engine.speak(response_text)

        # CONFIRM — только после «да»; таймер ожидания стартует после вопроса
        if to_confirm:
            self.set_pending(to_confirm, text)
            self.update_status("Подтвердите действия...", "orange")
            self.update_log("Подтвердите: " + "; ".join(describe_action(a) for a in to_confirm))
            self.engine.speak(confirmation_question(to_confirm))
            self.start_pending_timer(to_confirm)
        else:
            self.set_idle_status()

    def background_loop(self):
        try:
            self.engine.calibrate()
        except Exception as e:
            log.error("Не удалось открыть микрофон: %s", e)
            self.is_listening = False
            self.update_status("Ошибка микрофона", "red")
            return
        self.set_idle_status()

        while not self.stop_event.is_set():
            # Пока ассистент говорит, микрофон не слушаем
            if self.engine.speaking.is_set():
                time.sleep(0.1)
                continue

            started = time.monotonic()
            text = self.engine.listen()
            if self.stop_event.is_set():
                break
            if not text:
                continue
            if self.engine.speech_overlaps(started):
                # Во время записи ассистент заговорил — микрофон мог услышать его самого
                log.info("Отброшено (ассистент говорил во время записи): %s", text)
                continue

            self.process_command(text, source="voice")
        log.info("Цикл прослушивания остановлен")


if __name__ == "__main__":
    log_file = setup_logging()
    journal_file = setup_journal()
    log.info("Jarvis запущен, лог: %s, журнал действий: %s", log_file, journal_file)
    root = tk.Tk()
    app = VoiceApp(root)
    root.mainloop()
