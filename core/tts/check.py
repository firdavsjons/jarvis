"""Проверка нейронного голоса вручную: `python -m core.tts.check [--engine sherpa|silero] [--samples [--effect]] [--play]`.

Запускает пользователь (шаги 9.3 и 9.4), не тесты. Микрофон не включается. Без параметров: показывает настройки, наличие пакета
sherpa-onnx и файлов модели (ничего не скачивает), загружает и прогревает голос, замеряет первую фразу и RTF.
`--samples` создаёт WAV для 10 фраз Jarvis в ~/Library/Application Support/Jarvis/tts_samples/ (права 600) и ничего не
играет; `--effect` включает эффект «Jarvis» независимо от .env; `--play` проигрывает фразы через afplay.
`--engine silero` (или TTS_ENGINE=silero в .env) проверяет Silero (Этап 9.4): интерпретатор, воркер, модель, версии torch /
numpy / scipy из воркера, время его загрузки, первую фразу, RTF и пик громкости; `--samples` пишет образцы в
tts_samples/silero_<голос>_final/ (текст проходит prepare_for_speech и словарь произношения, как в приложении).
"""
import argparse
import importlib.util
import logging
import os
import statistics
import sys
import time

from core import storage
from core.tts import effect
from core.tts.base import SILERO_VOICES, TTSUnavailable, tts_settings
from core.tts.factory import build_neural, models_dir
from core.tts.player import AfplayPlayer, TempWavs

SAMPLES_DIR_NAME = "tts_samples"
SAMPLE_PHRASES = (
    ("short", "Готово."),
    ("app", "Открыто приложение Telegram."),
    ("time", "Сейчас вторник, 27 октября, 17:12."),
    ("confirm", "Отправить маме сообщение: «привет, буду через десять минут»?"),
    ("latin", "Запустил Safari и закрыл Visual Studio Code."),
    ("listening", "Слушаю."),
    ("failed", "Не получилось: файл не найден."),
    ("long", "Сегодня в Душанбе пасмурно, температура плюс двенадцать градусов. К вечеру ожидается дождь. "
             "Возьмите с собой зонт."),
    ("warning", "Текст мог распознаться неточно. Проверьте в окне Telegram."),
    ("refusal", "Это слишком опасно, я этого не сделаю."),
)


def samples_dir():
    return storage.DATA_DIR / SAMPLES_DIR_NAME


def write_sample(engine, name, sentence_text, directory):
    """Синтезирует фразу целиком (по предложениям), склеивает и пишет WAV с правами 600. Возвращает (путь, секунды звука)."""
    from core.tts.player import split_sentences
    np = effect.numpy_module()
    chunks, rate = [], None
    for sentence in split_sentences(sentence_text):
        samples, rate = engine._synthesize(sentence)
        chunks.append(np.asarray(samples, dtype=np.float64))
        chunks.append(np.zeros(int(rate * 0.12)))               # пауза между предложениями
    joined = np.concatenate(chunks) if chunks else np.zeros(1)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{name}.wav"
    wavs = TempWavs()
    temp = wavs.write(joined, rate or 22050)
    try:
        os.replace(temp, path)                                   # права 600 сохраняются (создан с 0600)
        os.chmod(path, 0o600)
    finally:
        wavs.cleanup()
    return path, len(joined) / float(rate or 22050)


def silero_sentences(text):
    """Предложения для Silero так же, как в приложении: prepare_for_speech, словарь произношения, нарезка."""
    from core.speech_text import prepare_for_speech
    from core.tts import pronounce
    from core.tts.player import split_sentences
    return split_sentences(pronounce.apply(prepare_for_speech(text)))


def write_silero_sample(engine, name, text, directory):
    """Образец фразы Silero: по предложениям через воркер, склейка, WAV с правами 600. (путь, секунды звука, пик)."""
    np = effect.numpy_module()
    chunks, rate = [], None
    for sentence in silero_sentences(text):
        samples, rate = engine.read_wav(engine.wav_path(engine.request(sentence)))
        if engine.effect_on:
            samples, _ = effect.apply_effect(samples, rate, engine.pitch, engine.reverb)
        chunks += [np.asarray(samples, dtype=np.float64), np.zeros(int(rate * 0.12))]
    joined = np.concatenate(chunks) if chunks else np.zeros(1)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{name}.wav"
    wavs = TempWavs()
    temp = wavs.write(joined, rate or 48000)
    try:
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        wavs.cleanup()
    return path, len(joined) / float(rate or 48000), float(np.max(np.abs(joined)))


def check_silero(args, settings, out):
    """Проверка Silero (Этап 9.4). 0 — работает, 1 — нет."""
    from core.speech_text import prepare_for_speech
    settings.engine = "silero"
    if settings.voice not in SILERO_VOICES:               # в .env может стоять папка Piper или пусто
        settings.voice = SILERO_VOICES[0]
    engine = build_neural(settings, models_dir(), player=AfplayPlayer())
    model_size = f"{engine.model.stat().st_size / 1e6:.1f} МБ" if engine.model.is_file() else "НЕТ ФАЙЛА"
    out(f"Интерпретатор: {engine.python} — {'есть' if engine.python.is_file() else 'НЕТ'}")
    out(f"Воркер: {engine.worker_script} — {'есть' if engine.worker_script.is_file() else 'НЕТ'}")
    out(f"Модель: {engine.model} — {model_size}")
    out(f"numpy в проекте: {'есть' if effect.available() else 'НЕТ (эффект недоступен)'}; выгрузка воркера через "
        f"{engine.unload_minutes or 'никогда'} мин простоя")
    problem = engine.installation_problem()
    if problem:
        out(f"Не установлено: {problem.reason}")
        return 1
    try:
        started = time.monotonic()
        engine.load()
        if engine.state != "ready" or not engine.info:
            out(f"Не загружено: {engine.unavailable.reason if engine.unavailable else 'воркер не ответил (см. jarvis.log)'}")
            return 1
        info = engine.info
        out(f"Воркер готов: torch {info.get('torch', '?')}, numpy {info.get('numpy') or '—'}, scipy {info.get('scipy') or '—'}; "
            f"потоков torch {info.get('threads', '?')}; голоса: {', '.join(info.get('voices', []))}")
        out(f"Загрузка модели в воркере {info.get('load_seconds', '?')} с; от запуска до готовности {time.monotonic() - started:.1f} с")
        first = silero_sentences("Открыто приложение Telegram.")[0]
        begin = time.monotonic()
        response = engine.request(first)
        elapsed = time.monotonic() - begin
        seconds = float(response.get("seconds") or 0.0)
        engine.read_wav(engine.wav_path(response))                  # проверка пути и удаление файла
        out(f"Первая фраза: синтез {elapsed:.2f} с (с обменом с воркером), звук {seconds:.2f} с, RTF "
            f"{elapsed / seconds if seconds else 0.0:.2f}, пик {response.get('peak', '?')} (допустимо до 0.89)")
        if not (args.samples or args.play):
            return 0
        rtfs, peaks = [], []
        folder = samples_dir() / f"silero_{settings.voice}_final"
        for name, text in SAMPLE_PHRASES:
            if args.samples:
                begin = time.monotonic()
                path, seconds, peak = write_silero_sample(engine, name, text, folder)
                rtfs.append((time.monotonic() - begin) / seconds)
                peaks.append(peak)
                out(f"  {name:10} {seconds:5.1f} с звука, пик {peak:.3f} -> {path}")
            if args.play:
                result = engine.speak(prepare_for_speech(text))
                out(f"  {name:10} воспроизведено предложений: {result.done} из {len(result.sentences)}")
        if rtfs:
            out(f"RTF по фразам: медиана {statistics.median(rtfs):.2f}, максимум {max(rtfs):.2f}; наибольший пик {max(peaks):.3f}")
        return 0
    finally:
        engine.close()


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description="Проверка нейронного голоса Jarvis")
    parser.add_argument("--samples", action="store_true", help="создать WAV для 10 фраз Jarvis (ничего не играет)")
    parser.add_argument("--effect", action="store_true", help="эффект «Jarvis» независимо от .env")
    parser.add_argument("--engine", choices=("sherpa", "silero"), help="какой нейронный голос проверять (по умолчанию — из "
                        ".env, если там sherpa или silero, иначе sherpa)")
    parser.add_argument("--play", action="store_true", help="проиграть фразы через afplay")
    args = parser.parse_args(argv)

    settings = tts_settings()
    if args.effect:
        settings.effect = True
    out(f"Движок в .env: {settings.engine}; голос: {settings.voice or '(не выбран)'}; скорость {settings.speed}; потоков "
        f"{settings.threads}; эффект: {'jarvis' if settings.effect else 'off'} (тон {settings.pitch:g}, реверберация "
        f"{settings.reverb:g})")
    if args.engine == "silero" or (args.engine is None and settings.engine == "silero"):
        return check_silero(args, settings, out)
    out(f"Пакет sherpa-onnx: {'установлен' if importlib.util.find_spec('sherpa_onnx') else 'НЕ установлен'}; numpy: "
        f"{'есть' if effect.available() else 'НЕТ (эффект недоступен)'}")
    settings.engine = "sherpa"
    engine = build_neural(settings, models_dir(), player=AfplayPlayer())
    folder = engine.folder
    out(f"Папка моделей: {models_dir()}")
    if folder is not None:
        files = sorted(p.name for p in folder.glob("*")) if folder.is_dir() else []
        out(f"Папка голоса: {folder} — {', '.join(files) if files else 'НЕТ ФАЙЛОВ'}")
    started = time.monotonic()
    engine.load()
    if engine.state != "ready":
        out(f"Не загружено: {engine.unavailable.reason}")
        return 1
    out(f"Голос загружен и прогрет за {time.monotonic() - started:.1f} с (прогрев RTF "
        f"{engine.warmup_rtf:.2f})" if engine.warmup_rtf is not None else f"Голос загружен за {time.monotonic() - started:.1f} с")

    first = time.monotonic()
    samples, rate = engine._synthesize("Открыто приложение Telegram.")
    elapsed = time.monotonic() - first
    duration = len(samples) / float(rate)
    out(f"Первая фраза: синтез {elapsed:.2f} с, звук {duration:.2f} с, RTF {elapsed / duration:.2f}")
    if not (args.samples or args.play):
        return 0
    rtfs = []
    for name, text in SAMPLE_PHRASES:
        begin = time.monotonic()
        if args.samples:
            path, seconds = write_sample(engine, name, text, samples_dir())
            rtfs.append((time.monotonic() - begin) / seconds)
            out(f"  {name:10} {seconds:5.1f} с звука -> {path}")
        if args.play:
            result = engine.speak(text)
            out(f"  {name:10} воспроизведено предложений: {result.done} из {len(result.sentences)}")
    if rtfs:
        out(f"RTF по фразам: медиана {statistics.median(rtfs):.2f}, максимум {max(rtfs):.2f}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
