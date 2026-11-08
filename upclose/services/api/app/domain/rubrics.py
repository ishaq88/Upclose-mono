"""Versioned assessment design configs: segmentation prompt plan and severity rubrics.

Pure loading and validation only. Everything here is an uncalibrated design default
subject to evaluation; nothing is a claim about provider capability, limits or price.
A concept without complete, valid severity anchors is disabled and reported through
coverage, never scored as clean.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.contracts.base import ContractModel, PartNumber
from app.contracts.enums import PART_ZONES, Concept, Zone

from .config import HashedConfig, ScoringConfig
from .dedup import DedupConfig

RUBRIC_UNSUPPORTED = "rubric_unsupported"
SEVERITIES = (1, 2, 3, 4, 5)
MAX_REASON_CHARS = 240
EXTENT_INSTRUCTION = ("Use the full frame to judge how much of the panel the damage covers; choose the highest "
                      "severity whose extent or character the visible damage meets.")

PromptId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
PromptText = Annotated[str, Field(min_length=1, max_length=48, pattern=r"^[a-z][a-z ]*$")]


# ---------------------------------------------------------------- prompt plan

class PromptEntry(ContractModel):
    prompt_id: PromptId
    concept: Concept | None
    text: PromptText  # short noun phrase, not an instruction

    @model_validator(mode="after")
    def mechanical_prompt(self) -> Self:
        if self.concept is None and self.prompt_id != "dash_cluster":
            raise ValueError("only the dashboard localization prompt has no visual concept")
        return self


class PartPlan(ContractModel):
    part_no: PartNumber
    zone: Zone
    disposition: Literal["planned", "deferred", "no_concept"]
    media: Literal["video", "stills"]
    still_count: Annotated[int, Field(strict=True, ge=1, le=64)] | None = None
    prompt_ids: tuple[PromptId, ...]
    reason: Annotated[str, Field(min_length=1, max_length=MAX_REASON_CHARS)] | None = None

    @model_validator(mode="after")
    def coherent_plan(self) -> Self:
        if self.zone != PART_ZONES[self.part_no - 1]:
            raise ValueError("part number and zone disagree")
        if len(set(self.prompt_ids)) != len(self.prompt_ids):
            raise ValueError("duplicate prompt in part plan")
        if self.disposition == "planned":
            if not self.prompt_ids:
                raise ValueError("planned part requires prompts")
            if (self.media == "stills") != (self.still_count is not None):
                raise ValueError("planned stills require a still count; video forbids one")
        elif self.prompt_ids or not self.reason:
            raise ValueError("deferred or conceptless part requires a reason and no prompts")
        if self.media == "video" and self.still_count is not None:
            raise ValueError("still count applies only to stills")
        return self


class PromptConfig(HashedConfig):
    status: Literal["design_config_subject_to_evaluation"]
    note: Annotated[str, Field(min_length=1, max_length=1000)]
    prompts: tuple[PromptEntry, ...]
    parts: tuple[PartPlan, ...]
    dedup: DedupConfig

    @model_validator(mode="after")
    def complete_plan(self) -> Self:
        ids = [row.prompt_id for row in self.prompts]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate prompt id")
        if [row.part_no for row in self.parts] != list(range(1, 9)):
            raise ValueError("plan requires parts 1..8 once, in order")
        if any(prompt_id not in set(ids) for row in self.parts for prompt_id in row.prompt_ids):
            raise ValueError("part plan references unknown prompt")
        if any((prompt_id == "dash_cluster") != (row.part_no == 5)
               for row in self.parts for prompt_id in row.prompt_ids):
            raise ValueError("dashboard localization is separate from visual damage prompts")
        return self

    def part(self, part_no: int) -> PartPlan:
        return self.parts[part_no - 1]

    def prompt(self, prompt_id: str) -> PromptEntry:
        return next(row for row in self.prompts if row.prompt_id == prompt_id)

    def expected_prompts(self, part_no: int) -> dict[Concept, tuple[str, ...]]:
        """Planned prompt ids by concept; empty for deferred or conceptless parts."""
        result: dict[Concept, list[str]] = {}
        for prompt_id in self.part(part_no).prompt_ids:
            concept = self.prompt(prompt_id).concept
            if concept is not None:
                result.setdefault(concept, []).append(prompt_id)
        return {concept: tuple(sorted(ids)) for concept, ids in sorted(result.items())}


def load_prompt_config(path: str | Path) -> PromptConfig:
    return PromptConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


def validate_plan_against_scoring(prompts: PromptConfig, scoring: ScoringConfig) -> None:
    """Planned concepts per zone must equal the scoring config's required concepts."""
    required = {row.zone: set(row.required_concepts) for row in scoring.zones}
    for plan in prompts.parts:
        if set(prompts.expected_prompts(plan.part_no)) != required[plan.zone]:
            raise ValueError(f"prompt plan and scoring requirements disagree for {plan.zone.value}")


# -------------------------------------------------------------------- rubrics

class RubricAnchor(ContractModel):
    # Deliberately permissive: an invalid anchor disables its concept, not the whole file.
    severity: int
    description: str


class ConceptRubricEntry(ContractModel):
    concept: Concept
    limitations: str
    suggested_flags: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = ()
    anchors: tuple[RubricAnchor, ...]


class RubricConfig(HashedConfig):
    status: Literal["uncalibrated_design_default"]
    mechanic_review_required: Literal[True]
    statement: Annotated[str, Field(min_length=1, max_length=1000)]
    forbidden_patterns: tuple[str, ...]
    concepts: tuple[ConceptRubricEntry, ...]

    @model_validator(mode="after")
    def compilable_patterns(self) -> Self:
        if not self.forbidden_patterns:
            raise ValueError("forbidden patterns must not be empty")
        for pattern in self.forbidden_patterns:
            re.compile(pattern)
        return self


class EnabledRubric(ContractModel):
    concept: Concept
    rubric_id: str
    rubric_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    anchors: tuple[RubricAnchor, ...]  # sorted by severity, exactly 1..5
    limitations: str
    suggested_flags: tuple[str, ...]


class DisabledRubric(ContractModel):
    concept: Concept
    reason: Annotated[str, Field(min_length=1, max_length=MAX_REASON_CHARS)]


class RubricRegistry(ContractModel):
    version: str
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    status: Literal["uncalibrated_design_default"]
    enabled: tuple[EnabledRubric, ...]
    disabled: tuple[DisabledRubric, ...]

    @model_validator(mode="after")
    def partition(self) -> Self:
        concepts = [row.concept for row in self.enabled] + [row.concept for row in self.disabled]
        if len(concepts) != len(set(concepts)) or set(concepts) != set(Concept):
            raise ValueError("every concept must be exactly one of enabled or disabled")
        return self

    def get(self, concept: Concept) -> EnabledRubric | None:
        return next((row for row in self.enabled if row.concept == concept), None)

    def is_enabled(self, concept: Concept) -> bool:
        return self.get(concept) is not None

    def disabled_reason(self, concept: Concept) -> str | None:
        row = next((row for row in self.disabled if row.concept == concept), None)
        return None if row is None else row.reason


def _entry_problem(entry: ConceptRubricEntry, forbidden: Sequence[re.Pattern[str]]) -> str | None:
    """Return why the entry cannot be used, or None when anchors are complete and visible-only."""
    if sorted(row.severity for row in entry.anchors) != list(SEVERITIES):
        return "anchors must cover severities 1..5 exactly once"
    texts = [row.description for row in entry.anchors] + [entry.limitations]
    if any(len(text.strip()) < 12 for text in texts):
        return "anchor or limitation text is empty or too short"
    if any(pattern.search(text) for pattern in forbidden for text in texts):
        return "anchor text makes a measurement, diagnosis or certification claim"
    return None


def _concept_hash(entry: ConceptRubricEntry) -> str:
    canonical = json.dumps(entry.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _disabled(concept: Concept, detail: str) -> DisabledRubric:
    return DisabledRubric(concept=concept, reason=f"{RUBRIC_UNSUPPORTED}: {detail}"[:MAX_REASON_CHARS])


def build_rubric_registry(config: RubricConfig) -> RubricRegistry:
    """Enable only concepts with complete valid anchors; disable the rest with a reason."""
    forbidden = [re.compile(pattern, re.IGNORECASE) for pattern in config.forbidden_patterns]
    enabled: list[EnabledRubric] = []
    disabled: list[DisabledRubric] = []
    for concept in sorted(Concept, key=lambda item: item.value):
        entries = [row for row in config.concepts if row.concept == concept]
        if not entries:
            disabled.append(_disabled(concept, "no rubric defined for concept"))
        elif len(entries) > 1:
            disabled.append(_disabled(concept, "ambiguous duplicate rubric entries"))
        elif (problem := _entry_problem(entries[0], forbidden)) is not None:
            disabled.append(_disabled(concept, problem))
        else:
            entry = entries[0]
            enabled.append(EnabledRubric(
                concept=concept, rubric_id=f"{config.version}:{concept.value}", rubric_hash=_concept_hash(entry),
                anchors=tuple(sorted(entry.anchors, key=lambda row: row.severity)),
                limitations=entry.limitations, suggested_flags=entry.suggested_flags))
    return RubricRegistry(version=config.version, config_hash=config.content_hash, status=config.status,
                          enabled=tuple(enabled), disabled=tuple(disabled))


def load_rubric_config(path: str | Path) -> RubricConfig:
    return RubricConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_rubric_registry(path: str | Path) -> RubricRegistry:
    return build_rubric_registry(load_rubric_config(path))


def render_rubric_text(rubric: EnabledRubric) -> str:
    """Stable text handed to a Verifier as its `rubric` argument."""
    lines = [f"Rubric {rubric.rubric_id} (uncalibrated design default; describes visible evidence only)"]
    lines.append(EXTENT_INSTRUCTION)
    lines += [f"Severity {row.severity}: {row.description}" for row in rubric.anchors]
    lines.append(f"Limitations: {rubric.limitations}")
    return "\n".join(lines)


# ------------------------------------------------------------ part planning

class PartPromptPlan(ContractModel):
    runnable: tuple[PromptEntry, ...]
    disabled_concepts: tuple[DisabledRubric, ...]


def plan_part_prompts(prompts: PromptConfig, rubrics: RubricRegistry, part_no: int) -> PartPromptPlan:
    """Prompts the pipeline may run for a part; rubric-disabled concepts are reported, not run."""
    runnable: list[PromptEntry] = []
    disabled: list[DisabledRubric] = []
    for concept, prompt_ids in prompts.expected_prompts(part_no).items():
        reason = rubrics.disabled_reason(concept)
        if reason is None:
            runnable += [prompts.prompt(prompt_id) for prompt_id in prompt_ids]
        else:
            disabled.append(DisabledRubric(concept=concept, reason=reason))
    return PartPromptPlan(runnable=tuple(runnable), disabled_concepts=tuple(disabled))
