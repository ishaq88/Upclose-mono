"""Evidence-source wire format (UB Hacking contract) and derived summaries.

Plugin data is additional evidence: it never changes the condition score. Raw samples stay in
the database row, bound to the report by `data_sha256`; reports carry only the summary.
"""
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .base import ContractModel, UTCDateTime

SourceKind = Literal["rig_telemetry", "obd_snapshot"]
SOURCE_KINDS: tuple[SourceKind, ...] = ("rig_telemetry", "obd_snapshot")
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


class RigTelemetryData(ContractModel):
    accel: AccelData
    sound: SoundData | None = None
    temperature: Annotated[tuple[TemperatureReading, ...], Field(max_length=600)] = ()
    cylinders: Annotated[int, Field(strict=True, ge=1, le=16)] = 4

    @model_validator(mode="after")
    def ordered_temperature(self) -> Self:
        times = [row.t_ms for row in self.temperature]
        if any(later < earlier for earlier, later in zip(times, times[1:])):
            raise ValueError("temperature t_ms must not decrease")
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


SourceRequest = Annotated[RigTelemetrySource | ObdSnapshotSource, Field(discriminator="kind")]


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


SourceSummary = Annotated[RigTelemetrySummary | ObdSummary, Field(discriminator="kind")]


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
