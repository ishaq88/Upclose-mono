from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import Confidence, ContractModel, Count, Score, Severity, UTCDateTime
from .enums import ClaimVerdict, ComponentId, Concept, CoverageState, ReportState, Subzone, Zone
from .evidence import BoundingBox, ClaimResult, NarratedStatement
from .scoring import RepairSummary, ScoreResult


class PublicCoverage(ContractModel):
    zone: Zone
    state: CoverageState
    reason: Annotated[str, Field(max_length=240)] | None = None


class PublicFinding(ContractModel):
    """Evidence route identifies a validated finding, never a storage key/URL."""
    id: UUID
    zone: Zone
    concept: Concept
    severity: Severity
    confidence: Confidence | None
    confidence_label: Literal["Uncalibrated model confidence, not an accuracy probability"] = "Uncalibrated model confidence, not an accuracy probability"
    evidence_note: Annotated[str, Field(max_length=240)]
    subzone: Subzone | None
    component_id: ComponentId | None
    flags: tuple[str, ...]
    bbox: BoundingBox
    evidence_available: bool
    source_timestamp_ms: Annotated[int, Field(strict=True, ge=0, le=180250)] | None = None
    mapping_basis: Literal["user_declared"] | None = None


class FreeFinding(PublicFinding):
    zone: Literal[Zone.DRIVER_SIDE] = Zone.DRIVER_SIDE


class PaidFinding(PublicFinding):
    pass


class PublicClaim(ContractModel):
    claim_index: Count
    text: Annotated[str, Field(min_length=1, max_length=10000)]
    verdict: ClaimVerdict
    finding_ids: tuple[UUID, ...]
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def affirmative_evidence(self) -> Self:
        if self.verdict != ClaimVerdict.NOT_VISIBLE and not self.finding_ids:
            raise ValueError("supported or contradicted claims require finding evidence")
        return self


class LockedZoneSummary(ContractModel):
    zone: Zone
    finding_count: Count
    locked: Literal[True] = True

    @model_validator(mode="after")
    def only_locked_zones(self) -> Self:
        if self.zone == Zone.DRIVER_SIDE:
            raise ValueError("driver side is free")
        return self


class ReportStatus(ContractModel):
    """A non-ready report exposes status only, never partial paid stage outputs."""
    scope: Literal["free", "paid"]
    slug: Annotated[str, Field(min_length=1, max_length=128)]
    revision_id: UUID
    state: Literal[ReportState.BUILDING, ReportState.FAILED]


class CandidateFinding(ContractModel):
    # Paid-only: SAM candidate whose verdict is still unverified. Never scored, never a finding.
    id: UUID
    zone: Zone
    concept: Concept
    confidence: Confidence | None
    confidence_label: Literal["Uncalibrated model confidence, not an accuracy probability"] = "Uncalibrated model confidence, not an accuracy probability"
    evidence_note: Annotated[str, Field(max_length=240)]
    bbox: BoundingBox
    evidence_available: bool
    source_timestamp_ms: Annotated[int, Field(strict=True, ge=0, le=180250)] | None = None
    mapping_basis: Literal["user_declared"] | None = None


class FreeReport(ContractModel):
    scope: Literal["free"] = "free"
    slug: Annotated[str, Field(min_length=1, max_length=128)]
    revision_id: UUID
    state: ReportState
    vehicle_label: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    coverage: tuple[PublicCoverage, ...]
    part1_score: Score | None
    findings: tuple[FreeFinding, ...]
    locked_zones: tuple[LockedZoneSummary, ...]
    part1_summary: Annotated[str, Field(max_length=2000)]


class ShareEnableResponse(ContractModel):
    """Owner-only share enable result. `url` is the existing public report
    path (`/r/{slug}`); `public_access_active` is
    `settings.public_sharing_enabled and share_enabled`, so callers can tell
    whether the link actually opens today while the global gate is off."""
    url: Annotated[str, Field(min_length=1, max_length=256)]
    share_enabled: Literal[True] = True
    public_access_active: bool


class ShareDisableResponse(ContractModel):
    share_enabled: Literal[False] = False


class Part1Verdict(ContractModel):
    """Owner-progress free verdict. No report identity: before finalize no report exists,
    and afterwards `ProgressResponse.report` carries the slug and state."""
    coverage: tuple[PublicCoverage, ...]
    part1_score: Score | None
    findings: tuple[FreeFinding, ...]
    locked_zones: tuple[LockedZoneSummary, ...]
    part1_summary: Annotated[str, Field(max_length=2000)]


class PaidReport(ContractModel):
    scope: Literal["paid"] = "paid"
    slug: Annotated[str, Field(min_length=1, max_length=128)]
    revision_id: UUID
    state: ReportState
    vehicle_label: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    coverage: tuple[PublicCoverage, ...]
    scores: ScoreResult
    findings: tuple[PaidFinding, ...]
    candidates: tuple[CandidateFinding, ...] = ()
    repairs: RepairSummary
    claims: tuple[PublicClaim, ...]
    narration: tuple[NarratedStatement, ...]
    rendered_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def citations_in_public_scope(self) -> Self:
        ids = {finding.id for finding in self.findings}
        if len(ids) != len(self.findings):
            raise ValueError("duplicate public finding ID")
        for statement in (*self.claims, *self.narration):
            if not set(statement.finding_ids) <= ids:
                raise ValueError("citation references finding outside report scope")
        for estimate in self.repairs.items:
            if not set(estimate.finding_ids) <= ids:
                raise ValueError("repair references finding outside report scope")
        return self
