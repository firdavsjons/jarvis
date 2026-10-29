import logging
import queue
import re
import time
import tkinter as tk
from threading import Thread, Lock, Timer, Event
from dotenv import load_dotenv
from core.engine import SpeechEngine
from core.ai import BLOCKED_REPLY, AIClient, describe_action, normalize_ai_response, summarize_results
from core.cache import CACHE_FILE, CLEARED_REPLY, CommandCache, is_forget_cache_command
from core.apps import registry, telegram
from core.fragments import CANCEL_WORDS, CONFIRM_WORDS
from core.history import FORGET_REPLY, is_forget_command
from core.links import parse_link_command, parse_read_command
from core.memory import MEMORY_FILE, UserMemory, is_clear_memory_command
from core import scheduler as timers
from core.journal import loggable_action, loggable_phrase, setup_journal
from core.llm import build_default_client
from core.logger import LOG_DIR, setup_logging
from core.speech_text import confirmation_question
from core.stt.truncated import TRUNCATED_REPLY, is_truncated
from core.wake import LISTENING_REPLY, WakeGate, build_gate, is_stop_phrase
from modules.mac_control import MacControl

log = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 20  # секунд на подтверждение после вопроса, потом действия отменяются
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
        self.engine.start_stt()   # локальная модель распознавания грузится в фоне; пока не готова — Google или просьба подождать
        self.ai = AIClient(llm=build_default_client(usage_path=LOG_DIR / "llm_usage.json"),
                           memory=UserMemory(MEMORY_FILE), cache=CommandCache(CACHE_FILE))
        # «Секунду, ищу.» — из таймера, параллельно с медленным веб-инструментом
        self.ai.on_slow_tool = self.quiet_speak
        self.mac = MacControl()
        # Таймеры и будильники: фоновый планировщик; озвучка через общий замок SpeechEngine.speak, окно диалога не открывает
        self.scheduler = timers.set_scheduler(timers.Scheduler(timers.TIMERS_FILE))
        self.scheduler.on_fire = timers.make_fire_handler(self.quiet_speak)
        self.scheduler.on_missed = timers.make_missed_handler(self.quiet_speak)
        self.scheduler.start()
        # Слово-активатор «Джарвис» (WAKE_WORD=off — None): голосовая фраза без обращения вне окна диалога отбрасывается
        self.wake = build_gate(self.engine, self.ai, CONFIRM_TIMEOUT)
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

    def quiet_speak(self, text):
        """Оповещение, а не ответ на команду («Секунду.», таймер, будильник): окно слова-активатора не открывается."""
        self.engine.speak(text, opens_window=False)

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
        log.info("⌛ Подтверждение не получено за %s с, отменено: %s", CONFIRM_TIMEOUT,
                 [loggable_action(a) for a in actions])
        notes = self.ai.reject_actions(actions, phrase, f"нет ответа за {CONFIRM_TIMEOUT} с", confirmed=False)
        reply = " ".join(notes) if isinstance(notes, list) and notes else "Время вышло, отменено."
        self.ai.remember("(нет ответа)", reply)
        self.engine.speak(reply)
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
            results = self.ai.run_actions(actions, phrase=phrase, confirmed=True)
            ok, summary = self.report_result(results)
            reply = summary if not ok else (summary or "Готово.")
            self.ai.remember(text, reply, results)
            self.engine.speak(reply)
        else:
            notes = self.ai.reject_actions(actions, phrase, "отменено пользователем", confirmed=False)
            reply = " ".join(notes) if isinstance(notes, list) and notes else "Отменено."   # «черновик остался в чате…»
            self.ai.remember(text, reply)
            self.engine.speak(reply)
        self.set_idle_status()

    def report_result(self, results):
        """Пишет результаты в лог и окно. Возвращает (всё ли успешно, что сказать)."""
        ok, summary = summarize_results(results)
        details = "\n".join(r["message"] for r in results)
        log.info("Результат действий:\n%s", details)
        self.update_log(f"{summary or 'Готово.'}\n{details}" if ok else f"{summary}\n{details}")
        return ok, summary

    def pending_is_private(self):
        """Среди ожидающих действий есть отправка сообщения: реплики пользователя в лог не пишем."""
        with self.pending_lock:
            tools = [registry.get(a.get("name")) for a in self.pending_actions if isinstance(a, dict)]
        return any(t is not None and t.private for t in tools)

    def process_command(self, text, source="voice", phrase=None):
        """Единая точка входа для обработки команд (голос или текст). phrase — core.wake.Phrase: длина записи и
        уверенность распознавания голосовой фразы.

        Порядок: подтверждение → стоп/отмена → слово «Джарвис»/окно диалога → обрывки и обрезанные → остальное."""
        # Длина записи нужна Telegram: длинная запись при коротком тексте — признак неточного распознавания
        telegram.note_recording(phrase.audio_seconds if source == "voice" and phrase is not None else None)
        gate = self.wake if isinstance(getattr(self, "wake", None), WakeGate) else None
        pending = self.has_pending()
        in_window = False          # фраза принята окном диалога (слова-активатора не было): обрезанное начало не проверяем
        if source == "voice" and gate is not None and not pending:
            # До первой записи фразы в лог: отброшенное (нет обращения) не пишется нигде
            decision = gate.check(text, phrase)
            if decision.action == "drop":
                return
            if decision.action == "stop":
                self.set_idle_status()
                return
            if decision.action == "listen":
                # После одного «Джарвис» окно нужно, но при слабой уверенности (0,3–0,4) только «Слушаю.», окно не открывается
                self.engine.speak(LISTENING_REPLY, opens_window=decision.opens_window)
                self.set_idle_status()
                return
            text, in_window = decision.text, decision.in_window

        # Идёт диалог о сообщении (спросили получателя или текст) или ждём подтверждения отправки — любая реплика скрыта
        private = self.ai.message_dialog_open() is True or self.pending_is_private() is True
        log.info("Обработка (%s): %s", source, loggable_phrase(text, private))

        # 0. Если ждём подтверждения — это ответ на вопрос («стоп» и «хватит» — отмена, слово не нужно)
        if self.has_pending():
            self.handle_confirmation("отмена" if gate is not None and is_stop_phrase(text) else text)
            self.ai.close_message_dialog()
            return

        # Обрезанная фраза («позвонить ма», «напиши себе в»): модель не спрашиваем, она бы догадалась за пользователя
        if source == "voice" and is_truncated(text, in_window=in_window):
            log.info("Отброшено (обрезанная фраза)")
            self.engine.speak(TRUNCATED_REPLY)
            self.set_idle_status()
            return

        # «да» / «нет» / «отмена» без ожидающего действия: нечего подтверждать (не модели и не инструментам). Если
        # ассистент только что задал вопрос («Поставить внутренний будильник?»), фраза — ответ на него и идёт дальше
        nothing = self.ai.bare_confirmation_reply(text)
        if isinstance(nothing, str) and nothing:
            self.ai.close_message_dialog()
            self.engine.speak(nothing)
            self.set_idle_status()
            return

        # 1. «Забудь разговор» — до MacControl и до модели
        if is_forget_command(text):
            self.ai.forget()
            self.engine.speak(FORGET_REPLY)
            self.set_idle_status()
            return

        if is_forget_cache_command(text):
            if self.ai.cache is not None:
                self.ai.cache.clear()
            self.engine.speak(CLEARED_REPLY)
            self.set_idle_status()
            return

        # «Забудь всё обо мне» — удаление всей памяти, сразу с вопросом (без модели)
        if is_clear_memory_command(text):
            pending = [{"name": "clear_memory", "args": {}}]
            self.ai.remember(text, confirmation_question(pending))
            self.ask_confirmation(pending, text)
            return

        # «Прочитай первую ссылку» / «прочитай сайт example.com» — адрес из сохранённых ссылок или из фразы
        read = parse_read_command(text)
        if read is not None:
            kind, value = read
            self.update_status("Читаю страницу...", "blue")
            self.deliver(self.ai.read_page_turn(text, number=value if kind == "number" else None,
                                                url=value if kind == "url" else None), text)
            return

        # «Открой первую ссылку» — адрес из сохранённого результата поиска, не от модели
        link_number = parse_link_command(text)
        if link_number is not None:
            self.deliver(self.ai.open_search_link(link_number, text), text)
            return

        # Быстрое правило с обязательным вопросом («напиши маме в телеграм …»): разбор кодом, дальше проверки, черновик
        # и вопрос подтверждения — как у ответа модели
        quick_turn = self.ai.quick_confirm(text)
        if quick_turn is not None:
            self.update_status("Готовлю сообщение...", "blue")
            self.deliver(quick_turn, text)
            return

        # 2. Проверяем жесткие системные команды (громкость и т.д.)
        sys_response = self.mac.execute(text)
        if sys_response:
            self.engine.speak(sys_response)
            self.set_idle_status()
            return

        # 3. Модель: инструменты (или JSON у моделей без них) — решение и честный текст строит ai.respond
        self.update_status("Думаю...", "blue")
        self.deliver(self.ai.respond(text), text)

    def deliver(self, turn, text):
        """Озвучить итог Turn: отказ, результаты, вопрос на подтверждение."""
        if turn.blocked:
            self.update_log("⛔ Отклонено: " + ", ".join(describe_action(a) if isinstance(a, dict) else str(a)
                                                          for a in turn.blocked))
            self.engine.speak(turn.speech or BLOCKED_REPLY)
            self.set_idle_status()
            return

        if turn.results:
            self.report_result(turn.results)
        # Звучит только после выполнения инструментов; при ошибке — «Не получилось: …»
        if turn.speech:
            self.engine.speak(turn.speech)

        # CONFIRM — только после «да»; таймер ожидания стартует после вопроса
        if turn.pending:
            self.ask_confirmation(turn.pending, text)
        else:
            self.set_idle_status()

    def ask_confirmation(self, pending, phrase):
        self.set_pending(pending, phrase)
        self.update_status("Подтвердите действия...", "orange")
        self.update_log("Подтвердите: " + "; ".join(describe_action(a) for a in pending))
        self.engine.speak(confirmation_question(pending))
        self.start_pending_timer(pending)

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
            tail = self.engine.tail_remaining()
            if tail > 0:
                time.sleep(min(tail, 0.1))   # хвост речи ассистента (SPEECH_TAIL_PAUSE)
                continue

            started = time.monotonic()
            text = self.engine.listen()
            if self.stop_event.is_set():
                break
            if not text:
                self.engine.flush_notices()     # «Не удалось распознать речь», «Распознавание загружается…»
                continue
            if self.engine.speech_overlaps(started):
                # Во время записи ассистент заговорил — микрофон мог услышать его самого
                log.info("Отброшено (ассистент говорил во время записи)")
                self.engine.flush_notices()
                continue

            if self.engine.is_echo(text):
                # Микрофон услышал саму речь ассистента (его вопрос подтверждения, ответ): не команда
                log.info("Отброшено (эхо ассистента)")
                self.engine.flush_notices()
                continue

            # Оповещения распознавания (один раз за запуск) звучат после проверок: иначе сама фраза была бы отброшена
            self.engine.flush_notices()
            self.process_command(text, source="voice", phrase=self.engine.last_phrase)
        log.info("Цикл прослушивания остановлен")


if __name__ == "__main__":
    log_file = setup_logging()
    journal_file = setup_journal()
    log.info("Jarvis запущен, лог: %s, журнал действий: %s", log_file, journal_file)
    root = tk.Tk()
    app = VoiceApp(root)
    root.mainloop()
