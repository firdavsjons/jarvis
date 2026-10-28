"""Распознавание речи (Этап 8.1): локальный Whisper вместо recognize_google, Google — запасной вариант.

Звук с микрофона собирает speech_recognition (SpeechEngine.listen), дальше он идёт в `SpeechToText` (service.py):
движок из .env (factory.py) → фильтр шума и галлюцинаций → текст. Тяжёлые пакеты (pywhispercpp, faster-whisper)
импортируются только при загрузке модели, поэтому `import core.stt` ничего не грузит.
"""
