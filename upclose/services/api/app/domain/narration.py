"""Template narration of frozen results, plus validation of model narration.

Every number in template output (scores, counts, severities, dollar amounts)
is read from `ScoreResult`, `RepairSummary`, claim results or the verified
findings and formatted here; a model never does arithmetic. Two scopes exist:

- `narrate_part1` is the free summary. Its signature accepts no repair
  summary, claim results or other-zone scores, and it filters findings and
  coverage to the free zone before building any text.
- `narrate_full` is the paid narration.

No network, ORM or framework imports.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from string import Formatter
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from app.contracts.base import ContractModel
from app.contracts.enums import PART_ZONES, ClaimVerdict, Concept, CoverageState, Zone
from app.contracts.evidence import ClaimResult, Coverage, Finding, NarratedStatement
from app.contracts.scoring import RepairSummary, ScoreResult, ScoringInput, ZoneScore

from .claims import eligible_findings
from .config import HashedConfig
from .dedup import INTERNAL_FLAGS

Pattern = Annotated[str, Field(min_length=1, max_length=300)]


class NarrationScope(StrEnum):
    FREE = "free"
    PAID = "paid"


_NO_SLOTS: frozenset[str] = frozenset()
_ALLOWED_SLOTS: dict[str, frozenset[str]] = {
    "provisional_notice": _NO_SLOTS,
    "confidence_notice": _NO_SLOTS,
    "free_score": frozenset({"zone_label", "score"}),
    "free_zero": frozenset({"zone_label"}),
    "free_findings": frozenset({"zone_label", "count", "finding_noun", "max_severity"}),
    "free_unscored": frozenset({"zone_label", "gaps"}),
    "free_partial_note": _NO_SLOTS,
    "paid_headline": frozenset({"headline", "assessed_count"}),
    "paid_headline_none": frozenset({"assessed_count"}),
    "paid_flagged": frozenset({"count", "finding_noun"}),
    "paid_zone_scored": frozenset({"zone_label", "score"}),
    "paid_zone_zero": frozenset({"zone_label"}),
    "paid_zone_findings": frozenset({"zone_label", "count", "finding_noun", "max_severity"}),
    "paid_zone_unscored": frozenset({"zone_label", "coverage_state"}),
    "paid_zone_unscored_findings": frozenset({"zone_label", "coverage_state", "count", "finding_noun"}),
    "paid_finding": frozenset({"severity", "concept_label", "location", "flag_clause"}),
    "paid_costs_known": frozenset({"low", "high", "table_version"}),
    "paid_costs_subtotal": frozenset({"low", "high", "unestimated_count", "table_version"}),
    "paid_costs_none": frozenset({"unestimated_count", "table_version"}),
    "paid_costs_empty": _NO_SLOTS,
    "paid_claims_none": _NO_SLOTS,
    "paid_claims_summary": frozenset({"total", "contradicted", "supported", "not_visible"}),
    "paid_claim_contradicted": frozenset({"claim_number"}),
    "paid_claim_supported": frozenset({"claim_number"}),
    "paid_coverage_gaps": frozenset({"gaps"}),
}


class TemplateRow(ContractModel):
    key: Annotated[str, Field(min_length=1, max_length=64)]
    text: Annotated[str, Field(min_length=1, max_length=400)]


class ZoneLabel(ContractModel):
    zone: Zone
    label: Annotated[str, Field(min_length=1, max_length=64)]


class ConceptLabel(ContractModel):
    concept: Concept
    label: Annotated[str, Field(min_length=1, max_length=64)]


class NarrationConfig(HashedConfig):
    free_zone: Zone
    max_statements: Annotated[int, Field(strict=True, ge=1, le=500)]
    gaps_unrecorded_text: Annotated[str, Field(min_length=1, max_length=120)]
    zone_labels: tuple[ZoneLabel, ...]
    concept_labels: tuple[ConceptLabel, ...]
    templates: tuple[TemplateRow, ...]
    forbidden_phrases: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...]
    zero_finding_patterns: tuple[Pattern, ...]
    zero_finding_phrase: Annotated[str, Field(min_length=1, max_length=120)]
    free_forbidden_patterns: tuple[Pattern, ...]

    @model_validator(mode="after")
    def coherent_config(self) -> Self:
        if {row.zone for row in self.zone_labels} != set(Zone) or len(self.zone_labels) != len(Zone):
            raise ValueError("config requires one label per canonical zone")
        if {row.concept for row in self.concept_labels} != set(Concept) or len(self.concept_labels) != len(Concept):
            raise ValueError("config requires one label per canonical concept")
        keys = [row.key for row in self.templates]
        if len(set(keys)) != len(keys) or set(keys) != _ALLOWED_SLOTS.keys():
            raise ValueError("templates must define each known key exactly once")
        for row in self.templates:
            slots = {field for _, field, _, _ in Formatter().parse(row.text) if field is not None}
            if not slots <= _ALLOWED_SLOTS[row.key]:
                raise ValueError(f"template {row.key} uses unknown slots")
        for pattern in (*self.zero_finding_patterns, *self.free_forbidden_patterns):
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc
        phrase = self.zero_finding_phrase.casefold()
        if any(phrase not in self.template(key).casefold() for key in ("free_zero", "paid_zone_zero")):
            raise ValueError("zero_finding_phrase must appear in the zero-finding templates")
        return self

    def template(self, key: str) -> str:
        return next(row.text for row in self.templates if row.key == key)

    def zone_label(self, zone: Zone) -> str:
        return next(row.label for row in self.zone_labels if row.zone == zone)

    def concept_label(self, concept: Concept) -> str:
        return next(row.label for row in self.concept_labels if row.concept == concept)


def load_narration_config(path: str | Path) -> NarrationConfig:
    return NarrationConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


class NarrationInputError(ValueError):
    """Frozen inputs are inconsistent (for example a citation outside the evidence)."""


# --------------------------------------------------------------------------
# Formatting helpers (all numeric content originates here)
# --------------------------------------------------------------------------

def _score_text(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):.1f}"


def _dollars(cents: int) -> str:
    whole, remainder = divmod(cents, 100)
    return f"${whole:,}" if remainder == 0 else f"${whole:,}.{remainder:02d}"


def _noun(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _ids(findings: Sequence[Finding]) -> tuple[UUID, ...]:
    return tuple(sorted({row.id for row in findings}, key=str))


def _statement(config: NarrationConfig, key: str, ids: Sequence[UUID] = (), **slots: object) -> NarratedStatement:
    text = config.template(key).format(**slots)
    return NarratedStatement(text=text, finding_ids=tuple(sorted(set(ids), key=str)))


def _max_severity(findings: Sequence[Finding]) -> int:
    return max(row.severity or 0 for row in findings)


def _zone_row(score: ScoreResult, zone: Zone) -> ZoneScore | None:
    return next((row for row in score.zones if row.zone == zone), None)


def _state_word(state: CoverageState) -> str:
    return state.value.replace("_", " ")


def _coverage_gap_text(rows: Sequence[Coverage], config: NarrationConfig) -> str:
    gaps = sorted(f"{config.concept_label(row.concept)} ({_state_word(row.state)})" for row in rows
                  if row.state != CoverageState.ASSESSED)
    return ", ".join(gaps) or config.gaps_unrecorded_text


# --------------------------------------------------------------------------
# Free Part-1 summary
# --------------------------------------------------------------------------

def narrate_part1(inputs: ScoringInput, score: ScoreResult, config: NarrationConfig) -> tuple[NarratedStatement, ...]:
    """Free summary built from the free zone's evidence only.

    Other zones' findings, coverage and scores are never read; concept
    counts, costs, claims and the headline are not part of this scope.
    """
    zone = config.free_zone
    findings = [row for row in eligible_findings(inputs) if row.zone == zone]
    rows = [row for row in inputs.coverage if row.zone == zone]
    zone_score = _zone_row(score, zone)
    label = config.zone_label(zone)
    out: list[NarratedStatement] = []
    if zone_score is not None and zone_score.state == CoverageState.ASSESSED and zone_score.score is not None:
        out.append(_statement(config, "free_score", zone_label=label, score=_score_text(zone_score.score)))
        if findings:
            out.append(_free_findings(config, label, findings))
        else:
            out.append(_statement(config, "free_zero", zone_label=label))
    else:
        out.append(_statement(config, "free_unscored", zone_label=label, gaps=_coverage_gap_text(rows, config)))
        if findings:
            out.append(_free_findings(config, label, findings))
            out.append(_statement(config, "free_partial_note", ids=_ids(findings)))
    out.append(_statement(config, "provisional_notice"))
    if any(row.confidence is not None for row in findings):
        out.append(_statement(config, "confidence_notice", ids=_ids(findings)))
    return tuple(out)


def _free_findings(config: NarrationConfig, label: str, findings: Sequence[Finding]) -> NarratedStatement:
    return _statement(config, "free_findings", ids=_ids(findings), zone_label=label, count=len(findings),
                      finding_noun=_noun(len(findings), "finding is", "findings are"),
                      max_severity=_max_severity(findings))


def part1_summary_text(statements: Sequence[NarratedStatement]) -> str:
    """Single string for `FreeReport.part1_summary` (max 2000 characters)."""
    return " ".join(item.text for item in statements)[:2000]


# --------------------------------------------------------------------------
# Full paid narration
# --------------------------------------------------------------------------

def narrate_full(inputs: ScoringInput, score: ScoreResult, repairs: RepairSummary,
                 claims: Sequence[ClaimResult], config: NarrationConfig) -> tuple[NarratedStatement, ...]:
    """Paid narration from the frozen scoring, repair and claim results."""
    findings = eligible_findings(inputs)
    known = {row.id for row in findings}
    _check_citations(repairs, claims, known)
    out: list[NarratedStatement] = []
    out.extend(_headline(score, config))
    out.extend(_flagged(findings, config))
    for zone in PART_ZONES:
        out.extend(_zone_statements(zone, _zone_row(score, zone), [row for row in findings if row.zone == zone], config))
    out.extend(_costs(repairs, config))
    out.extend(_claims(claims, config))
    gaps = _paid_gaps(score, config)
    if gaps:
        out.append(_statement(config, "paid_coverage_gaps", gaps=gaps))
    out.append(_statement(config, "provisional_notice"))
    if any(row.confidence is not None for row in findings):
        out.append(_statement(config, "confidence_notice"))
    return tuple(out)


def _check_citations(repairs: RepairSummary, claims: Sequence[ClaimResult], known: set[UUID]) -> None:
    cited = {item for estimate in repairs.items for item in estimate.finding_ids}
    cited |= {item for claim in claims for item in claim.finding_ids}
    if not cited <= known:
        raise NarrationInputError("repair or claim cites a finding outside the frozen verified evidence")


def _headline(score: ScoreResult, config: NarrationConfig) -> list[NarratedStatement]:
    assessed = sum(row.score is not None for row in score.zones)
    if score.headline is None:
        return [_statement(config, "paid_headline_none", assessed_count=assessed)]
    return [_statement(config, "paid_headline", headline=_score_text(score.headline), assessed_count=assessed)]


def _risk_flagged(finding: Finding) -> bool:
    # Dedup bookkeeping flags are not risk signals.
    return any(flag not in INTERNAL_FLAGS for flag in finding.flags)


def _flagged(findings: Sequence[Finding], config: NarrationConfig) -> list[NarratedStatement]:
    flagged = [row for row in findings if _risk_flagged(row)]
    if not flagged:
        return []
    return [_statement(config, "paid_flagged", ids=_ids(flagged), count=len(flagged),
                       finding_noun=_noun(len(flagged), "finding is", "findings are"))]


def _zone_statements(zone: Zone, zone_score: ZoneScore | None, findings: Sequence[Finding],
                     config: NarrationConfig) -> list[NarratedStatement]:
    label = config.zone_label(zone)
    noun = _noun(len(findings), "finding", "findings")
    out: list[NarratedStatement] = []
    if zone_score is not None and zone_score.state == CoverageState.ASSESSED and zone_score.score is not None:
        out.append(_statement(config, "paid_zone_scored", zone_label=label, score=_score_text(zone_score.score)))
        if findings:
            out.append(_statement(config, "paid_zone_findings", ids=_ids(findings), zone_label=label, count=len(findings),
                                  finding_noun=noun, max_severity=_max_severity(findings)))
        else:
            out.append(_statement(config, "paid_zone_zero", zone_label=label))
    else:
        state = _state_word(zone_score.state if zone_score is not None else CoverageState.NOT_ASSESSED)
        if findings:
            out.append(_statement(config, "paid_zone_unscored_findings", ids=_ids(findings), zone_label=label,
                                  coverage_state=state, count=len(findings), finding_noun=noun))
        else:
            out.append(_statement(config, "paid_zone_unscored", zone_label=label, coverage_state=state))
    for row in sorted(findings, key=lambda item: (-(item.severity or 0), str(item.id))):
        location = label if row.component_id is None else f"{label} ({row.component_id.value.replace('_', ' ')})"
        out.append(_statement(config, "paid_finding", ids=(row.id,), severity=row.severity,
                              concept_label=config.concept_label(row.concept), location=location,
                              flag_clause=", flagged as high risk" if _risk_flagged(row) else ""))
    return out


def _costs(repairs: RepairSummary, config: NarrationConfig) -> list[NarratedStatement]:
    if not repairs.items:
        return [_statement(config, "paid_costs_empty")]
    ids = [item for estimate in repairs.items for item in estimate.finding_ids]
    unknown = repairs.unestimated_count
    if unknown == len(repairs.items):
        return [_statement(config, "paid_costs_none", ids=ids, unestimated_count=unknown,
                           table_version=repairs.table_version)]
    low, high = _dollars(repairs.subtotal_low_cents), _dollars(repairs.subtotal_high_cents)
    if unknown:
        return [_statement(config, "paid_costs_subtotal", ids=ids, low=low, high=high, unestimated_count=unknown,
                           table_version=repairs.table_version)]
    return [_statement(config, "paid_costs_known", ids=ids, low=low, high=high, table_version=repairs.table_version)]


def _claims(claims: Sequence[ClaimResult], config: NarrationConfig) -> list[NarratedStatement]:
    if not claims:
        return [_statement(config, "paid_claims_none")]
    ordered = sorted(claims, key=lambda item: item.claim_index)
    count = {verdict: sum(item.verdict == verdict for item in ordered) for verdict in ClaimVerdict}
    out = [_statement(config, "paid_claims_summary", total=len(ordered), contradicted=count[ClaimVerdict.CONTRADICTED],
                      supported=count[ClaimVerdict.SUPPORTED], not_visible=count[ClaimVerdict.NOT_VISIBLE])]
    for item in ordered:
        if item.verdict == ClaimVerdict.CONTRADICTED:
            out.append(_statement(config, "paid_claim_contradicted", ids=item.finding_ids, claim_number=item.claim_index + 1))
        elif item.verdict == ClaimVerdict.SUPPORTED:
            out.append(_statement(config, "paid_claim_supported", ids=item.finding_ids, claim_number=item.claim_index + 1))
    return out


def _paid_gaps(score: ScoreResult, config: NarrationConfig) -> str:
    present = {row.zone: row for row in score.zones}
    parts = []
    for zone in PART_ZONES:
        row = present.get(zone)
        state = row.state if row is not None else CoverageState.NOT_ASSESSED
        if state != CoverageState.ASSESSED or (row is not None and row.score is None):
            parts.append(f"{config.zone_label(zone)} ({_state_word(state)})")
    return ", ".join(parts)


# --------------------------------------------------------------------------
# Hashing, validation of model narration, fallback
# --------------------------------------------------------------------------

def narration_hash(statements: Sequence[NarratedStatement]) -> str:
    payload = [item.model_dump(mode="json") for item in statements]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class NarrationValidationError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class NarrationOutcome(ContractModel):
    scope: NarrationScope
    statements: tuple[NarratedStatement, ...]
    source: Literal["template", "model", "fallback"]
    errors: tuple[str, ...]
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    statements_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LINK = re.compile(r"(?:https?://|www\.|ftp://)", re.IGNORECASE)


def _numbers(text: str) -> set[str]:
    return {token.rstrip(",") for token in _NUMBER.findall(text)}


def _parse_statements(raw: object, limit: int) -> list[NarratedStatement]:
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise NarrationValidationError(["model narration must be a list"])
    if not 0 < len(raw) <= limit:
        raise NarrationValidationError(["model narration statement count is out of bounds"])
    parsed: list[NarratedStatement] = []
    errors: list[str] = []
    for position, item in enumerate(raw):
        try:
            parsed.append(item if isinstance(item, NarratedStatement) else NarratedStatement.model_validate(item))
        except ValidationError as exc:
            errors.append(f"statement {position} is malformed: {len(exc.errors())} schema error(s)")
    if errors:
        raise NarrationValidationError(errors)
    return parsed


def _statement_errors(position: int, item: NarratedStatement, allowed_ids: frozenset[UUID], allowed_numbers: set[str],
                      scope: NarrationScope, config: NarrationConfig) -> list[str]:
    text, lowered = item.text, item.text.casefold()
    errors: list[str] = []
    if not set(item.finding_ids) <= allowed_ids:
        errors.append(f"statement {position} cites a finding outside the allowed evidence scope")
    if _LINK.search(text) or any(ord(ch) < 32 for ch in text):
        errors.append(f"statement {position} contains a link or control character")
    if not _numbers(text) <= allowed_numbers:
        errors.append(f"statement {position} contains a number not produced by the templates")
    if any(phrase in lowered for phrase in config.forbidden_phrases):
        errors.append(f"statement {position} uses forbidden assurance wording")
    if "confidence" in lowered and "uncalibrated" not in lowered:
        errors.append(f"statement {position} mentions confidence without the uncalibrated label")
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in config.zero_finding_patterns) \
            and config.zero_finding_phrase.casefold() not in lowered:
        errors.append(f"statement {position} states an absence without the required zero-finding wording")
    if scope == NarrationScope.FREE and any(re.search(pattern, text, re.IGNORECASE) for pattern in config.free_forbidden_patterns):
        errors.append(f"statement {position} mentions content outside the free Part-1 scope")
    return errors


def validate_model_narration(raw: object, *, scope: NarrationScope, allowed_ids: frozenset[UUID],
                             template_statements: Sequence[NarratedStatement],
                             config: NarrationConfig) -> tuple[NarratedStatement, ...]:
    """Validate untrusted model narration or raise `NarrationValidationError` (all-or-nothing).

    Citations must be inside `allowed_ids` (scope-filtered verified evidence);
    every number must already appear in the template rendering of the same
    frozen inputs; no links, assurance wording ("damage free"), unlabeled
    confidence or unqualified absence statements; the free scope additionally
    forbids costs, claims, headline and other zones.
    """
    parsed = _parse_statements(raw, config.max_statements)
    allowed_numbers: set[str] = set()
    for item in template_statements:
        allowed_numbers |= _numbers(item.text)
    errors: list[str] = []
    for position, item in enumerate(parsed):
        errors.extend(_statement_errors(position, item, allowed_ids, allowed_numbers, scope, config))
    if errors:
        raise NarrationValidationError(errors)
    return tuple(parsed)


def _outcome(scope: NarrationScope, statements: Sequence[NarratedStatement], source: Literal["template", "model", "fallback"],
             errors: Sequence[str], config: NarrationConfig) -> NarrationOutcome:
    return NarrationOutcome(scope=scope, statements=tuple(statements), source=source, errors=tuple(errors),
                            config_hash=config.content_hash, statements_hash=narration_hash(statements))


def _resolve(scope: NarrationScope, template: tuple[NarratedStatement, ...], allowed_ids: frozenset[UUID],
             model_output: object | None, config: NarrationConfig) -> NarrationOutcome:
    if model_output is None:
        return _outcome(scope, template, "template", (), config)
    try:
        validated = validate_model_narration(model_output, scope=scope, allowed_ids=allowed_ids,
                                             template_statements=template, config=config)
    except NarrationValidationError as exc:
        return _outcome(scope, template, "fallback", exc.errors, config)
    return _outcome(scope, validated, "model", (), config)


def resolve_part1_narration(inputs: ScoringInput, score: ScoreResult, config: NarrationConfig,
                            model_output: object | None = None) -> NarrationOutcome:
    """Free summary: validated model text if supplied and clean, else the template text."""
    template = narrate_part1(inputs, score, config)
    allowed = frozenset(row.id for row in eligible_findings(inputs) if row.zone == config.free_zone)
    return _resolve(NarrationScope.FREE, template, allowed, model_output, config)


def resolve_full_narration(inputs: ScoringInput, score: ScoreResult, repairs: RepairSummary,
                           claims: Sequence[ClaimResult], config: NarrationConfig,
                           model_output: object | None = None) -> NarrationOutcome:
    """Paid narration: validated model text if supplied and clean, else the template text."""
    template = narrate_full(inputs, score, repairs, claims, config)
    allowed = frozenset(row.id for row in eligible_findings(inputs))
    return _resolve(NarrationScope.PAID, template, allowed, model_output, config)
