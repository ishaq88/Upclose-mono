"""Explicit local config loading, separated from the pure engines."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, model_validator

from app.contracts.base import ContractModel
from app.contracts.enums import ComponentId, Concept, RepairOperation, Zone

Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class HashedConfig(ContractModel):
    version: Annotated[str, Field(min_length=1, max_length=128)]

    @property
    def content_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ZoneConfig(ContractModel):
    zone: Zone
    weight: Positive
    required_concepts: tuple[Concept, ...]


class ConceptWeight(ContractModel):
    concept: Concept
    weight: Positive


class ScoringConfig(HashedConfig):
    zones: tuple[ZoneConfig, ...]
    concepts: tuple[ConceptWeight, ...]
    deduction_cap: Annotated[Decimal, Field(gt=0, le=100)]
    minimum_headline_zones: Annotated[int, Field(strict=True, ge=3, le=8)]

    @model_validator(mode="after")
    def complete_config(self) -> Self:
        if len(self.zones) != len(Zone) or {row.zone for row in self.zones} != set(Zone):
            raise ValueError("config requires each canonical zone exactly once")
        if len(self.concepts) != len(Concept) or {row.concept for row in self.concepts} != set(Concept):
            raise ValueError("config requires each canonical concept exactly once")
        if any(len(set(row.required_concepts)) != len(row.required_concepts) for row in self.zones):
            raise ValueError("duplicate required concept")
        return self


class CostRow(ContractModel):
    operation: RepairOperation
    components: tuple[ComponentId, ...]
    concepts: tuple[Concept, ...]
    low_dollars: Positive
    high_dollars: Positive

    @model_validator(mode="after")
    def coherent_row(self) -> Self:
        if not self.components or not self.concepts or self.high_dollars < self.low_dollars:
            raise ValueError("cost row requires mapping and ordered positive endpoints")
        if len(set(self.components)) != len(self.components) or len(set(self.concepts)) != len(self.concepts):
            raise ValueError("duplicate cost mapping")
        return self


class SeverityMultiplier(ContractModel):
    severity: Annotated[int, Field(strict=True, ge=1, le=5)]
    multiplier: Positive


class CostConfig(HashedConfig):
    rows: tuple[CostRow, ...]
    severity_multipliers: tuple[SeverityMultiplier, ...]
    blocked_flag_fragments: tuple[str, ...]

    @model_validator(mode="after")
    def coherent_config(self) -> Self:
        if len(self.severity_multipliers) != 5 or {row.severity for row in self.severity_multipliers} != set(range(1, 6)):
            raise ValueError("config requires severity multipliers 1..5 exactly once")
        mappings = [(component, concept) for row in self.rows for component in row.components for concept in row.concepts]
        if len(mappings) != len(set(mappings)):
            raise ValueError("ambiguous repair mapping")
        if not self.blocked_flag_fragments or any(not fragment for fragment in self.blocked_flag_fragments):
            raise ValueError("blocked flag fragments must be nonempty")
        return self


def load_scoring_config(path: str | Path) -> ScoringConfig:
    return ScoringConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_cost_config(path: str | Path) -> CostConfig:
    return CostConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))
