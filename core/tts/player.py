"""Нарезка текста на предложения, временные WAV и очередь воспроизведения.

Фоновый поток синтезирует предложения по порядку в ограниченную очередь (2 вперёд), вызывающий поток проигрывает их через
`afplay` (список аргументов, без shell). Первое предложение звучит сразу после своего синтеза, а не после всего ответа.
stop() завершает текущий afplay, очищает очередь и увеличивает поколение: синтез, закончившийся после остановки,
отбрасывается. WAV лежат во временной папке (права 0700), имена случайные (без текста и времени), права файлов 0600,
удаляются сразу после воспроизведения и при выходе. Текст озвучки в лог не пишется.
"""
import atexit
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import wave
from array import array
from dataclasses import dataclass

log = logging.getLogger(__name__)

LOOKAHEAD = 2                 # сколько синтезированных предложений ждёт своей очереди
SENTENCE_LIMIT = 200          # длиннее — дополнительно режем по запятым и «;»
MIN_FRAGMENT = 3              # короче (например, одинокий «?») приклеивается к соседнему предложению

# Точка после этих слов — сокращение, а не конец предложения
ABBREVIATIONS = {"т", "д", "е", "п", "к", "г", "гг", "в", "вв", "ул", "пр", "просп", "пер", "см", "напр", "им", "руб",
                 "коп", "тыс", "млн", "млрд", "др", "проф", "доц", "акад", "ст", "стр", "рис", "табл", "англ", "рус",
                 "обл", "оф", "кв", "тел", "etc", "mr", "mrs", "dr", "vs"}
_END_RE = re.compile(r"[.!?…]+")


def _is_sentence_end(text, match):
    """Знак препинания — конец предложения? «?» и «!» — всегда; точка — если это не сокращение, не инициал и дальше
    заглавная буква, цифра, кавычка или конец текста. Числа вида «3.5» сюда не попадают: после точки нет пробела."""
    end = match.end()
    if end < len(text) and not text[end].isspace():
        return False                           # «3.5», «example.com», «v1.2.3»
    sign = match.group()
    if "?" in sign or "!" in sign or "…" in sign:
        return True
    rest = text[end:].lstrip()
    if rest and not (rest[0].isupper() or rest[0].isdigit() or rest[0] in "«\"'(["):
        return False
    word = re.search(r"(\w+)$", text[:match.start()])
    if word:
        token = word.group(1).lower()
        if token in ABBREVIATIONS or (len(token) == 1 and token.isalpha() and text[:match.start()][-2:-1] in ("", " ", ".")):
            return False                       # «т. д.», «см.», инициал «А.»
    return True


def _split_long(sentence, limit):
    """Предложение длиннее limit режем по запятым и «;» (если после них пробел: числа «3,5» целы) на куски не длиннее limit;
    кусок без запятых, всё ещё длиннее limit, режем по пробелу."""
    if len(sentence) <= limit:
        return [sentence]
    parts = re.split(r"(?<=[,;])\s+", sentence)
    pieces, current = [], ""
    for part in parts:
        while len(part) > limit:                       # последний запас: по пробелу ближе к limit
            cut = part.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            head, part = part[:cut].strip(), part[cut:].strip()
            if current:
                pieces.append(current)
                current = ""
            pieces.append(head)
        if not part:
            continue
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = part
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


def split_sentences(text, limit=SENTENCE_LIMIT):
    """Предложения для синтеза: по строкам, затем по «. ? ! …» (без сокращений, инициалов и чисел), длинные — ещё по
    запятым и «;». Пустое и слишком короткое не возвращается."""
    sentences = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        start = 0
        for match in _END_RE.finditer(line):
            if _is_sentence_end(line, match):
                sentences.append(line[start:match.end()].strip())
                start = match.end()
        if line[start:].strip():
            sentences.append(line[start:].strip())
    result = []
    for sentence in sentences:
        for piece in _split_long(sentence, limit):
            if result and len(piece) < MIN_FRAGMENT:
                result[-1] = f"{result[-1]} {piece}"        # одинокий «?» или «!» к предыдущему
            elif piece:
                result.append(piece)
    return result


class TempWavs:
    """Временная папка с WAV: папка 0700, файлы 0600, имена случайные, удаление сразу и при выходе."""

    def __init__(self):
        self._dir = None
        self._lock = threading.Lock()

    @property
    def directory(self):
        with self._lock:
            if self._dir is None:
                self._dir = tempfile.mkdtemp(prefix="jarvis-tts-")      # права 0700
                atexit.register(self.cleanup)
            return self._dir

    def new_path(self):
        return os.path.join(self.directory, secrets.token_hex(8) + ".wav")

    def write(self, samples, rate):
        """Записывает 16-битный моно WAV (samples — числа в [-1, 1]); возвращает путь."""
        path = self.new_path()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle, wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(rate))
            wav.writeframes(_pcm16(samples))
        return path

    def remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def cleanup(self):
        with self._lock:
            directory, self._dir = self._dir, None
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


def _pcm16(samples):
    try:
        import numpy as np
        return (np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    except ImportError:
        data = array("h", (int(max(-1.0, min(1.0, float(s))) * 32767) for s in samples))
        if os.sys.byteorder == "big":
            data.byteswap()
        return data.tobytes()


class AfplayPlayer:
    """Проигрывает WAV через afplay (список аргументов, без shell); stop() прерывает текущий."""

    def __init__(self):
        self._proc = None

    def play(self, path):
        """True — доиграно до конца; False — ошибка (afplay не запустился или завершился с кодом ≠ 0, не из-за stop())."""
        self._stopped = False
        try:
            self._proc = subprocess.Popen(["afplay", path])
            code = self._proc.wait()
        except OSError as e:
            log.error("afplay не запустился: %s", e)
            return False
        finally:
            self._proc = None
        return code == 0 or self._stopped

    def stop(self):
        self._stopped = True
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()


@dataclass
class PlayResult:
    done: int = 0            # сколько предложений прозвучало целиком
    error: bool = False      # синтез или воспроизведение упали
    stopped: bool = False    # речь прервана stop()


class SentenceQueue:
    """Синтез в фоне + воспроизведение по порядку. synthesize(sentence) -> (samples, rate); оба вызываются не в одном потоке."""

    def __init__(self, synthesize, player, wavs=None, lookahead=LOOKAHEAD):
        self.synthesize, self.player = synthesize, player
        self.wavs = wavs or TempWavs()
        self.lookahead = lookahead
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def stop(self):
        with self._lock:
            self._generation += 1
        self.player.stop()

    def speak(self, sentences):
        """Проигрывает предложения по порядку; блокирует до конца, ошибки или stop()."""
        generation = self.generation
        ready = queue.Queue(maxsize=self.lookahead)

        def put(item):
            while self.generation == generation:
                try:
                    ready.put(item, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def produce():
            for index, sentence in enumerate(sentences):
                if self.generation != generation:
                    return
                try:
                    samples, rate = self.synthesize(sentence)
                    if self.generation != generation:
                        return                                    # запоздалый синтез после stop() отбрасывается
                    path = self.wavs.write(samples, rate)
                except Exception as e:
                    log.warning("Нейронный голос: синтез предложения %d не удался (%s)", index + 1, type(e).__name__)
                    put(("error", index, None))
                    return
                if not put(("ok", index, path)):
                    self.wavs.remove(path)
                    return
            put(("end", len(sentences), None))

        thread = threading.Thread(target=produce, name="tts-synth", daemon=True)
        thread.start()
        result = PlayResult()
        try:
            while True:
                try:
                    kind, index, path = ready.get(timeout=0.05)
                except queue.Empty:
                    if self.generation != generation:
                        result.stopped = True
                        break
                    continue
                if kind != "ok":
                    result.error = kind == "error"
                    break
                if self.generation != generation:
                    self.wavs.remove(path)
                    result.stopped = True
                    break
                try:
                    played = self.player.play(path)
                finally:
                    self.wavs.remove(path)
                if self.generation != generation:
                    result.stopped = True
                    break
                if not played:
                    log.warning("Нейронный голос: воспроизведение предложения %d не удалось", index + 1)
                    result.error = True
                    break
                result.done += 1
        finally:
            self._drain(ready)
            thread.join(timeout=2)
            self._drain(ready)
        if self.generation != generation:
            result.stopped = True
        return result

    def _drain(self, ready):
        while True:
            try:
                kind, _, path = ready.get_nowait()
            except queue.Empty:
                return
            if path:
                self.wavs.remove(path)
