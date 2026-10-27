import tkinter as tk
from threading import Thread, Lock, Timer
import re
from dotenv import load_dotenv
from core.engine import SpeechEngine
from core.ai import LocalAIClient, describe_action
from modules.mac_control import MacControl

CONFIRM_WORDS = {"да", "подтверждаю", "выполняй"}
CANCEL_WORDS = {"нет", "отмена"}
CONFIRM_TIMEOUT = 15  # секунд на подтверждение, потом действия отменяются


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
        self.root.geometry("400x300")

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

        self.engine = SpeechEngine()
        self.ai = LocalAIClient()
        self.mac = MacControl()
        self.is_listening = False
        self.pending_actions = [] # Действия, ждущие подтверждения
        self.pending_timer = None
        self.pending_lock = Lock()

    def start_listening(self):
        if not self.is_listening:
            self.is_listening = True
            self.status_label.config(text="Слушаю...", fg="black")
            Thread(target=self.background_loop, daemon=True).start()

    def update_status(self, text, color="black"):
        self.status_label.config(text=text, fg=color)

    def set_idle_status(self):
        self.update_status("Слушаю..." if self.is_listening else "Готово", "black")

    def update_log(self, text):
        self.log_label.config(text=text)

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

    def set_pending(self, actions):
        with self.pending_lock:
            if self.pending_timer:
                self.pending_timer.cancel()
            self.pending_actions = actions
            self.pending_timer = Timer(CONFIRM_TIMEOUT, self.expire_pending, args=(actions,))
            self.pending_timer.daemon = True
            self.pending_timer.start()

    def take_pending(self):
        """Забирает отложенные действия и останавливает таймер."""
        with self.pending_lock:
            actions = self.pending_actions
            self.pending_actions = []
            if self.pending_timer:
                self.pending_timer.cancel()
                self.pending_timer = None
            return actions

    def expire_pending(self, actions):
        with self.pending_lock:
            # Действия уже подтверждены, отменены или заменены новыми
            if self.pending_actions is not actions:
                return
            self.pending_actions = []
            self.pending_timer = None
        print(f"⌛ Подтверждение не получено за {CONFIRM_TIMEOUT} с, отменено: {actions}")
        self.engine.speak("Время вышло, отменено.")
        self.set_idle_status()

    def handle_confirmation(self, text):
        answer = parse_confirmation(text)
        if answer is None:
            self.engine.speak("Скажите «да» или «нет».")
            return

        actions = self.take_pending()
        if not actions:
            return # Таймаут успел сработать раньше

        if answer:
            self.update_status("Выполняю...", "green")
            self.engine.speak("Выполняю.")
            print(self.ai.execute_actions(actions))
            self.engine.speak("Готово.")
        else:
            self.engine.speak("Отменено.")
        self.set_idle_status()

    def process_command(self, text, source="voice"):
        """Единая точка входа для обработки команд (голос или текст)."""
        print(f"Обработка ({source}): {text}")

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
        ai_data = self.ai.ask(text)

        # Обработка ответа
        response_text = ai_data.get("response", "")
        immediate, to_confirm = self.ai.split_actions(ai_data.get("actions", []))

        if response_text:
            self.engine.speak(response_text)

        # Открытие приложений и громкость — сразу, без подтверждения
        if immediate:
            self.update_status("Выполняю...", "green")
            print(self.ai.execute_actions(immediate))

        # Закрытие приложений и ссылки — только после подтверждения
        if to_confirm:
            self.set_pending(to_confirm)
            self.update_status("Подтвердите действия...", "orange")
            descriptions = ", ".join(describe_action(a) for a in to_confirm)
            self.engine.speak(f"Нужно подтверждение: {descriptions}. Выполнить?")
        else:
            self.set_idle_status()

    def background_loop(self):
        while True:
            text = self.engine.listen()

            if text:
                self.process_command(text, source="voice")


if __name__ == "__main__":
    root = tk.Tk()
    app = VoiceApp(root)
    root.mainloop()
