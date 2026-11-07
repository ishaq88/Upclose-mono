from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import Confidence, ContractModel, Count, ObjectKey, PartNumber, Severity, UTCDateTime
from .enums import (
    PART_ZONES, ClaimVerdict, ComponentId, Concept, ConceptRunState,
    CoverageState, FindingStatus, PrivacyState, Subzone, VerificationStatus, Zone,
)


class BoundingBox(ContractModel):
    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    w: Annotated[float, Field(gt=0, le=1)]
    h: Annotated[float, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def within_frame(self) -> Self:
        if self.x + self.w > 1 or self.y + self.h > 1:
            raise ValueError("bbox extends beyond frame")
        return self


class EvidenceRef(ContractModel):
    frame_key: ObjectKey
    frame_index: Count
    frame_width: Annotated[int, Field(strict=True, gt=0)]
    frame_height: Annotated[int, Field(strict=True, gt=0)]
    bbox: BoundingBox
    mask_key: ObjectKey | None = None


class Finding(ContractModel):
    id: UUID
    session_id: UUID
    run_id: UUID
    part_id: UUID
    concept_run_id: UUID
    part_no: PartNumber
    zone: Zone
    track_id: Annotated[str, Field(min_length=1, max_length=128)]
    status: FindingStatus
    concept: Concept
    evidence: EvidenceRef
    severity: Severity | None = None
    confidence: Confidence | None = None
    evidence_note: Annotated[str, Field(max_length=240)]
    subzone: Subzone | None = None
    component_id: ComponentId | None = None
    flags: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = ()
    duplicate_of: UUID | None = None
    coverage_pct: None = None

    @model_validator(mode="after")
    def valid_finding(self) -> Self:
        if self.zone != PART_ZONES[self.part_no - 1]:
            raise ValueError("part number and zone disagree")
        if (self.status == FindingStatus.VERIFIED) != (self.severity is not None):
            raise ValueError("severity is required exactly for verified findings")
        if self.duplicate_of == self.id:
            raise ValueError("finding cannot duplicate itself")
        if self.duplicate_of is not None and self.status != FindingStatus.REJECTED:
            raise ValueError("duplicate finding must be rejected")
        return self


class Coverage(ContractModel):
    run_id: UUID
    zone: Zone
    concept: Concept
    state: CoverageState
    reason: Annotated[str, Field(min_length=1, max_length=240)] | None = None

    @model_validator(mode="after")
    def missing_assessment_reason(self) -> Self:
        if self.state != CoverageState.ASSESSED and not self.reason:
            raise ValueError("incomplete coverage requires a reason")
        return self


class ConceptRun(ContractModel):
    id: UUID
    run_id: UUID
    part_id: UUID
    zone: Zone
    concept: Concept
    prompt_id: Annotated[str, Field(min_length=1, max_length=128)]
    state: ConceptRunState
    reason: Annotated[str, Field(min_length=1, max_length=240)] | None = None

    @model_validator(mode="after")
    def failure_reason(self) -> Self:
        if self.state != ConceptRunState.SUCCEEDED and not self.reason:
            raise ValueError("unsuccessful concept requires a reason")
        return self


class VerificationVerdict(ContractModel):
    finding_id: UUID
    verdict: VerificationStatus
    concept: Concept
    severity: Severity | None = None
    confidence: Confidence | None = None
    evidence_note: Annotated[str, Field(max_length=240)]
    subzone: Subzone | None = None
    component_id: ComponentId | None = None
    duplicate_of: UUID | None = None
    flags: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = ()

    @model_validator(mode="after")
    def coherent_verdict(self) -> Self:
        if (self.verdict == VerificationStatus.CONFIRMED) != (self.severity is not None):
            raise ValueError("confirmed requires severity; other verdicts require null")
        if (self.verdict == VerificationStatus.DUPLICATE) != (self.duplicate_of is not None):
            raise ValueError("duplicate requires duplicate_of; other verdicts forbid it")
        if self.duplicate_of == self.finding_id:
            raise ValueError("finding cannot duplicate itself")
        return self


class VerificationGroup(ContractModel):
    """Construct from trusted input IDs plus untrusted parsed verdicts."""
    candidate_ids: frozenset[UUID]
    verdicts: tuple[VerificationVerdict, ...]

    @model_validator(mode="after")
    def references_in_group(self) -> Self:
        seen: set[UUID] = set()
        for verdict in self.verdicts:
            if verdict.finding_id not in self.candidate_ids or (
                verdict.duplicate_of is not None and verdict.duplicate_of not in self.candidate_ids
            ):
                raise ValueError("verdict references foreign finding")
            if verdict.finding_id in seen:
                raise ValueError("duplicate verdict for finding")
            seen.add(verdict.finding_id)
        if seen != self.candidate_ids:
            raise ValueError("every candidate requires exactly one verdict")
        verdict_map = {verdict.finding_id: verdict for verdict in self.verdicts}
        for verdict in self.verdicts:
            if verdict.duplicate_of is not None:
                target = verdict_map[verdict.duplicate_of]
                if target.verdict != VerificationStatus.CONFIRMED:
                    raise ValueError("duplicate target must be retained confirmed evidence")
        return self


class ClaimResult(ContractModel):
    claim_index: Count
    text: Annotated[str, Field(min_length=1, max_length=10000)]
    verdict: ClaimVerdict
    finding_ids: tuple[UUID, ...]
    evidence_frame_keys: tuple[ObjectKey, ...]
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def affirmative_evidence(self) -> Self:
        if self.verdict != ClaimVerdict.NOT_VISIBLE and not self.finding_ids:
            raise ValueError("supported or contradicted claims require finding evidence")
        return self


class ClaimGroup(ContractModel):
    """Trusted revision evidence whitelist plus model claim results."""
    findings: tuple[Finding, ...]
    claims: tuple[ClaimResult, ...]

    @model_validator(mode="after")
    def claim_references(self) -> Self:
        findings = {finding.id: finding for finding in self.findings}
        if len(findings) != len(self.findings):
            raise ValueError("duplicate evidence finding")
        if len({finding.session_id for finding in self.findings}) > 1:
            raise ValueError("claim evidence must belong to same session")
        for claim in self.claims:
            if not set(claim.finding_ids) <= findings.keys():
                raise ValueError("claim references foreign finding")
            frames = {findings[fid].evidence.frame_key for fid in claim.finding_ids}
            if any(findings[fid].status != FindingStatus.VERIFIED for fid in claim.finding_ids):
                raise ValueError("claims require verified evidence")
            if not set(claim.evidence_frame_keys) <= frames:
                raise ValueError("claim references foreign frame")
        return self


class Artifact(ContractModel):
    id: UUID
    session_id: UUID
    revision_id: UUID | None = None
    key: ObjectKey
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    privacy_state: PrivacyState
    created_at: UTCDateTime


class NarratedStatement(ContractModel):
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    finding_ids: tuple[UUID, ...]
