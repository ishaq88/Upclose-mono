"""Generated, paid-scope additions stored with a report revision: advisory, narration audio, VIN check and
vibration series. All of it is frozen when the report is built, so a re-render never calls a provider again."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from app.briefing.advisory_block import AdvisoryBlock

from .base import ContractModel

Sha = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
MatchStatus = Literal["match", "mismatch", "not_checked"]


class NarrationAudioMeta(ContractModel):
    provider: Annotated[str, Field(max_length=64)]
    voice_id: Annotated[str, Field(max_length=128)]
    model_id: Annotated[str, Field(max_length=128)]
    provenance: Literal["synthetic", "live"]
    sha256: Sha
    storage_key: Annotated[str, Field(min_length=1, max_length=1024)]
    content_type: Annotated[str, Field(max_length=64)]
    size_bytes: Annotated[int, Field(strict=True, ge=1)]
    label: Annotated[str, Field(max_length=80)]


class FrozenSeriesPoint(ContractModel):
    t_s: float
    n_accel: Annotated[int, Field(strict=True, ge=0)]
    accel_rms: float | None = None
    sound_mean: float | None = None
    sound_max: int | None = None


class FrozenVibration(ContractModel):
    series_source: Literal["tiger", "computed"]
    state: Literal["assessed", "not_assessed"]
    reason: Annotated[str, Field(max_length=120)] | None = None
    sample_count: Annotated[int, Field(strict=True, ge=0)]
    duration_s: Annotated[float, Field(ge=0)]
    points: Annotated[tuple[FrozenSeriesPoint, ...], Field(max_length=600)] = ()


class FrozenVinDecode(ContractModel):
    make: Annotated[str, Field(max_length=64)] | None = None
    model: Annotated[str, Field(max_length=64)] | None = None
    model_year: Annotated[int, Field(strict=True, ge=1900, le=2100)] | None = None
    cylinders: Annotated[int, Field(strict=True, ge=1, le=16)] | None = None
    displacement_l: Annotated[float, Field(ge=0, le=20)] | None = None
    fuel_type: Annotated[str, Field(max_length=64)] | None = None
    body_class: Annotated[str, Field(max_length=64)] | None = None
    source: Literal["fixture", "nhtsa-vpic"]


class FrozenVin(ContractModel):
    """VIN facts. The free view shows only make, model, year and `match`, never the VIN."""
    vin: Annotated[str, Field(pattern=r"^[A-HJ-NPR-Z0-9]{17}$")]
    check_digit: Literal["valid", "invalid", "not_applicable"]
    year_candidates: Annotated[tuple[int, ...], Field(max_length=4)] = ()
    manufacturer: Annotated[str, Field(max_length=64)] | None = None
    decode: FrozenVinDecode | None = None
    decode_reason: Annotated[str, Field(max_length=120)] | None = None
    match: MatchStatus = "not_checked"


class StoredBriefing(ContractModel):
    advisory: AdvisoryBlock | None = None
    narration_audio: NarrationAudioMeta | None = None
    vin: FrozenVin | None = None
    vibration: FrozenVibration | None = None


class FreeVehicleCheck(ContractModel):
    make: Annotated[str, Field(max_length=64)] | None = None
    model: Annotated[str, Field(max_length=64)] | None = None
    model_year: Annotated[int, Field(strict=True, ge=1900, le=2100)] | None = None
    match: MatchStatus = "not_checked"


class NarrationAudioView(ContractModel):
    """Paid report: a spoken version exists. Served only through the entitlement-checked narration route."""
    label: Annotated[str, Field(max_length=80)]
    provenance: Literal["synthetic", "live"]
    content_type: Annotated[str, Field(max_length=64)]
