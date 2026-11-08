"""Synthetic engine audio: 16 kHz mono WAV from the same parameters as `simulate`.

One pulse per firing event (a low thump plus a short higher-pitched crack), over
exhaust-like noise. A misfire drops a pulse (strongly attenuated, as in the
vibration simulator). Deterministic for a given AudioConfig. SYNTHETIC.
"""

from __future__ import annotations

import json
import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16000
MISFIRE_AMP = 0.08
PULSE_TAU_S = 0.008
CRACK_HZ = 180.0
CRACK_TAU_S = 0.004
CRACK_GAIN = 0.4
PEAK = 0.8  # fraction of int16 full scale


@dataclass(frozen=True)
class AudioConfig:
    seconds: float = 60.0
    rpm: float = 780.0
    cylinders: int = 4
    misfire_every: int = 0  # 0 = none; N = every Nth firing event is dropped
    misfire_from_s: float = 0.0  # misfires apply from here ...
    misfire_until_s: float | None = None  # ... to here (None = the end)
    cold: bool = False
    seed: int = 1234

    @property
    def fire_hz(self) -> float:
        return self.rpm / 60.0 * self.cylinders / 2.0


def _check(cfg: AudioConfig) -> None:
    if cfg.seconds <= 0 or cfg.rpm <= 0 or not 1 <= cfg.cylinders <= 16:
        raise ValueError("seconds and rpm must be positive; cylinders 1..16")
    if cfg.misfire_every < 0:
        raise ValueError("misfire_every must be >= 0")


def pulse_times(cfg: AudioConfig) -> np.ndarray:
    """Nominal time (s) of every firing event."""
    return np.arange(int(cfg.seconds * cfg.fire_hz) + 1) / cfg.fire_hz


def misfired(cfg: AudioConfig) -> np.ndarray:
    """Boolean per firing event: True where the pulse is dropped."""
    k = np.arange(len(pulse_times(cfg)))
    t = pulse_times(cfg)
    if not cfg.misfire_every:
        return np.zeros(len(k), dtype=bool)
    until = math.inf if cfg.misfire_until_s is None else cfg.misfire_until_s
    return ((k + 1) % cfg.misfire_every == 0) & (t >= cfg.misfire_from_s) & (t < until)


def synthesize(cfg: AudioConfig) -> np.ndarray:
    """int16 samples at SAMPLE_RATE."""
    _check(cfg)
    rng = np.random.default_rng(cfg.seed)
    n = int(round(cfg.seconds * SAMPLE_RATE))
    t = np.arange(n) / SAMPLE_RATE
    sig = np.zeros(n)
    span = int(10 * PULSE_TAU_S * SAMPLE_RATE)
    drop = misfired(cfg)
    for k, tk0 in enumerate(pulse_times(cfg)):
        tk = tk0 + rng.normal(0.0, 0.0004)
        i0 = max(0, int(math.floor(tk * SAMPLE_RATE)))
        i1 = min(n, i0 + span)
        if i0 >= i1:
            continue
        dt = np.maximum(t[i0:i1] - tk, 0.0)
        x = dt / PULSE_TAU_S
        thump = (1.0 - x) * np.exp(1.0 - x) / math.e  # zero-mean (derivative of an alpha pulse): no DC offset
        crack = CRACK_GAIN * np.exp(-dt / CRACK_TAU_S) * np.sin(2 * math.pi * CRACK_HZ * dt)
        sig[i0:i1] += (MISFIRE_AMP if drop[k] else 1.0) * (thump + crack)
    sig -= sig.mean()
    sig /= np.abs(sig).max() or 1.0
    # exhaust-like noise: white noise through a one-pole low-pass (~800 Hz)
    white = rng.normal(0.0, 1.0, n)
    a = math.exp(-2 * math.pi * 800.0 / SAMPLE_RATE)
    noise = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc = a * acc + (1 - a) * white[i]
        noise[i] = acc
    noise /= noise.std() or 1.0
    sig = sig + noise * (0.10 if cfg.cold else 0.05)
    sig = sig / (np.abs(sig).max() or 1.0) * PEAK
    return np.rint(sig * 32767).astype("<i2")


def write_wav(path: str | Path, samples: np.ndarray) -> int:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return int(samples.shape[0])


def sidecar_for(cfg: AudioConfig) -> dict[str, Any]:
    return {
        "provenance": "synthetic",
        "generator": "upclose_rig.audio",
        "sample_rate_hz": SAMPLE_RATE,
        "channels": 1,
        "fire_hz": round(cfg.fire_hz, 4),
        "params": asdict(cfg),
    }


def write_synthetic(path: str | Path, cfg: AudioConfig) -> Path:
    """Write the WAV and its `<file>.json` sidecar; returns the sidecar path."""
    write_wav(path, synthesize(cfg))
    side = Path(str(path) + ".json")
    side.write_text(json.dumps(sidecar_for(cfg), indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return side
