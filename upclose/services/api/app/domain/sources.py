"""Pure derived summaries for plugin evidence sources. No framework, ORM or network imports.

Summaries are deterministic for (data, provenance, config): floats are rounded to fixed
precision, there is no randomness, and the spectral search is a plain-Python Goertzel scan over
a decimated segment so 240k samples stay fast without numpy. The values are uncalibrated
estimates and never feed the condition score.
"""
import math
from pathlib import Path
from statistics import median
from typing import Annotated

from pydantic import Field

from app.contracts.base import ContractModel
from app.contracts.sources import (IrregularWindow, ObdSnapshotData, ObdSummary, RigTelemetryData,
                                   RigTelemetrySummary, SourceProvenance, TemperatureSummary)
from app.domain.config import HashedConfig

Pos = Annotated[float, Field(gt=0)]


class RigConfig(ContractModel):
    analysis_rate_hz: Pos
    min_duration_s: Pos
    min_analysis_samples: Annotated[int, Field(strict=True, ge=16)]
    max_analysis_samples: Annotated[int, Field(strict=True, ge=16)]
    band_low_hz: Pos
    band_high_hz: Pos
    coarse_step_hz: Pos
    fine_step_hz: Pos
    peak_to_median_min: Pos
    min_signal_mg: Pos
    window_ms: Annotated[int, Field(strict=True, gt=0)]
    irregular_deviation_ratio: Pos
    max_irregular_windows: Annotated[int, Field(strict=True, ge=0, le=20)]
    rough_cv_min: Pos
    warm_start_celsius: float


class ObdConfig(ContractModel):
    cleared_distance_km_below: Annotated[int, Field(strict=True, ge=0)]
    cleared_warmups_below: Annotated[int, Field(strict=True, ge=0)]
    incomplete_monitors_at_or_above: Annotated[int, Field(strict=True, ge=1)]
    cold_start_coolant_c_below: float


class SourcesConfig(HashedConfig):
    rig: RigConfig
    obd: ObdConfig


def load_sources_config(path: str | Path) -> SourcesConfig:
    return SourcesConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---- rig telemetry ------------------------------------------------------------------------------


def _decimate(values: tuple[int, ...], factor: int) -> list[float]:
    if factor == 1:
        return [float(v) for v in values]
    count = len(values) // factor
    return [sum(values[i * factor:(i + 1) * factor]) / factor for i in range(count)]


def _goertzel_power(segment: list[float], freq_hz: float, rate_hz: float) -> float:
    coeff = 2.0 * math.cos(2.0 * math.pi * freq_hz / rate_hz)
    s1 = s2 = 0.0
    for value in segment:
        s1, s2 = value + coeff * s1 - s2, s1
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def _scan(segment: list[float], rate_hz: float, low: float, high: float, step: float) -> list[tuple[float, float]]:
    count = int(round((high - low) / step))
    return [(low + k * step, _goertzel_power(segment, low + k * step, rate_hz)) for k in range(count + 1)]


def _dominant_frequency(deviations: list[float], rate_hz: float, config: RigConfig) -> float | None:
    """Frequency of the strongest line in the band, or None when no line stands clear of the band median."""
    n = min(len(deviations), config.max_analysis_samples)
    start = (len(deviations) - n) // 2
    segment = deviations[start:start + n]
    mean = sum(segment) / n
    segment = [(v - mean) * (0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1))) for i, v in enumerate(segment)]
    high = min(config.band_high_hz, 0.45 * rate_hz)
    if high <= config.band_low_hz:
        return None
    coarse = _scan(segment, rate_hz, config.band_low_hz, high, config.coarse_step_hz)
    peak_freq, peak_power = max(coarse, key=lambda item: item[1])
    floor = median(power for _, power in coarse)
    if peak_power <= 0.0 or peak_power < config.peak_to_median_min * floor:
        return None
    span = 1.5 * config.coarse_step_hz
    fine = _scan(segment, rate_hz, max(config.band_low_hz, peak_freq - span), min(high, peak_freq + span),
                 config.fine_step_hz)
    return max(fine, key=lambda item: item[1])[0]


def _temperature(data: RigTelemetryData, config: RigConfig) -> TemperatureSummary | None:
    readings = data.temperature
    if not readings:
        return None
    first, last = readings[0].celsius, readings[-1].celsius
    return TemperatureSummary(start_c=round(first, 1),
                              rise_c=round(last - first, 1) if len(readings) > 1 else None,
                              engine_warm_at_start=first >= config.warm_start_celsius)


def summarize_rig(data: RigTelemetryData, provenance: SourceProvenance, config: SourcesConfig) -> RigTelemetrySummary:
    cfg = config.rig
    accel = data.accel
    count = len(accel.x_mg)
    duration = round(count / accel.rate_hz, 3)

    def result(**fields: object) -> RigTelemetrySummary:
        return RigTelemetrySummary(config_hash=config.content_hash, provenance=provenance, sample_count=count,
                                   duration_s=duration, cylinders=data.cylinders,
                                   temperature=_temperature(data, cfg), **fields)  # type: ignore[arg-type]

    def not_assessed(reason: str) -> RigTelemetrySummary:
        return result(state="not_assessed", reason=reason)

    if duration < cfg.min_duration_s:
        return not_assessed(f"Fewer than {cfg.min_duration_s:g} seconds of vibration samples.")
    factor = max(1, math.ceil(accel.rate_hz / cfg.analysis_rate_hz))
    rate = accel.rate_hz / factor
    axes = [_decimate(values, factor) for values in (accel.x_mg, accel.y_mg, accel.z_mg)]
    length = len(axes[0])
    if length < cfg.min_analysis_samples:
        return not_assessed("Too few vibration samples to analyse.")
    deviations = []
    for series in axes:
        mean = sum(series) / length
        deviations.append([v - mean for v in series])
    energies = [sum(v * v for v in series) / length for series in deviations]
    if math.sqrt(sum(energies)) < cfg.min_signal_mg:
        return not_assessed("No measurable vibration signal.")
    strongest = deviations[energies.index(max(energies))]
    frequency = _dominant_frequency(strongest, rate, cfg)
    if frequency is None:
        return not_assessed("No clear dominant frequency between "
                            f"{cfg.band_low_hz:g} and {cfg.band_high_hz:g} Hz.")

    squares = [a * a + b * b + c * c for a, b, c in zip(*deviations)]
    size = max(1, round(cfg.window_ms / 1000.0 * rate))
    rms = [math.sqrt(sum(squares[i:i + size]) / size) for i in range(0, length - size + 1, size)]
    typical = median(rms)
    roughness: float | None = None
    windows: list[IrregularWindow] = []
    if typical > 0.0:
        mean_rms = sum(rms) / len(rms)
        roughness = math.sqrt(sum((v - mean_rms) ** 2 for v in rms) / len(rms)) / mean_rms
        flagged = [(abs(v - typical), index) for index, v in enumerate(rms)
                   if abs(v - typical) > cfg.irregular_deviation_ratio * typical]
        flagged.sort(key=lambda item: (-item[0], item[1]))
        for _, index in sorted(flagged[:cfg.max_irregular_windows], key=lambda item: item[1]):
            windows.append(IrregularWindow(t_ms=round(index * size * factor / accel.rate_hz * 1000),
                                           rms_mg=round(rms[index], 3)))
    flags: list[str] = []
    if windows:
        flags.append("irregular_vibration")
    if roughness is not None and roughness >= cfg.rough_cv_min:
        flags.append("rough_vibration")
    return result(state="assessed", dominant_hz=round(frequency, 2),
                  rpm_estimate=round(frequency * 120.0 / data.cylinders),
                  roughness=None if roughness is None else round(roughness, 4),
                  irregular_windows=tuple(windows), flags=tuple(flags), flag_count=len(flags))


# ---- OBD snapshot -------------------------------------------------------------------------------


def summarize_obd(data: ObdSnapshotData, provenance: SourceProvenance, config: SourcesConfig) -> ObdSummary:
    cfg = config.obd
    incomplete = tuple(sorted(name for name, state in data.readiness.items() if state == "incomplete"))
    distance, warmups = data.distance_since_codes_cleared_km, data.warmups_since_codes_cleared
    cleared: bool | None = None
    if distance is not None or warmups is not None or data.readiness:
        cleared = ((distance is not None and distance < cfg.cleared_distance_km_below)
                   or (warmups is not None and warmups < cfg.cleared_warmups_below)
                   or len(incomplete) >= cfg.incomplete_monitors_at_or_above)
    coolant = data.pids.get("coolant_c")
    flags = []
    if data.mil_on:
        flags.append("mil_on")
    if data.dtcs:
        flags.append("trouble_codes")
    if cleared:
        flags.append("codes_recently_cleared")
    return ObdSummary(config_hash=config.content_hash, provenance=provenance, vin=data.vin, mil_on=data.mil_on,
                      dtcs=data.dtcs, incomplete_monitors=incomplete, codes_recently_cleared=cleared,
                      cold_start=None if coolant is None else coolant < cfg.cold_start_coolant_c_below,
                      coolant_c=None if coolant is None else round(coolant, 1),
                      distance_since_codes_cleared_km=distance, warmups_since_codes_cleared=warmups,
                      flags=tuple(flags), flag_count=len(flags))
