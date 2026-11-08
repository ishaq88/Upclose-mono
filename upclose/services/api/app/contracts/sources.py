"""Evidence-source wire format and derived summaries.

Plugin data is additional evidence: it never changes the condition score. Raw samples stay in
the database row, bound to the report by `data_sha256`; reports carry only the summary.
"""
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .base import ContractModel, UTCDateTime

SourceKind = Literal["rig_telemetry", "obd_snapshot", "engine_audio"]
SOURCE_KINDS: tuple[SourceKind, ...] = ("rig_telemetry", "obd_snapshot", "engine_audio")
SourceProvenance = Literal["synthetic", "cached", "live"]
Sha = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
Device = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
PidName = Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")]
UNCALIBRATED_NOTE = "Values are uncalibrated estimates, not measurements."

MAX_SAMPLES = 240000
AccelSamples = Annotated[tuple[Annotated[int, Field(strict=True, ge=-16000, le=16000)], ...],
                         Field(min_length=1, max_length=MAX_SAMPLES)]


class AccelData(ContractModel):
    rate_hz: Annotated[float, Field(ge=50, le=1000)]
    x_mg: AccelSamples
    y_mg: AccelSamples
    z_mg: AccelSamples

    @model_validator(mode="after")
    def equal_axes(self) -> Self:
        if not len(self.x_mg) == len(self.y_mg) == len(self.z_mg):
            raise ValueError("accelerometer axes must have equal length")
        return self


class SoundData(ContractModel):
    rate_hz: Annotated[float, Field(ge=1, le=1000)]
    level: Annotated[tuple[Annotated[int, Field(strict=True, ge=0, le=1023)], ...],
                     Field(min_length=1, max_length=MAX_SAMPLES)]


class TemperatureReading(ContractModel):
    t_ms: Annotated[int, Field(strict=True, ge=0)]
    celsius: Annotated[float, Field(ge=-40, le=150)]

FeatureName = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,48}$")]
_FRACTION_TOLERANCE = 0.0006


class VerdictFeature(ContractModel):
    name: FeatureName
    share: Annotated[float, Field(ge=0, le=1)]


class VerdictRunFeature(VerdictFeature):
    z: Annotated[float, Field(ge=-1000, le=1000)]


class VerdictRun(ContractModel):
    t_start_s: Annotated[float, Field(ge=0, le=100000)]
    t_end_s: Annotated[float, Field(ge=0, le=100000)]
    peak_score: Annotated[float, Field(ge=0, le=1e9)]
    top_features: Annotated[tuple[VerdictRunFeature, ...], Field(max_length=5)] = ()

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.t_end_s <= self.t_start_s:
            raise ValueError("anomalous run must end after it starts")
        return self


class VerdictModel(ContractModel):
    fire_hz: Annotated[float, Field(gt=0, le=1000)]
    fs_hz: Annotated[float, Field(gt=0, le=10000)]
    has_audio: bool
    baseline_seconds: Annotated[float, Field(gt=0, le=100000)]
    baseline_source: Annotated[str, Field(pattern=r"^[a-z0-9_.-]{1,32}$")]
    threshold_percentile: Annotated[float, Field(gt=0, le=100)]
    config_hash: Sha


class ModelVerdict(ContractModel):
    """Engine-anomaly model output (upclose-anomaly-verdict/1): relative to a baseline, not a diagnosis."""
    model_config = ContractModel.model_config | {"populate_by_name": True, "protected_namespaces": (), "serialize_by_alias": True}

    schema_: Literal["upclose-anomaly-verdict/1"] = Field(alias="schema")
    verdict: Literal["anomaly_suspected", "no_anomaly_detected", "not_assessed"]
    reason: Annotated[str, Field(pattern=r"^[a-z_]{1,48}$")] | None
    data_source: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    duration_s: Annotated[float, Field(ge=0, le=100000)]
    windows: Annotated[int, Field(strict=True, ge=0, le=1000000)] | None
    anomalous_windows: Annotated[int, Field(strict=True, ge=0, le=1000000)] | None
    anomalous_fraction: Annotated[float, Field(ge=0, le=1)] | None
    max_score: Annotated[float, Field(ge=0, le=1e9)] | None
    median_score: Annotated[float, Field(ge=0, le=1e9)] | None
    threshold: Annotated[float, Field(gt=0, le=1e9)]
    fraction_threshold: Annotated[float, Field(gt=0, le=1)]
    min_anomalous_windows: Annotated[int, Field(strict=True, ge=1, le=1000)]
    top_features: Annotated[tuple[VerdictFeature, ...], Field(max_length=5)] = ()
    anomalous_runs: Annotated[tuple[VerdictRun, ...], Field(max_length=10)] = ()
    model: VerdictModel
    note: Annotated[str, Field(max_length=300)]

    @model_validator(mode="after")
    def internally_consistent(self) -> Self:
        counts = (self.windows, self.anomalous_windows, self.anomalous_fraction, self.max_score, self.median_score)
        if self.verdict == "not_assessed":
            if self.reason is None or any(value is not None for value in counts):
                raise ValueError("not_assessed carries a reason and null counts")
            if self.top_features or self.anomalous_runs:
                raise ValueError("not_assessed carries no features or runs")
            return self
        if self.reason is not None or any(value is None for value in counts):
            raise ValueError("an assessed verdict has counts and no reason")
        windows, bad = self.windows, self.anomalous_windows
        fraction, top, median = self.anomalous_fraction, self.max_score, self.median_score
        assert windows is not None and bad is not None and fraction is not None and top is not None and median is not None
        if windows < 1 or bad > windows:
            raise ValueError("window counts are inconsistent")
        if windows > self.duration_s / 0.25 + 1:
            raise ValueError("more windows than the recording can hold")
        if abs(fraction - bad / windows) > _FRACTION_TOLERANCE:
            raise ValueError("anomalous_fraction is not anomalous_windows / windows")
        suspected = bad >= self.min_anomalous_windows and fraction >= self.fraction_threshold - _FRACTION_TOLERANCE
        if suspected != (self.verdict == "anomaly_suspected"):
            raise ValueError("verdict disagrees with the stated thresholds")
        if median > top:
            raise ValueError("median score exceeds max score")
        if bad > 0 and top < self.threshold:
            raise ValueError("anomalous windows need a score above the threshold")
        if self.top_features and not suspected:
            raise ValueError("features are reported only for a suspected anomaly")
        if sum(row.share for row in self.top_features) > 1.001:
            raise ValueError("feature shares exceed one")
        if (bad == 0) != (not self.anomalous_runs) or len(self.anomalous_runs) > bad:
            raise ValueError("anomalous runs disagree with the window count")
        runs = self.anomalous_runs
        if any(later.t_start_s < earlier.t_end_s for earlier, later in zip(runs, runs[1:])):
            raise ValueError("anomalous runs must be ordered and not overlap")
        if runs and runs[-1].t_end_s > self.duration_s + 0.001:
            raise ValueError("anomalous run lies outside the recording")
        if any(row.peak_score > top + 0.001 for row in runs):
            raise ValueError("run peak exceeds max score")
        return self


class RigTelemetryData(ContractModel):
    accel: AccelData
    sound: SoundData | None = None
    temperature: Annotated[tuple[TemperatureReading, ...], Field(max_length=600)] = ()
    cylinders: Annotated[int, Field(strict=True, ge=1, le=16)] = 4
    model_verdict: ModelVerdict | None = None

    @model_validator(mode="after")
    def ordered_temperature(self) -> Self:
        times = [row.t_ms for row in self.temperature]
        if any(later < earlier for earlier, later in zip(times, times[1:])):
            raise ValueError("temperature t_ms must not decrease")
        return self

    @model_validator(mode="after")
    def verdict_within_recording(self) -> Self:
        if (self.model_verdict is not None
                and self.model_verdict.duration_s > len(self.accel.x_mg) / self.accel.rate_hz + 1.0):
            raise ValueError("verdict covers more time than the recording")
        return self


class DtcEntry(ContractModel):
    code: Annotated[str, Field(pattern=r"^[PCBU][0-3][0-9A-F]{3}$")]
    status: Literal["stored", "pending", "permanent"]


class ObdSnapshotData(ContractModel):
    vin: Annotated[str, Field(pattern=r"^[A-HJ-NPR-Z0-9]{17}$")] | None = None
    mil_on: Annotated[bool, Field(strict=True)]
    dtcs: Annotated[tuple[DtcEntry, ...], Field(max_length=64)] = ()
    readiness: Annotated[dict[Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")],
                              Literal["complete", "incomplete", "unsupported"]], Field(max_length=16)] = {}
    distance_since_codes_cleared_km: Annotated[int, Field(strict=True, ge=0, le=65535)] | None = None
    warmups_since_codes_cleared: Annotated[int, Field(strict=True, ge=0, le=255)] | None = None
    pids: Annotated[dict[PidName, float], Field(max_length=32)] = {}


class _SourceEnvelope(ContractModel):
    provenance: SourceProvenance
    device: Device
    captured_at: UTCDateTime


class RigTelemetrySource(_SourceEnvelope):
    kind: Literal["rig_telemetry"]
    data: RigTelemetryData


class ObdSnapshotSource(_SourceEnvelope):
    kind: Literal["obd_snapshot"]
    data: ObdSnapshotData


class EngineAudioData(ContractModel):
    """Reference to a completed private audio upload; the server verifies its size and sha256."""
    upload_id: UUID
    sha256: Sha


class EngineAudioSource(_SourceEnvelope):
    kind: Literal["engine_audio"]
    data: EngineAudioData


SourceRequest = Annotated[RigTelemetrySource | ObdSnapshotSource | EngineAudioSource, Field(discriminator="kind")]


# ---- derived summaries (computed by app.domain.sources) ----------------------------------------


class IrregularWindow(ContractModel):
    t_ms: Annotated[int, Field(strict=True, ge=0)]
    rms_mg: Annotated[float, Field(ge=0)]
    status: Literal["unverified"] = "unverified"


class TemperatureSummary(ContractModel):
    start_c: float
    rise_c: float | None
    engine_warm_at_start: bool


class RigTelemetrySummary(ContractModel):
    kind: Literal["rig_telemetry"] = "rig_telemetry"
    config_hash: Sha
    provenance: SourceProvenance
    state: Literal["assessed", "not_assessed"]
    reason: Annotated[str, Field(max_length=240)] | None = None
    sample_count: Annotated[int, Field(strict=True, ge=0)]
    duration_s: Annotated[float, Field(ge=0)]
    dominant_hz: float | None = None
    rpm_estimate: Annotated[int, Field(strict=True, ge=0)] | None = None
    cylinders: Annotated[int, Field(strict=True, ge=1, le=16)]
    roughness: Annotated[float, Field(ge=0)] | None = None
    irregular_windows: Annotated[tuple[IrregularWindow, ...], Field(max_length=20)] = ()
    temperature: TemperatureSummary | None = None
    model_verdict: ModelVerdict | None = None
    flags: tuple[str, ...] = ()
    flag_count: Annotated[int, Field(strict=True, ge=0)] = 0
    uncalibrated_note: Literal["Values are uncalibrated estimates, not measurements."] = UNCALIBRATED_NOTE


class ObdSummary(ContractModel):
    kind: Literal["obd_snapshot"] = "obd_snapshot"
    config_hash: Sha
    provenance: SourceProvenance
    state: Literal["assessed", "not_assessed"] = "assessed"
    reason: Annotated[str, Field(max_length=240)] | None = None
    vin: str | None = None
    mil_on: Annotated[bool, Field(strict=True)]
    dtcs: tuple[DtcEntry, ...] = ()
    incomplete_monitors: tuple[str, ...] = ()
    codes_recently_cleared: bool | None = None
    cold_start: bool | None = None
    coolant_c: float | None = None
    distance_since_codes_cleared_km: int | None = None
    warmups_since_codes_cleared: int | None = None
    flags: tuple[str, ...] = ()
    flag_count: Annotated[int, Field(strict=True, ge=0)] = 0
    uncalibrated_note: Literal["Values are uncalibrated estimates, not measurements."] = UNCALIBRATED_NOTE


class EngineAudioSummary(ContractModel):
    kind: Literal["engine_audio"] = "engine_audio"
    config_hash: Sha
    provenance: SourceProvenance
    state: Literal["processing", "assessed", "not_assessed"]
    reason: Annotated[str, Field(max_length=240)] | None = None
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=10485760)]
    content_type: Annotated[str, Field(max_length=32)]
    duration_s: Annotated[float, Field(ge=0, le=1000)] | None = None
    rms_db: Annotated[float, Field(ge=-120, le=0)] | None = None
    peak_db: Annotated[float, Field(ge=-120, le=0)] | None = None
    envelope_db: Annotated[tuple[Annotated[float, Field(ge=-120, le=0)], ...], Field(max_length=240)] = ()
    envelope_step_ms: Annotated[int, Field(strict=True, ge=1)]
    spectrogram_sha256: Sha | None = None
    flags: tuple[str, ...] = ()
    flag_count: Annotated[int, Field(strict=True, ge=0)] = 0
    uncalibrated_note: Literal["Values are uncalibrated estimates, not measurements."] = UNCALIBRATED_NOTE


SourceSummary = Annotated[RigTelemetrySummary | ObdSummary | EngineAudioSummary, Field(discriminator="kind")]


class SourceRecord(ContractModel):
    """One frozen source: summary plus the hash binding it to the raw row. Manifest and paid report."""
    kind: SourceKind
    provenance: SourceProvenance
    device: Device
    captured_at: UTCDateTime
    data_sha256: Sha
    summary: SourceSummary

    @model_validator(mode="after")
    def summary_matches(self) -> Self:
        if self.summary.kind != self.kind or self.summary.provenance != self.provenance:
            raise ValueError("source summary disagrees with its envelope")
        return self


class SourceResponse(ContractModel):
    source_id: UUID
    kind: SourceKind
    data_sha256: Sha
    summary: SourceSummary


class FreeSourceCheck(ContractModel):
    """Free scope: only whether a kind was checked and how many flags it raised. No values."""
    kind: SourceKind
    checked: bool
    flag_count: Annotated[int, Field(strict=True, ge=0)] = 0


class PaidSourceCheck(ContractModel):
    kind: SourceKind
    checked: bool
    source: SourceRecord | None = None


class SourceListItem(ContractModel):
    """Owner read of an attached source: envelope, hash and summary, never the raw samples."""
    source_id: UUID
    kind: SourceKind
    provenance: SourceProvenance
    device: Device
    captured_at: UTCDateTime
    data_sha256: Sha
    summary: SourceSummary


class SourceListResponse(ContractModel):
    sources: tuple[SourceListItem, ...]
