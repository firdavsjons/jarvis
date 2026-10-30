"""Эффект «Jarvis» (TTS_EFFECT=jarvis): тон ниже и короткая реверберация. Чистые функции на numpy, без новых зависимостей.

Понижение тона — ресэмплингом: сигнал растягивается в 1/r раз (r = 2^(полутона/12), 0,7…1,0), тон падает в r раз. Чтобы
длительность сохранилась, модель синтезирует быстрее в 1/r раз (`compensated_speed`). Реверберация — свёртка с коротким
синтетическим импульсом (хвост 120 мс, затухающий шум с фиксированным зерном), доля смеси задаётся TTS_REVERB.
"""
import logging
import math

log = logging.getLogger(__name__)

TAIL_SECONDS = 0.12                      # хвост реверберации (80–150 мс)
RATIO_RANGE = (0.7, 1.0)
MODEL_SPEED_RANGE = (0.5, 2.5)           # скорость, которую просим у модели с учётом компенсации
PEAK_LIMIT = 0.95
NOISE_SEED = 12345


def numpy_module():
    """numpy или None (нет — эффект выключается с предупреждением)."""
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def available():
    return numpy_module() is not None


def pitch_ratio(semitones):
    """Во сколько раз понижается частота: 2^(полутона/12), в пределах 0,7…1,0."""
    return max(RATIO_RANGE[0], min(RATIO_RANGE[1], 2.0 ** (float(semitones) / 12.0)))


def compensated_speed(speed, semitones):
    """Скорость, которую просим у модели, чтобы после растяжения ресэмплингом длительность осталась как при `speed`."""
    wanted = speed / pitch_ratio(semitones)
    return max(MODEL_SPEED_RANGE[0], min(MODEL_SPEED_RANGE[1], wanted))


def lower_pitch(samples, semitones):
    """Понижение тона ресэмплингом: частоты × r, длина × 1/r."""
    np = numpy_module()
    x = np.asarray(samples, dtype=np.float64)
    ratio = pitch_ratio(semitones)
    if len(x) < 2 or ratio >= 1.0:
        return x
    positions = np.arange(int(len(x) / ratio)) * ratio
    return np.interp(positions, np.arange(len(x)), x)


def reverb_impulse(rate, tail=TAIL_SECONDS):
    """Синтетический импульс: затухающий шум на `tail` секунд, нормирован по энергии."""
    np = numpy_module()
    length = max(2, int(rate * tail))
    t = np.arange(length) / float(length)
    impulse = np.random.default_rng(NOISE_SEED).standard_normal(length) * np.exp(-6.9 * t)   # затухание ≈ 60 дБ
    return impulse / math.sqrt(float(np.sum(impulse ** 2)))


def add_reverb(samples, rate, mix, tail=TAIL_SECONDS):
    """(сигнал с реверберацией, длина добавленного хвоста в секундах). mix = 0 — сигнал без изменений и хвоста."""
    np = numpy_module()
    x = np.asarray(samples, dtype=np.float64)
    if mix <= 0 or len(x) == 0:
        return x, 0.0
    impulse = reverb_impulse(rate, tail)
    size = len(x) + len(impulse) - 1
    fft = 1 << (size - 1).bit_length()
    wet = np.fft.irfft(np.fft.rfft(x, fft) * np.fft.rfft(impulse, fft), fft)[:size]
    dry = np.zeros(size)
    dry[:len(x)] = x
    return (1.0 - mix) * dry + mix * wet, (len(impulse) - 1) / float(rate)


def normalize(processed, reference):
    """Громкость как у исходного сигнала (по RMS), без клиппинга: пик не выше PEAK_LIMIT."""
    np = numpy_module()
    out = np.asarray(processed, dtype=np.float64)
    if len(out) == 0:
        return out
    rms_in = float(np.sqrt(np.mean(np.square(np.asarray(reference, dtype=np.float64))))) if len(reference) else 0.0
    rms_out = float(np.sqrt(np.mean(np.square(out))))
    if rms_in > 0 and rms_out > 0:
        out = out * (rms_in / rms_out)
    peak = float(np.max(np.abs(out)))
    if peak > PEAK_LIMIT:
        out = out * (PEAK_LIMIT / peak)
    return out


def apply_effect(samples, rate, semitones, mix):
    """Полный эффект: (сигнал float64 в [-1, 1], хвост реверберации в секундах)."""
    np = numpy_module()
    original = np.asarray(samples, dtype=np.float64)
    shifted = lower_pitch(original, semitones)
    wet, tail = add_reverb(shifted, rate, mix)
    return normalize(wet, original), tail
