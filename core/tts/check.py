"""Проверка нейронного голоса вручную: `python -m core.tts.check [--samples [--effect]] [--play]`.

Запускает пользователь (шаг 9.3), не тесты. Микрофон не включается. Без параметров: показывает настройки, наличие пакета
sherpa-onnx и файлов модели (ничего не скачивает), загружает и прогревает голос, замеряет первую фразу и RTF.
`--samples` создаёт WAV для 10 фраз Jarvis в ~/Library/Application Support/Jarvis/tts_samples/ (права 600) и ничего не
играет; `--effect` включает эффект «Jarvis» независимо от .env; `--play` проигрывает фразы через afplay.
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
from core.tts.base import TTSUnavailable, tts_settings
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


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description="Проверка нейронного голоса Jarvis")
    parser.add_argument("--samples", action="store_true", help="создать WAV для 10 фраз Jarvis (ничего не играет)")
    parser.add_argument("--effect", action="store_true", help="эффект «Jarvis» независимо от .env")
    parser.add_argument("--play", action="store_true", help="проиграть фразы через afplay")
    args = parser.parse_args(argv)

    settings = tts_settings()
    if args.effect:
        settings.effect = True
    out(f"Движок в .env: {settings.engine}; голос: {settings.voice or '(не выбран)'}; скорость {settings.speed}; потоков "
        f"{settings.threads}; эффект: {'jarvis' if settings.effect else 'off'} (тон {settings.pitch:g}, реверберация "
        f"{settings.reverb:g})")
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
