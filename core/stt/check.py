"""Проверка локального распознавания вручную: `python -m core.stt.check [--wav файл] [--mic]`.

Запускает пользователь, не тесты. Без параметров: показывает настройки, наличие пакета и файла модели, загружает
модель (время загрузки и прогрева) — микрофон не включается, звук никуда не уходит. `--wav` распознаёт файл (любой
WAV/AIFF/FLAC; текст печатается в консоль), `--mic` записывает одну фразу с микрофона. Откат на Google здесь отключён:
проверяется только локальный движок. Ничего не скачивает.
"""
import argparse
import importlib.util
import sys
import time

from core import storage
from core.stt.base import Audio, SttError
from core.stt.factory import models_dir, stt_settings
from core.stt.faster import FasterWhisperEngine
from core.stt.prompt import build_prompt
from core.stt.whisper_cpp import WhisperCppEngine

PACKAGES = {"whisper_cpp": "pywhispercpp", "faster_whisper": "faster_whisper"}


def local_engine(settings):
    classes = {"whisper_cpp": WhisperCppEngine, "faster_whisper": FasterWhisperEngine}
    return classes[settings.engine](model=settings.model, language=settings.language, threads=settings.threads,
                                    unload_minutes=0, models_dir=models_dir())


def read_wav(path):
    import speech_recognition as sr
    with sr.AudioFile(path) as source:
        return Audio(sr.Recognizer().record(source))


def record_phrase():
    import speech_recognition as sr
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        print("Говорите (до 20 секунд)...")
        return Audio(recognizer.listen(source, timeout=10, phrase_time_limit=20))


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description="Проверка локального распознавания речи Jarvis")
    parser.add_argument("--wav", help="распознать этот аудиофайл")
    parser.add_argument("--mic", action="store_true", help="записать одну фразу с микрофона")
    args = parser.parse_args(argv)

    settings = stt_settings()
    out(f"Движок: {settings.engine}; откат на Google: {'да' if settings.fallback else 'нет'}; потоков: {settings.threads}; "
        f"выгрузка через {settings.unload_minutes:g} мин; язык: {settings.language}")
    if settings.engine == "google":
        out("Выбран Google: локальной модели нет, проверять нечего (звук уходит в Google).")
        return 0
    engine = local_engine(settings)
    package = PACKAGES[settings.engine]
    out(f"Пакет {package}: {'установлен' if importlib.util.find_spec(package) else 'НЕ установлен'}")
    out(f"Папка моделей: {models_dir()} (данные Jarvis: {storage.DATA_DIR})")
    if settings.engine == "whisper_cpp":
        path = engine.model_path()
        size = f"{path.stat().st_size / 1e6:.0f} МБ" if path.is_file() else "ФАЙЛА НЕТ"
        out(f"Модель: {path.name} — {size}")
    else:
        out(f"Модель: {engine.model_name} (в {models_dir() / 'faster-whisper'})")
    try:
        started = time.monotonic()
        engine.ensure_loaded()
    except SttError as e:
        out(f"Не загружено: {getattr(e, 'reason', e)}")
        return 1
    out(f"Модель загружена и прогрета за {time.monotonic() - started:.1f} с")
    out(f"Подсказка Whisper: {len(build_prompt())} символов")
    audio = read_wav(args.wav) if args.wav else record_phrase() if args.mic else None
    if audio is None:
        return 0
    try:
        result = engine.recognize(audio, build_prompt())
    except SttError as e:
        out(f"Ошибка распознавания: {e}")
        return 1
    out(f"Запись {result.audio_seconds:.1f} с, распознавание {result.seconds:.1f} с"
        + (f", уверенность {result.confidence:.2f}" if result.confidence is not None else ""))
    out("Отброшено как шум/галлюцинация" if result.discarded else f"Текст: {result.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
