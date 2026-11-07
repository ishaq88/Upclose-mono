"""Window features from vibration (accelerometer) and audio. numpy only.

A window is 0.5 s with 50 % overlap. All features are computed relative to the
firing frequency ``f0`` estimated from the healthy baseline.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ..protocol import Record

WINDOW_S = 0.5
HOP_S = 0.25
AUDIO_SR = 16000
N_MEL = 24
EPS = 1e-9

VIB_FEATURES = (
    "vib_log_rms", "vib_crest", "vib_kurtosis",
    "vib_log_frac_fire", "vib_log_frac_half", "vib_log_frac_sub", "vib_log_frac_2x", "vib_log_frac_hi",
    "vib_sub_ratio", "vib_cycle_cv",
)
# Smallest change worth calling a deviation, per vibration feature. Without
# a floor a very clean baseline gets a near-zero MAD and any gain drift between
# recordings looks like a fault.
VIB_MIN_SCALE = (0.50, 0.10, 0.20, 0.10, 0.20, 0.20, 0.10, 0.20, 0.20, 0.03)
AUDIO_FEATURES = tuple(f"aud_mel_{i:02d}" for i in range(N_MEL)) + ("aud_flux", "aud_env_cycle_cv")

FLAT_STD_MG = 1.5
CLIP_FRACTION = 0.02
FULL_SCALE_MG = 4000.0   # the pod runs the LIS3DH at +/-4 g
NEAR_FULL_SCALE = 0.9     # an extreme must be within 10 % of full scale to count as saturation
RANGE_LIMIT_MG = 3990.0


class AnomalyError(ValueError):
    """Input or model cannot be used (the caller reports it, never a clean score)."""


def accel_from_records(records: Sequence[Record]) -> tuple[np.ndarray, float]:
    """(n, 3) float accel in mg and the sample rate from timestamps."""
    acc = [r for r in records if r.kind == "A"]
    if len(acc) < 2:
        raise AnomalyError("no_accel: fewer than 2 accelerometer records")
    t = np.fromiter((r.t_ms for r in acc), dtype=float, count=len(acc))
    xyz = np.array([r.values for r in acc], dtype=float)
    span = (t[-1] - t[0]) / 1000.0
    if span <= 0 or not np.all(np.isfinite(xyz)):
        raise AnomalyError("no_accel: bad timestamps or non-finite values")
    return xyz, (len(acc) - 1) / span


def quality_problem(xyz: np.ndarray, full_scale_mg: float = FULL_SCALE_MG) -> str | None:
    """Reason a signal must not be assessed, or None.

    Clipping means saturation near the sensor's full scale. A quiet signal that
    is coarsely quantized (the pod delivers 8 mg steps) repeats its own min and
    max often in a short window; that is not clipping.
    """
    if not np.all(np.isfinite(xyz)):
        return "non_finite_signal"
    ac = xyz - xyz.mean(axis=0)
    if float(ac.std(axis=0).max()) < FLAT_STD_MG or float(np.mean(np.diff(xyz, axis=0) == 0)) > 0.95:
        return "flat_signal"
    near_full_scale = NEAR_FULL_SCALE * full_scale_mg
    for ax in range(xyz.shape[1]):
        col = xyz[:, ax]
        for extreme in (col.max(), col.min()):
            if abs(extreme) >= near_full_scale and np.mean(col == extreme) >= CLIP_FRACTION:
                return "clipped_signal"
    if float(np.mean(np.abs(xyz) >= RANGE_LIMIT_MG)) > 0.01:
        return "clipped_signal"
    return None


def principal_direction(xyz: np.ndarray) -> np.ndarray:
    ac = xyz - xyz.mean(axis=0)
    vals, vecs = np.linalg.eigh(np.cov(ac.T))
    d = vecs[:, int(np.argmax(vals))]
    if d[int(np.argmax(np.abs(d)))] < 0:
        d = -d
    return d


def estimate_fire_hz(sig: np.ndarray, fs: float) -> float:
    """Dominant spectral peak between 8 Hz and fs/2.2, parabolic-refined."""
    x = (sig - sig.mean()) * np.hanning(len(sig))
    nfft = 1 << int(np.ceil(np.log2(len(x))))
    p = np.abs(np.fft.rfft(x, nfft)) ** 2
    f = np.fft.rfftfreq(nfft, 1 / fs)
    m = np.flatnonzero((f >= 8.0) & (f <= fs / 2.2))
    if len(m) < 3:
        raise AnomalyError("sample rate too low to find a firing frequency")
    k = int(m[np.argmax(p[m])])
    if 0 < k < len(p) - 1:
        a, b, c = np.log(p[k - 1] + EPS), np.log(p[k] + EPS), np.log(p[k + 1] + EPS)
        den = a - 2 * b + c
        k_ref = k + (0.5 * (a - c) / den if den != 0 else 0.0)
    else:
        k_ref = float(k)
    return float(k_ref * fs / nfft)


def window_shape(fs: float) -> tuple[int, int]:
    return max(8, int(round(WINDOW_S * fs))), max(1, int(round(HOP_S * fs)))


def n_windows(n_samples: int, fs: float) -> int:
    w, h = window_shape(fs)
    return 0 if n_samples < w else (n_samples - w) // h + 1


def cycle_cv(x: np.ndarray, fs: float, f0: float) -> np.ndarray:
    """Firing-to-firing amplitude spread per window: std/mean of the
    peak-to-peak of consecutive one-period segments. x is (nw, n)."""
    period = fs / f0
    n = x.shape[1]
    if period < 2.0 or period > n / 2:
        return np.zeros(x.shape[0])
    edges = np.floor(np.arange(0.0, n + 1e-9, period)).astype(int)
    edges = edges[edges <= n]
    seg = x[:, : edges[-1]]
    starts = edges[:-1]
    ptp = np.maximum.reduceat(seg, starts, axis=1) - np.minimum.reduceat(seg, starts, axis=1)
    return ptp.std(axis=1) / (ptp.mean(axis=1) + EPS)


def vib_features(sig: np.ndarray, fs: float, f0: float) -> np.ndarray:
    """(nw, len(VIB_FEATURES)) from a projected 1-D signal."""
    w, h = window_shape(fs)
    if len(sig) < w:
        return np.zeros((0, len(VIB_FEATURES)))
    x = sliding_window_view(sig, w)[::h].astype(float)
    x = x - x.mean(axis=1, keepdims=True)
    rms = np.sqrt((x ** 2).mean(axis=1)) + EPS
    crest = np.abs(x).max(axis=1) / rms
    kurt = (x ** 4).mean(axis=1) / rms ** 4 - 3.0
    nfft = 1024
    p = np.abs(np.fft.rfft(x * np.hanning(w), nfft, axis=1)) ** 2
    f = np.fft.rfftfreq(nfft, 1 / fs)

    nyq = 0.98 * fs / 2

    def band(lo: float, hi: float) -> np.ndarray:
        hi = min(hi, nyq)
        if hi - lo < 3 * fs / nfft:  # band lost to Nyquist: a constant, not a per-recording artifact
            return np.ones(len(x))
        m = (f >= lo) & (f <= hi)
        return p[:, m].sum(axis=1)

    total = p.sum(axis=1) + EPS  # band energies are fractions of the total: invariant to sensor gain
    hw = max(1.5, 0.12 * f0)
    e_fire = band(f0 - hw, f0 + hw)
    e_half = band(f0 / 2 - hw, f0 / 2 + hw)
    e_sub = band(0.1 * f0, 0.85 * f0)
    e_2x = band(2 * f0 - hw, 2 * f0 + hw)
    e_hi = band(2.5 * f0, fs / 2)
    lg = lambda v: np.log(v + EPS)  # noqa: E731
    cols = [lg(rms), crest, kurt, lg(e_fire / total), lg(e_half / total), lg(e_sub / total), lg(e_2x / total),
            lg(e_hi / total), lg(e_sub) - lg(e_fire), cycle_cv(x, fs, f0)]
    return np.stack(cols, axis=1)


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Mono float samples in [-1, 1] and the sample rate (PCM 8/16/24/32)."""
    try:
        with wave.open(str(path), "rb") as wf:
            nch, width, sr, nfr = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
            raw = wf.readframes(nfr)
    except (wave.Error, EOFError) as exc:
        raise AnomalyError(f"{path}: not a PCM WAV ({exc})") from exc
    if width == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(float) - 128.0) / 128.0
    elif width == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(float) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        v = b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        a = v.astype(float) / (1 << 23)
    elif width == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(float) / 2147483648.0
    else:
        raise AnomalyError(f"{path}: unsupported sample width {width}")
    if nch > 1:
        a = a[: len(a) // nch * nch].reshape(-1, nch).mean(axis=1)
    return a, sr


def resample_16k(a: np.ndarray, sr: int) -> np.ndarray:
    """Band-limited FFT resampling to 16 kHz."""
    if sr == AUDIO_SR or len(a) == 0:
        return a
    n_new = int(round(len(a) * AUDIO_SR / sr))
    spec = np.fft.rfft(a)
    spec2 = np.zeros(n_new // 2 + 1, dtype=complex)
    k = min(len(spec), len(spec2))
    spec2[:k] = spec[:k]
    return np.fft.irfft(spec2, n_new) * (n_new / len(a))


def _mel_edges() -> np.ndarray:
    mel = lambda hz: 2595.0 * np.log10(1.0 + hz / 700.0)  # noqa: E731
    inv = lambda m: 700.0 * (10 ** (m / 2595.0) - 1.0)  # noqa: E731
    return inv(np.linspace(mel(50.0), mel(7800.0), N_MEL + 1))


def audio_features(a16: np.ndarray, n_win: int, f0: float) -> np.ndarray:
    """(n_win, N_MEL + 2) aligned to the vibration windows (audio t=0 is
    recording t=0). Raises AnomalyError if the audio is too short."""
    w, h = int(WINDOW_S * AUDIO_SR), int(HOP_S * AUDIO_SR)
    if n_win <= 0 or len(a16) < w + (n_win - 1) * h:
        raise AnomalyError("audio_too_short: audio does not cover the vibration windows")
    edges = _mel_edges()
    fr = np.fft.rfftfreq(512, 1 / AUDIO_SR)
    masks = [(fr >= edges[i]) & (fr < edges[i + 1]) for i in range(N_MEL)]
    win = np.hanning(512)
    out = np.zeros((n_win, N_MEL + 2))
    for k in range(n_win):
        seg = a16[k * h: k * h + w]
        frames = sliding_window_view(seg - seg.mean(), 512)[::256]
        pw = np.abs(np.fft.rfft(frames * win, axis=1)) ** 2
        bands = np.stack([pw[:, m].sum(axis=1) if m.any() else np.zeros(len(pw)) for m in masks], axis=1)
        lb = np.log(bands + 1e-12)
        out[k, :N_MEL] = np.log(bands.mean(axis=0) + 1e-12)
        out[k, N_MEL] = float(np.maximum(0.0, np.diff(lb, axis=0)).mean()) if len(lb) > 1 else 0.0
        env = np.abs(seg)
        env = env[: len(env) // 16 * 16].reshape(-1, 16).mean(axis=1)  # 1 kHz envelope
        out[k, N_MEL + 1] = cycle_cv(env[None, :], 1000.0, f0)[0]
    return out
