"""Воркер Silero TTS v5 (Этап 9.4): отдельный процесс, единственный, где живёт torch.

Запуск (делает core/tts/silero.py, не вручную): `<python окружения Silero> -I silero_worker.py <модель.pt> <голос> <потоки>`.
Исполняется в ОТДЕЛЬНОМ окружении (torch, numpy, scipy) и НЕ импортирует ничего из проекта: только стандартную библиотеку,
а torch — внутри main(), чтобы тесты могли загрузить модуль без него.

Обмен — JSON-строки (по одной на сообщение):
  в stdout после загрузки: {"ready": true, "torch": "...", "numpy": "...", "scipy": "...", "threads": N,
                            "load_seconds": X, "voices": [...], "wav_dir": "/tmp/jarvis-silero-…"}
                      или: {"fatal": "<класс ошибки>", "missing": "<модуль>"} — и выход с кодом 1;
  в stdin  запрос: {"id": N, "text": "…", "voice": "eugene"};
  в stdout ответ:  {"id": N, "wav": "<путь>", "seconds": X, "peak": 0.89, "raw_peak": 1.04} или {"id": N, "error": "<класс>"}.
В stdout только JSON (print и предупреждения библиотек уходят в stderr). В stderr — служебные строки с меткой «[silero-worker]»
без текста запросов; тексты исключений не пишутся (в них может оказаться текст). WAV (48 кГц, 16 бит, моно) лежат в
папке 0700 с случайными именами, файлы 0600; папка удаляется при выходе. Закрытый stdin — штатное завершение.

Громкость: пик не выше 0,89, только уменьшение, без обрезки (`peak_gain`).
Лицензия модели Silero: CC BY-NC-SA 4.0 (личное некоммерческое использование).
"""
import atexit
import importlib.metadata
import json
import math
import os
import secrets
import shutil
import signal
import sys
import tempfile
import time
import wave

SAMPLE_RATE = 48000
PEAK_MAX = 0.89
VOICES = ("aidar", "eugene")
WARMUP_TEXT = "Привет."
LOG_TAG = "[silero-worker] "


def peak_gain(peak, limit=PEAK_MAX):
    """Множитель громкости: 1.0, если пик в пределах limit (или не число/ноль), иначе limit / peak. Только уменьшение."""
    if not peak > limit or math.isinf(peak):
        return 1.0
    return limit / peak


def valid_voice(name, voices=VOICES):
    return name in voices


def log(message):
    sys.stderr.write(LOG_TAG + message + "\n")
    sys.stderr.flush()


def send(out, message):
    out.write(json.dumps(message, ensure_ascii=False) + "\n")
    out.flush()


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def write_wav(path, pcm):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle, wave.open(handle, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


def main(argv):
    if len(argv) != 4:
        sys.stderr.write("использование: silero_worker.py <модель.pt> <голос> <потоков>\n")
        return 2
    model_path, voice, threads = argv[1], argv[2], max(1, int(argv[3]))
    out, sys.stdout = sys.stdout, sys.stderr                  # любой print библиотек — в stderr, не в протокол
    started = time.monotonic()
    wav_dir = tempfile.mkdtemp(prefix="jarvis-silero-")       # права 0700
    atexit.register(shutil.rmtree, wav_dir, True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))     # atexit удалит папку
    try:
        if not valid_voice(voice):
            raise ValueError("voice")
        import torch
        torch.set_num_threads(threads)
        model = torch.package.PackageImporter(model_path).load_pickle("tts_models", "model")
        model.to("cpu")
        voices = [name for name in model.speakers if valid_voice(name)]
        if voice not in voices:
            raise ValueError("voice")
        model.apply_tts(text=WARMUP_TEXT, speaker=voice, sample_rate=SAMPLE_RATE, put_accent=True, put_yo=True)
    except Exception as e:                                    # нет пакета, битая модель, нет памяти
        send(out, {"fatal": type(e).__name__, "missing": getattr(e, "name", "") if isinstance(e, ImportError) else ""})
        log(f"не загрузилось: {type(e).__name__}")
        return 1
    send(out, {"ready": True, "torch": torch.__version__, "numpy": package_version("numpy"),
               "scipy": package_version("scipy"), "threads": torch.get_num_threads(),
               "load_seconds": round(time.monotonic() - started, 2), "voices": voices, "wav_dir": wav_dir})
    log(f"готов за {time.monotonic() - started:.1f} с, потоков torch {torch.get_num_threads()}")

    for line in sys.stdin:
        try:
            request = json.loads(line)
            request_id, text = request["id"], str(request["text"])
            speaker = request.get("voice") or voice
        except (ValueError, KeyError, TypeError):
            log("непонятный запрос")
            continue
        try:
            if not valid_voice(speaker) or speaker not in voices:
                raise ValueError("voice")
            begun = time.monotonic()
            audio = model.apply_tts(text=text, speaker=speaker, sample_rate=SAMPLE_RATE, put_accent=True, put_yo=True)
            raw_peak = float(audio.abs().max()) if audio.numel() else 0.0
            audio = audio * peak_gain(raw_peak)
            pcm = (audio.clamp(-1.0, 1.0) * 32767.0).round().to(torch.int16).numpy().astype("<i2").tobytes()
            path = os.path.join(wav_dir, secrets.token_hex(8) + ".wav")
            write_wav(path, pcm)
            seconds = audio.numel() / float(SAMPLE_RATE)
            send(out, {"id": request_id, "wav": path, "seconds": round(seconds, 3),
                       "peak": round(float(audio.abs().max()) if audio.numel() else 0.0, 4), "raw_peak": round(raw_peak, 4)})
            log(f"синтез {seconds:.2f} с звука за {time.monotonic() - begun:.2f} с")
        except Exception as e:
            send(out, {"id": request_id, "error": type(e).__name__})
            log(f"ошибка синтеза: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
