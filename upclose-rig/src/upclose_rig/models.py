"""Pydantic mirror of the UB Hacking evidence-source wire contract
(.docs/ubhacking-evidence-sources.md in the UpClose repo). Strict, extra forbidden."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Provenance = Literal["synthetic", "cached", "live"]

MAX_ACCEL_SAMPLES = 240_000
MAX_SOUND_SAMPLES = 240_000
MAX_TEMPERATURE = 600
ACCEL_LIMIT_MG = 16_000

_Strict = ConfigDict(extra="forbid", allow_inf_nan=False, protected_namespaces=())

AccelInt = Annotated[int, Field(ge=-ACCEL_LIMIT_MG, le=ACCEL_LIMIT_MG, strict=True)]
SoundInt = Annotated[int, Field(ge=0, le=1023, strict=True)]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]


class Accel(BaseModel):
    model_config = _Strict
    rate_hz: float = Field(ge=50, le=1000)
    x_mg: list[AccelInt] = Field(min_length=1, max_length=MAX_ACCEL_SAMPLES)
    y_mg: list[AccelInt] = Field(min_length=1, max_length=MAX_ACCEL_SAMPLES)
    z_mg: list[AccelInt] = Field(min_length=1, max_length=MAX_ACCEL_SAMPLES)

    @model_validator(mode="after")
    def _equal_lengths(self) -> "Accel":
        if not (len(self.x_mg) == len(self.y_mg) == len(self.z_mg)):
            raise ValueError("accel axis arrays must have equal length")
        return self


class Sound(BaseModel):
    model_config = _Strict
    rate_hz: float = Field(ge=1, le=1000)
    level: list[SoundInt] = Field(min_length=1, max_length=MAX_SOUND_SAMPLES)


class TemperatureReading(BaseModel):
    model_config = _Strict
    t_ms: int = Field(ge=0, strict=True)
    celsius: float = Field(ge=-40, le=150)


FiniteFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
FeatureName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]
VERDICT_SCHEMA = "upclose-anomaly-verdict/1"


class VerdictFeature(BaseModel):
    model_config = _Strict
    name: FeatureName
    share: float = Field(ge=0, le=1)
    z: float | None = None


class VerdictRun(BaseModel):
    model_config = _Strict
    t_start_s: FiniteFloat
    t_end_s: FiniteFloat
    peak_score: FiniteFloat
    top_features: list[VerdictFeature] = Field(max_length=5)

    @model_validator(mode="after")
    def _ordered(self) -> "VerdictRun":
        if self.t_end_s < self.t_start_s:
            raise ValueError("run ends before it starts")
        return self


class VerdictModelInfo(BaseModel):
    model_config = _Strict
    fire_hz: FiniteFloat
    fs_hz: FiniteFloat
    has_audio: bool
    baseline_seconds: FiniteFloat
    baseline_source: str = Field(max_length=32)
    threshold_percentile: float = Field(gt=0, le=100)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelVerdict(BaseModel):
    """Mirror of `model_verdict` (README: Engine anomaly model). Includes the
    internal-consistency checks the server applies."""
    model_config = _Strict
    schema_: Literal["upclose-anomaly-verdict/1"] = Field(alias="schema")
    verdict: Literal["anomaly_suspected", "no_anomaly_detected", "not_assessed"]
    reason: str | None = Field(default=None, max_length=64)
    data_source: str = Field(max_length=32)
    duration_s: FiniteFloat
    windows: int | None = Field(ge=0, strict=True)
    anomalous_windows: int | None = Field(ge=0, strict=True)
    anomalous_fraction: float | None = Field(ge=0, le=1)
    max_score: FiniteFloat | None
    median_score: FiniteFloat | None
    threshold: FiniteFloat
    fraction_threshold: float = Field(gt=0, le=1)
    min_anomalous_windows: int = Field(ge=1, le=1000, strict=True)
    top_features: list[VerdictFeature] = Field(max_length=5)
    anomalous_runs: list[VerdictRun] = Field(max_length=10)
    model: VerdictModelInfo
    note: str = Field(max_length=300)

    @model_validator(mode="after")
    def _consistent(self) -> "ModelVerdict":
        counts = (self.windows, self.anomalous_windows, self.anomalous_fraction)
        if self.verdict == "not_assessed":
            if not self.reason or any(c is not None for c in counts) or self.anomalous_runs or self.top_features:
                raise ValueError("not_assessed needs a reason and null counts, no runs or features")
            return self
        if self.reason is not None or any(c is None for c in counts):
            raise ValueError("assessed verdicts carry counts and no reason")
        assert self.windows is not None and self.anomalous_windows is not None and self.anomalous_fraction is not None
        if self.windows < 1 or self.anomalous_windows > self.windows:
            raise ValueError("bad window counts")
        frac = self.anomalous_windows / self.windows
        if abs(frac - self.anomalous_fraction) > 1e-3:
            raise ValueError("anomalous_fraction != anomalous_windows / windows")
        suspected = self.anomalous_windows >= self.min_anomalous_windows and frac >= self.fraction_threshold - 1e-3
        if suspected != (self.verdict == "anomaly_suspected"):
            raise ValueError("verdict inconsistent with window counts and thresholds")
        for run in self.anomalous_runs:
            if run.t_end_s > self.duration_s + 0.05:
                raise ValueError("run outside the recording")
        return self


class RigTelemetryData(BaseModel):
    model_config = _Strict
    accel: Accel
    sound: Sound | None = None
    temperature: list[TemperatureReading] | None = Field(default=None, max_length=MAX_TEMPERATURE)
    cylinders: int | None = Field(default=None, ge=1, le=16, strict=True)
    model_verdict: ModelVerdict | None = None

    @field_validator("temperature")
    @classmethod
    def _non_decreasing(cls, v: list[TemperatureReading] | None) -> list[TemperatureReading] | None:
        if v and any(b.t_ms < a.t_ms for a, b in zip(v, v[1:])):
            raise ValueError("temperature t_ms must be non-decreasing")
        return v


class Dtc(BaseModel):
    model_config = _Strict
    code: str = Field(pattern=r"^[PCBU][0-3][0-9A-F]{3}$")
    status: Literal["stored", "pending", "permanent"]


class ObdSnapshotData(BaseModel):
    model_config = _Strict
    vin: str | None = Field(default=None, pattern=r"^[A-HJ-NPR-Z0-9]{17}$")
    mil_on: bool
    dtcs: list[Dtc] = Field(max_length=64)
    readiness: dict[Name, Literal["complete", "incomplete", "unsupported"]] = Field(max_length=16)
    distance_since_codes_cleared_km: int | None = Field(default=None, ge=0)
    warmups_since_codes_cleared: int | None = Field(default=None, ge=0)
    pids: dict[Name, float] | None = Field(default=None, max_length=32)


class _Envelope(BaseModel):
    model_config = _Strict
    provenance: Provenance
    device: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    captured_at: str

    @field_validator("captured_at")
    @classmethod
    def _utc_z(cls, v: str) -> str:
        if not v.endswith("Z"):
            raise ValueError("captured_at must be UTC with trailing Z")
        datetime.fromisoformat(v[:-1] + "+00:00")
        return v


class RigTelemetryEnvelope(_Envelope):
    kind: Literal["rig_telemetry"]
    data: RigTelemetryData


class ObdSnapshotEnvelope(_Envelope):
    kind: Literal["obd_snapshot"]
    data: ObdSnapshotData


SourceEnvelope = Annotated[Union[RigTelemetryEnvelope, ObdSnapshotEnvelope], Field(discriminator="kind")]
