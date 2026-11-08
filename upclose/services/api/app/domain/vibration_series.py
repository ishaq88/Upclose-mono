"""Per-second vibration series from raw rig_telemetry data. Pure domain code: no framework, ORM or network imports.

The accel RMS uses the same definition as the Tiger `pod_samples_1s` rollup: the square root of
the mean of x^2 + y^2 + z^2 over the raw samples in each one-second bucket (gravity included,
exactly as stored). Deterministic for the same input: fixed rounding, no randomness.
"""
import math
from dataclasses import dataclass
from typing import Literal

from app.contracts.sources import RigTelemetryData

MAX_POINTS = 600
MIN_DURATION_S = 1.0


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    t_s: float
    n_accel: int
    accel_rms: float | None
    sound_mean: float | None
    sound_max: int | None


@dataclass(frozen=True, slots=True)
class VibrationSeries:
    state: Literal["assessed", "not_assessed"]
    reason: str | None
    sample_count: int
    duration_s: float
    points: tuple[SeriesPoint, ...]


def _bucketize(values: tuple[int, ...] | tuple[float, ...], rate_hz: float) -> dict[int, list[float]]:
    buckets: dict[int, list[float]] = {}
    for index, value in enumerate(values):
        buckets.setdefault(int(index / rate_hz), []).append(float(value))
    return buckets


def _rms(xs: list[float], ys: list[float], zs: list[float]) -> float:
    return math.sqrt(sum(x * x + y * y + z * z for x, y, z in zip(xs, ys, zs)) / len(xs))


def vibration_series(data: RigTelemetryData) -> VibrationSeries:
    """One point per whole second from recording start (t_s = 0, 1, 2, ...).

    Longer runs are downsampled to at most MAX_POINTS points by averaging whole buckets.
    Fewer than MIN_DURATION_S seconds of accel samples is never scored as a flat line:
    it returns not_assessed with a reason.
    """
    accel = data.accel
    count = len(accel.x_mg)
    duration = round(count / accel.rate_hz, 3)

    def not_assessed(reason: str) -> VibrationSeries:
        return VibrationSeries(state="not_assessed", reason=reason, sample_count=count,
                               duration_s=duration, points=())

    if duration < MIN_DURATION_S:
        return not_assessed(f"Fewer than {MIN_DURATION_S:g} second(s) of vibration samples.")
    xs = _bucketize(accel.x_mg, accel.rate_hz)
    ys = _bucketize(accel.y_mg, accel.rate_hz)
    zs = _bucketize(accel.z_mg, accel.rate_hz)
    sounds: dict[int, list[float]] = {}
    if data.sound is not None:
        sounds = _bucketize(data.sound.level, data.sound.rate_hz)
    points = []
    for bucket in sorted(xs):
        samples = sounds.get(bucket, [])
        points.append(SeriesPoint(
            t_s=float(bucket), n_accel=len(xs[bucket]),
            accel_rms=round(_rms(xs[bucket], ys[bucket], zs[bucket]), 3),
            sound_mean=round(sum(samples) / len(samples), 3) if samples else None,
            sound_max=int(max(samples)) if samples else None,
        ))
    stride = max(1, math.ceil(len(points) / MAX_POINTS))
    if stride > 1:
        merged = []
        for start in range(0, len(points), stride):
            group = points[start:start + stride]
            rms = [p.accel_rms for p in group if p.accel_rms is not None]
            means = [p.sound_mean for p in group if p.sound_mean is not None]
            maxes = [p.sound_max for p in group if p.sound_max is not None]
            merged.append(SeriesPoint(
                t_s=group[0].t_s, n_accel=sum(p.n_accel for p in group),
                accel_rms=round(sum(rms) / len(rms), 3) if rms else None,
                sound_mean=round(sum(means) / len(means), 3) if means else None,
                sound_max=max(maxes) if maxes else None,
            ))
        points = merged
    return VibrationSeries(state="assessed", reason=None, sample_count=count,
                           duration_s=duration, points=tuple(points))
