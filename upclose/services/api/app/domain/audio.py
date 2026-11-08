"""Pure loudness summary of decoded engine audio (mono signed 16-bit PCM). No framework, ORM or I/O."""
import math
from array import array
from dataclasses import dataclass

FULL_SCALE = 32768.0
FLOOR_DB = -120.0


@dataclass(frozen=True, slots=True)
class Loudness:
    sample_count: int
    rms_db: float
    peak_db: float
    envelope_db: tuple[float, ...]


def _db(level: float) -> float:
    return FLOOR_DB if level <= 0.0 else max(FLOOR_DB, 20.0 * math.log10(level / FULL_SCALE))


def loudness(pcm: bytes, rate_hz: int, window_ms: int, max_points: int) -> Loudness:
    """RMS and peak in dBFS plus a per-window RMS envelope (rounded; deterministic for the same bytes)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if samples.itemsize != 2:  # pragma: no cover - platform guard
        raise ValueError("unexpected sample width")
    if not samples:
        return Loudness(0, FLOOR_DB, FLOOR_DB, ())
    size = max(1, rate_hz * window_ms // 1000)
    envelope: list[float] = []
    total = 0
    for start in range(0, len(samples) - size + 1, size):
        energy = sum(value * value for value in samples[start:start + size])
        total += energy
        envelope.append(round(_db(math.sqrt(energy / size)), 1))
    tail = len(samples) % size
    if tail and not envelope:
        energy = sum(value * value for value in samples)
        total, envelope = energy, [round(_db(math.sqrt(energy / len(samples))), 1)]
    covered = size * max(1, len(envelope)) if len(samples) >= size else len(samples)
    rms = math.sqrt(total / covered) if covered else 0.0
    peak = max(max(samples), -min(samples))
    return Loudness(len(samples), round(_db(rms), 1), round(_db(float(peak)), 1), tuple(envelope[:max_points]))
