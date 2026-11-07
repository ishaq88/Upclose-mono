"""Synthetic engine-cover vibration, emitting the same records as the pod."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from .protocol import Record

ACCEL_HZ = 400
SOUND_HZ = 100
TEMP_PERIOD_MS = 2000
EVENT_RE = re.compile(r"^([BK])@(\d+(?:\.\d+)?)=(\d+)$")


@dataclass(frozen=True)
class SimConfig:
    seconds: float = 60.0
    rpm: float = 780.0
    cylinders: int = 4
    misfire_every: int = 0  # 0 = none; N = every Nth firing event is dropped
    cold: bool = False
    seed: int = 1234
    events: tuple[str, ...] = field(default_factory=tuple)  # "B@10=1", "K@5=512"

    @property
    def fire_hz(self) -> float:
        """Firing frequency of a 4-stroke engine: rpm/60 * cylinders/2."""
        return self.rpm / 60.0 * self.cylinders / 2.0


def parse_event(spec: str) -> Record:
    m = EVENT_RE.match(spec)
    if not m:
        raise ValueError(f"bad event {spec!r}; use B@<sec>=<0|1> or K@<sec>=<0-1023>")
    kind, sec, val = m.group(1), float(m.group(2)), int(m.group(3))
    if (kind == "B" and val not in (0, 1)) or (kind == "K" and not 0 <= val <= 1023):
        raise ValueError(f"event value out of range: {spec!r}")
    return Record(kind, int(round(sec * 1000)), (val,))


def _vibration(cfg: SimConfig, rng: np.random.Generator, n: int) -> np.ndarray:
    """Sum of alpha-function pulses, one per firing event. Misfires are
    dropped (strongly attenuated) events, giving amplitude dips and a
    sub-harmonic at fire_hz / N."""
    t = np.arange(n) / ACCEL_HZ
    sig = np.zeros(n)
    period = 1.0 / cfg.fire_hz
    tau = 0.008
    n_events = int(cfg.seconds * cfg.fire_hz) + 2
    for k in range(n_events):
        amp = 1.0
        if cfg.misfire_every and (k + 1) % cfg.misfire_every == 0:
            amp = 0.08
        tk = k * period + rng.normal(0.0, 0.0004)
        i0 = max(0, int(math.floor(tk * ACCEL_HZ)))
        i1 = min(n, i0 + int(10 * tau * ACCEL_HZ) + 1)
        if i0 >= i1:
            continue
        dt = t[i0:i1] - tk
        dt = np.where(dt >= 0, dt, 0.0)
        sig[i0:i1] += amp * (dt / tau) * np.exp(1.0 - dt / tau)
    return sig - sig.mean()


def simulate(cfg: SimConfig) -> list[Record]:
    """Deterministic for a given SimConfig (seeded numpy Generator)."""
    if cfg.seconds <= 0 or cfg.rpm <= 0 or not 1 <= cfg.cylinders <= 16:
        raise ValueError("seconds and rpm must be positive; cylinders 1..16")
    rng = np.random.default_rng(cfg.seed)
    n = int(cfg.seconds * ACCEL_HZ)
    v = _vibration(cfg, rng, n)
    v = v / (np.abs(v).max() or 1.0)
    gains = {"x": 70.0, "y": 45.0, "z": 110.0}
    base = {"x": 18.0, "y": -12.0, "z": 984.0}
    axes = {}
    for ax in "xyz":
        noise = rng.normal(0.0, 5.0, n)
        axes[ax] = np.rint(base[ax] + gains[ax] * v + noise).astype(int)
    recs: list[Record] = []
    for i in range(n):
        recs.append(Record("A", int(round(i * 1000 / ACCEL_HZ)), (int(axes["x"][i]), int(axes["y"][i]), int(axes["z"][i]))))
    # sound: ~100 Hz level correlated with vibration magnitude
    step = ACCEL_HZ // SOUND_HZ
    m = n // step
    mag = np.abs(v[: m * step]).reshape(m, step).mean(axis=1)
    level = np.clip(np.rint(280 + 520 * mag + rng.normal(0.0, 6.0, m)), 0, 1023).astype(int)
    for j in range(m):
        recs.append(Record("S", int(round(j * 1000 / SOUND_HZ)), (int(level[j]),)))
    # temperature every 2 s
    for t_ms in range(0, int(cfg.seconds * 1000), TEMP_PERIOD_MS):
        if cfg.cold:
            c = 21.0 + (88.0 - 21.0) * (1.0 - math.exp(-t_ms / 1000.0 / 240.0))
        else:
            c = 87.5
        recs.append(Record("T", t_ms, (round(c + rng.normal(0.0, 0.08), 2), round(float(rng.normal(34.0, 0.2)), 1))))
    recs.extend(parse_event(e) for e in cfg.events)
    order = {"A": 0, "S": 1, "T": 2, "B": 3, "K": 4}
    recs.sort(key=lambda r: (r.t_ms, order[r.kind]))
    return recs
