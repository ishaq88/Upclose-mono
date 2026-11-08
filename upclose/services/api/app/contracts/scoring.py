from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from .base import Cents, ContractModel, Count, Score, Severity
from .enums import ComponentId, CoverageState, RepairOperation, Zone
from .evidence import Coverage, Finding


class ScoringInput(ContractModel):
    session_id: UUID
    run_ids: frozenset[UUID]
    findings: tuple[Finding, ...]
    coverage: tuple[Coverage, ...]

    @model_validator(mode="after")
    def same_frozen_input(self) -> Self:
        findings = {finding.id: finding for finding in self.findings}
        if len(findings) != len(self.findings):
            raise ValueError("duplicate finding ID")
        for finding in self.findings:
            if finding.session_id != self.session_id or finding.run_id not in self.run_ids:
                raise ValueError("finding belongs to foreign session or run")
            if finding.duplicate_of is not None:
                target = findings.get(finding.duplicate_of)
                if target is None or target.part_id != finding.part_id:
                    raise ValueError("duplicate target must belong to same part/input")
        coverage_keys = [(item.run_id, item.zone, item.concept) for item in self.coverage]
        if len(set(coverage_keys)) != len(coverage_keys):
            raise ValueError("duplicate coverage")
        if any(item.run_id not in self.run_ids for item in self.coverage):
            raise ValueError("coverage belongs to foreign run")
        return self


class ZoneScore(ContractModel):
    zone: Zone
    state: CoverageState
    score: Score | None
    reason: Annotated[str, Field(min_length=1, max_length=240)] | None = None

    @model_validator(mode="after")
    def assessed_score_only(self) -> Self:
        if (self.state == CoverageState.ASSESSED) != (self.score is not None):
            raise ValueError("only assessed zones have numeric scores")
        return self


class ScoreResult(ContractModel):
    headline: Score | None
    zones: tuple[ZoneScore, ...]
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    label: str = "Provisional visual-condition index"
    # Set only when the headline counted unassessed scorable zones at this value. Absent on results written
    # before it existed, which therefore serialise to exactly the same bytes.
    unassessed_zone_score: Score | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def unique_zones_and_headline(self) -> Self:
        if len({zone.zone for zone in self.zones}) != len(self.zones):
            raise ValueError("duplicate score zone")
        if (self.headline is not None and self.unassessed_zone_score is None
                and sum(zone.score is not None for zone in self.zones) < 3):
            raise ValueError("headline requires at least three assessed zones")
        return self


class RepairEstimate(ContractModel):
    operation: RepairOperation | None
    component_id: ComponentId | None
    severity: Severity
    finding_ids: tuple[UUID, ...]
    low_cents: Cents | None
    high_cents: Cents | None
    reason: Annotated[str, Field(min_length=1, max_length=240)] | None

    @model_validator(mode="after")
    def known_estimate(self) -> Self:
        if (self.low_cents is None) != (self.high_cents is None):
            raise ValueError("estimate endpoints must both be known or null")
        if self.low_cents is None:
            if not self.reason:
                raise ValueError("unknown estimate requires reason")
        elif self.operation is None or self.component_id is None or self.high_cents < self.low_cents:
            raise ValueError("numeric estimate requires component, operation, ordered endpoints")
        return self


class RepairSummary(ContractModel):
    items: tuple[RepairEstimate, ...]
    subtotal_low_cents: Cents
    subtotal_high_cents: Cents
    unestimated_count: Count
    table_version: Annotated[str, Field(min_length=1, max_length=128)]
    label: str = "Local planning estimate; not a current shop quote"
