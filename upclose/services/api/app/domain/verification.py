"""Verifier verdict validation, finding resolution and coverage derivation.

Pure rules over contract types. Model output is untrusted: a verdict is parsed strictly,
checked against its trusted candidate group and rubric registry, and only then mapped to a
finding resolution. Model confidence is never a calibrated probability and is labeled so.
Failure, doubt or missing support makes coverage partial or not assessed, never clean.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from app.contracts.base import Confidence, ContractModel, Count, ObjectKey, Severity
from app.contracts.enums import (
    ComponentId, Concept, ConceptRunState, CoverageState, FindingStatus, Subzone, VerificationStatus, Zone,
)
from app.contracts.evidence import ConceptRun, Coverage, Finding, VerificationGroup, VerificationVerdict

from .dedup import AmbiguousOverlap, unresolved_overlaps
from .rubrics import MAX_REASON_CHARS, RubricRegistry

CONFIDENCE_LABEL: Literal["uncalibrated"] = "uncalibrated"
CONFIDENCE_NOTE = "Model confidence is an uncalibrated score, not a probability of being correct."


# ------------------------------------------------------------ verdict validation

class RejectionCode(StrEnum):
    SCHEMA_INVALID = "schema_invalid"
    INVALID_CANDIDATES = "invalid_candidates"
    UNKNOWN_REFERENCE = "unknown_reference"
    DUPLICATE_VERDICT = "duplicate_verdict"
    MISSING_VERDICT = "missing_verdict"
    SEVERITY_INVALID = "severity_invalid"
    CONCEPT_MISMATCH = "concept_mismatch"
    RUBRIC_UNSUPPORTED = "rubric_unsupported"
    DUPLICATE_TARGET_INVALID = "duplicate_target_invalid"
    GROUP_INVALID = "group_invalid"


class VerdictRejected(ValueError):
    """Verdict output is unusable. The message never embeds provider payload text."""

    def __init__(self, code: RejectionCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def parse_verdict(payload: str | bytes | Mapping[str, Any]) -> VerificationVerdict:
    """Strictly parse one model verdict (unknown fields, fake enums, severity 0/6 all fail)."""
    try:
        if isinstance(payload, (str, bytes)):
            return VerificationVerdict.model_validate_json(payload)
        return VerificationVerdict.model_validate(dict(payload))
    except ValidationError as error:
        fields = ",".join(sorted({".".join(str(part) for part in row["loc"]) or "<root>" for row in error.errors()}))
        raise VerdictRejected(RejectionCode.SCHEMA_INVALID, f"invalid fields: {fields}"[:MAX_REASON_CHARS]) from None
    except (json.JSONDecodeError, ValueError, TypeError):
        raise VerdictRejected(RejectionCode.SCHEMA_INVALID, "payload is not a verdict object") from None


def _check_candidates(candidates: Sequence[Finding]) -> dict[UUID, Finding]:
    by_id = {row.id: row for row in candidates}
    if not candidates or len(by_id) != len(candidates):
        raise VerdictRejected(RejectionCode.INVALID_CANDIDATES, "group needs unique candidates")
    if len({row.session_id for row in candidates}) > 1 or len({row.part_id for row in candidates}) > 1:
        raise VerdictRejected(RejectionCode.INVALID_CANDIDATES, "group spans sessions or parts")
    if any(row.status != FindingStatus.CANDIDATE for row in candidates):
        raise VerdictRejected(RejectionCode.INVALID_CANDIDATES, "only candidates are verified")
    return by_id


def _check_verdict(verdict: VerificationVerdict, candidate: Finding, rubrics: RubricRegistry) -> None:
    confirmed = verdict.verdict == VerificationStatus.CONFIRMED
    severity = verdict.severity
    valid_severity = isinstance(severity, int) and not isinstance(severity, bool) and 1 <= severity <= 5
    if confirmed != (severity is not None) or (confirmed and not valid_severity):
        raise VerdictRejected(RejectionCode.SEVERITY_INVALID, "confirmed requires severity 1..5; others forbid it")
    if confirmed and verdict.concept != candidate.concept:
        raise VerdictRejected(RejectionCode.CONCEPT_MISMATCH, "confirmed concept differs from the candidate")
    if confirmed and not rubrics.is_enabled(candidate.concept):
        raise VerdictRejected(RejectionCode.RUBRIC_UNSUPPORTED, "no enabled rubric for the confirmed concept")


def validate_verdict_group(candidates: Sequence[Finding], verdicts: Sequence[VerificationVerdict],
                           rubrics: RubricRegistry) -> VerificationGroup:
    """Check verdicts against their trusted candidates; return the validated group, sorted by ID."""
    by_id = _check_candidates(candidates)
    seen: set[UUID] = set()
    for verdict in verdicts:
        if verdict.finding_id not in by_id or (verdict.duplicate_of is not None and verdict.duplicate_of not in by_id):
            raise VerdictRejected(RejectionCode.UNKNOWN_REFERENCE, "verdict references a finding outside the group")
        if verdict.finding_id in seen:
            raise VerdictRejected(RejectionCode.DUPLICATE_VERDICT, "more than one verdict for a finding")
        seen.add(verdict.finding_id)
        _check_verdict(verdict, by_id[verdict.finding_id], rubrics)
    if seen != set(by_id):
        raise VerdictRejected(RejectionCode.MISSING_VERDICT, "every candidate requires exactly one verdict")
    confirmed = {row.finding_id for row in verdicts if row.verdict == VerificationStatus.CONFIRMED}
    for verdict in verdicts:
        if verdict.duplicate_of is None:
            continue
        target_ok = verdict.duplicate_of in confirmed and by_id[verdict.duplicate_of].concept == by_id[verdict.finding_id].concept
        if not target_ok:
            raise VerdictRejected(RejectionCode.DUPLICATE_TARGET_INVALID,
                                  "duplicate target must be a confirmed finding of the same concept")
    try:
        return VerificationGroup(candidate_ids=frozenset(by_id), verdicts=tuple(sorted(verdicts, key=lambda row: row.finding_id)))
    except ValidationError:
        raise VerdictRejected(RejectionCode.GROUP_INVALID, "group failed contract validation") from None


# ------------------------------------------------------------------ resolution

class ResolutionReason(StrEnum):
    CONFIRMED = "confirmed"
    ARTIFACT = "rejected_artifact"
    WRONG_TYPE = "rejected_wrong_type"
    DUPLICATE = "rejected_duplicate_linked"
    UNVERIFIABLE = "unverified"


class ResolutionProvenance(ContractModel):
    """Frame and rubric provenance; rubric fields are mandatory for verified findings."""
    part_id: UUID
    concept_run_id: UUID
    track_id: str
    frame_key: ObjectKey
    frame_index: Count
    verdict: VerificationStatus
    link_source: Literal["verifier"] = "verifier"
    duplicate_of: UUID | None = None
    rubric_id: str | None = None
    rubric_version: str | None = None
    rubric_config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None = None
    concept_rubric_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None = None


class FindingResolution(ContractModel):
    finding_id: UUID
    status: FindingStatus
    reason: ResolutionReason
    concept: Concept
    severity: Severity | None
    confidence: Confidence | None
    confidence_label: Literal["uncalibrated"] = CONFIDENCE_LABEL
    evidence_note: Annotated[str, Field(max_length=240)]
    subzone: Subzone | None
    component_id: ComponentId | None
    flags: tuple[str, ...]
    duplicate_of: UUID | None
    scoreable: bool  # verified, nonduplicate: contributes to the score exactly once
    provenance: ResolutionProvenance

    @model_validator(mode="after")
    def coherent_resolution(self) -> Self:
        if (self.status == FindingStatus.VERIFIED) != (self.severity is not None):
            raise ValueError("severity is required exactly for verified findings")
        if self.scoreable != (self.status == FindingStatus.VERIFIED and self.duplicate_of is None):
            raise ValueError("only verified nonduplicate findings are scoreable")
        if self.status == FindingStatus.VERIFIED and not all((
                self.provenance.rubric_id, self.provenance.rubric_version,
                self.provenance.rubric_config_hash, self.provenance.concept_rubric_hash)):
            raise ValueError("verified findings require rubric provenance")
        return self


_STATUS = {
    VerificationStatus.CONFIRMED: (FindingStatus.VERIFIED, ResolutionReason.CONFIRMED),
    VerificationStatus.ARTIFACT: (FindingStatus.REJECTED, ResolutionReason.ARTIFACT),
    VerificationStatus.WRONG_TYPE: (FindingStatus.REJECTED, ResolutionReason.WRONG_TYPE),
    VerificationStatus.DUPLICATE: (FindingStatus.REJECTED, ResolutionReason.DUPLICATE),
    VerificationStatus.UNVERIFIABLE: (FindingStatus.UNVERIFIED, ResolutionReason.UNVERIFIABLE),
}


def _merged_flags(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({flag for group in groups for flag in group}))


def resolve_verdict(candidate: Finding, verdict: VerificationVerdict, rubrics: RubricRegistry) -> FindingResolution:
    """Map one checked verdict to a resolution. Flags stay independent of status."""
    _check_candidates([candidate])
    if verdict.finding_id != candidate.id:
        raise VerdictRejected(RejectionCode.UNKNOWN_REFERENCE, "verdict is for a different finding")
    _check_verdict(verdict, candidate, rubrics)
    status, reason = _STATUS[verdict.verdict]
    confirmed = status == FindingStatus.VERIFIED
    rubric = rubrics.get(candidate.concept) if confirmed else None
    evidence = candidate.evidence
    provenance = ResolutionProvenance(
        part_id=candidate.part_id, concept_run_id=candidate.concept_run_id, track_id=candidate.track_id,
        frame_key=evidence.frame_key, frame_index=evidence.frame_index, verdict=verdict.verdict,
        duplicate_of=verdict.duplicate_of,
        rubric_id=rubric.rubric_id if rubric else None, rubric_version=rubrics.version if rubric else None,
        rubric_config_hash=rubrics.config_hash if rubric else None,
        concept_rubric_hash=rubric.rubric_hash if rubric else None)
    return FindingResolution(
        finding_id=candidate.id, status=status, reason=reason, concept=candidate.concept,
        severity=verdict.severity, confidence=verdict.confidence, evidence_note=verdict.evidence_note,
        subzone=verdict.subzone if verdict.subzone is not None else candidate.subzone,
        component_id=verdict.component_id if verdict.component_id is not None else candidate.component_id,
        flags=_merged_flags(candidate.flags, verdict.flags), duplicate_of=verdict.duplicate_of,
        scoreable=confirmed and verdict.duplicate_of is None, provenance=provenance)


def resolve_group(candidates: Sequence[Finding], verdicts: Sequence[VerificationVerdict],
                  rubrics: RubricRegistry) -> tuple[FindingResolution, ...]:
    """Validate a whole group, then resolve every candidate; output is sorted by finding ID."""
    group = validate_verdict_group(candidates, verdicts, rubrics)
    by_id = {row.id: row for row in candidates}
    return tuple(resolve_verdict(by_id[row.finding_id], row, rubrics) for row in group.verdicts)


def apply_resolution(finding: Finding, resolution: FindingResolution) -> Finding:
    """Produce the resolved finding; Finding validators re-check the status/severity rules."""
    if finding.id != resolution.finding_id or finding.concept != resolution.concept:
        raise ValueError("resolution does not match finding")
    fields = finding.model_dump()
    fields.update(status=resolution.status, severity=resolution.severity, confidence=resolution.confidence,
                  evidence_note=resolution.evidence_note, subzone=resolution.subzone,
                  component_id=resolution.component_id, flags=resolution.flags, duplicate_of=resolution.duplicate_of)
    return Finding.model_validate(fields)


# -------------------------------------------------------------------- coverage

class CoverageReason(StrEnum):
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    RUBRIC_UNSUPPORTED = "rubric_unsupported"
    CONCEPT_RUN_MISSING = "concept_run_missing"
    CONCEPT_UNSUPPORTED = "concept_unsupported"
    CONCEPT_FAILED = "concept_failed"
    CONCEPT_BUDGET_EXHAUSTED = "concept_budget_exhausted"
    CAPTURE_QUALITY_UNUSABLE = "capture_quality_unusable"
    UNRESOLVED_CANDIDATES = "unresolved_candidates"
    DUPLICATE_REVIEW_UNRESOLVED = "duplicate_review_unresolved"


_REASON_TEXT = {
    CoverageReason.VERIFICATION_UNAVAILABLE: "Live verification is unavailable; footage is not fully assessed.",
    CoverageReason.CONCEPT_RUN_MISSING: "A required assessment pass did not run.",
    CoverageReason.CONCEPT_UNSUPPORTED: "This assessment is not supported for the footage.",
    CoverageReason.CONCEPT_FAILED: "A required assessment pass failed.",
    CoverageReason.CONCEPT_BUDGET_EXHAUSTED: "A required assessment pass was skipped for budget.",
    CoverageReason.CAPTURE_QUALITY_UNUSABLE: "Capture quality was too low to assess.",
    CoverageReason.UNRESOLVED_CANDIDATES: "Some candidates are not yet confirmed or rejected.",
    CoverageReason.DUPLICATE_REVIEW_UNRESOLVED: "Overlapping candidates could not be told apart.",
}
_RUN_STATE_REASON = {
    ConceptRunState.FAILED: CoverageReason.CONCEPT_FAILED,
    ConceptRunState.BUDGET_EXHAUSTED: CoverageReason.CONCEPT_BUDGET_EXHAUSTED,
}
_ORDER = (CoverageReason.RUBRIC_UNSUPPORTED, CoverageReason.VERIFICATION_UNAVAILABLE,
          CoverageReason.CONCEPT_RUN_MISSING, CoverageReason.CONCEPT_UNSUPPORTED,
          CoverageReason.CONCEPT_FAILED, CoverageReason.CONCEPT_BUDGET_EXHAUSTED, CoverageReason.CAPTURE_QUALITY_UNUSABLE,
          CoverageReason.UNRESOLVED_CANDIDATES, CoverageReason.DUPLICATE_REVIEW_UNRESOLVED)


def _run_issues(concept: Concept, expected: Collection[str], runs: Sequence[ConceptRun]) -> tuple[bool, set[CoverageReason]]:
    """Return (nothing_usable, reasons) for a concept's required passes."""
    by_prompt: dict[str, ConceptRun] = {}
    for row in runs:
        if row.concept != concept or row.prompt_id not in expected or row.prompt_id in by_prompt:
            raise ValueError("concept run does not match the expected prompt plan")
        by_prompt[row.prompt_id] = row
    if not expected or not by_prompt:
        return True, {CoverageReason.CONCEPT_RUN_MISSING}
    reasons: set[CoverageReason] = set()
    if set(by_prompt) != set(expected):
        reasons.add(CoverageReason.CONCEPT_RUN_MISSING)
    states = {row.state for row in by_prompt.values()}
    if states == {ConceptRunState.UNSUPPORTED} and not reasons:
        return True, {CoverageReason.CONCEPT_UNSUPPORTED}
    for state in states - {ConceptRunState.SUCCEEDED}:
        reasons.add(_RUN_STATE_REASON.get(state, CoverageReason.CONCEPT_UNSUPPORTED))
    return False, reasons


def _compose(reasons: set[CoverageReason], detail: str | None) -> str:
    parts = [f"{code.value}: {detail if code == CoverageReason.RUBRIC_UNSUPPORTED and detail else _REASON_TEXT[code]}"
             for code in _ORDER if code in reasons]
    return "; ".join(parts)[:MAX_REASON_CHARS]


def derive_zone_coverage(
    *, run_id: UUID, zone: Zone, required_concepts: Sequence[Concept],
    expected_prompts: Mapping[Concept, Collection[str]], concept_runs: Sequence[ConceptRun],
    findings: Sequence[Finding], ambiguous: Sequence[AmbiguousOverlap], rubrics: RubricRegistry,
    quality_usable: bool, adjudicated_distinct: Collection[frozenset[UUID]] = (),
    verification_available: bool = True,
) -> tuple[Coverage, ...]:
    """Per (zone, concept) coverage for `ScoringInput.coverage`; one row per required concept.

    Assessed only when the rubric is enabled, every expected concept run succeeded, capture
    quality is usable, no candidate is unresolved and no overlap ambiguity is open. A zone
    with no required concepts yields no rows and is reported not_assessed by scoring.
    """
    if any(row.run_id != run_id or row.zone != zone for row in concept_runs):
        raise ValueError("concept run belongs to another run or zone")
    if any(row.run_id != run_id or row.zone != zone for row in findings):
        raise ValueError("finding belongs to another run or zone")
    open_overlaps = unresolved_overlaps(ambiguous, findings, adjudicated_distinct)
    rows: list[Coverage] = []
    for concept in sorted(set(required_concepts), key=lambda item: item.value):
        rubric_reason = rubrics.disabled_reason(concept)
        if rubric_reason is not None:
            rows.append(Coverage(run_id=run_id, zone=zone, concept=concept, state=CoverageState.NOT_ASSESSED,
                                 reason=_compose({CoverageReason.RUBRIC_UNSUPPORTED}, rubric_reason)))
            continue
        nothing_usable, reasons = _run_issues(concept, expected_prompts.get(concept, ()),
                                              [row for row in concept_runs if row.concept == concept])
        if not verification_available:
            reasons.add(CoverageReason.VERIFICATION_UNAVAILABLE)
        if not quality_usable:
            reasons.add(CoverageReason.CAPTURE_QUALITY_UNUSABLE)
        if any(row.concept == concept and row.status in (FindingStatus.CANDIDATE, FindingStatus.UNVERIFIED) for row in findings):
            reasons.add(CoverageReason.UNRESOLVED_CANDIDATES)
        if any(row.concept == concept for row in open_overlaps):
            reasons.add(CoverageReason.DUPLICATE_REVIEW_UNRESOLVED)
        if not reasons:
            rows.append(Coverage(run_id=run_id, zone=zone, concept=concept, state=CoverageState.ASSESSED))
            continue
        state = CoverageState.NOT_ASSESSED if nothing_usable else CoverageState.PARTIAL
        rows.append(Coverage(run_id=run_id, zone=zone, concept=concept, state=state, reason=_compose(reasons, None)))
    return tuple(rows)
