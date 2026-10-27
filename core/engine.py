import speech_recognition as sr
import pyttsx3
import threading


class SpeechEngine:
    def __init__(self):
        # На Mac используется драйвер 'nsss' (NSSpeechSynthesizer)
        self.engine = pyttsx3.init('nsss')
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()

        # Настройка голоса (попытка найти русский)
        voices = self.engine.getProperty('voices')
        for voice in voices:
            if 'ru' in voice.languages or 'Russian' in voice.name:
                self.engine.setProperty('voice', voice.id)
                break

        # Скорость речи (на Mac лучше ставить чуть медленнее стандартной)
        self.engine.setProperty('rate', 170)

    def speak(self, text):
        print(f"🗣 Assistant: {text}")
        # pyttsx3 на Mac иногда капризничает в потоках, используем блокировку
        self.engine.say(text)
        self.engine.runAndWait()

    def listen(self):
        with self.microphone as source:
            print("🎤 Слушаю...")
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            try:
                audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=5)
                text = self.recognizer.recognize_google(audio, language="ru-RU")
                print(f"👤 Вы: {text}")
                return text.lower()
            except sr.WaitTimeoutError:
                return None
            except sr.UnknownValueError:
                return None
            except Exception as e:
                print(f"Ошибка микрофона: {e}")
                return None