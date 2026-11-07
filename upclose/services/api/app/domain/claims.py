"""Deterministic listing-claim extraction, adjudication and validation.

Listing text is untrusted data. It is normalized, bounded, split and classified
with fixed regular expressions only; no stage here interprets it as an
instruction, fetches a URL or calls a tool. Verdicts come from frozen evidence
(`ScoringInput`) and versioned config, never from the listing's own wording.
No network, ORM or framework imports.
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from app.contracts.base import ContractModel, Count
from app.contracts.enums import PART_ZONES, ClaimVerdict, Concept, CoverageState, FindingStatus, Zone
from app.contracts.evidence import ClaimGroup, ClaimResult, Finding
from app.contracts.scoring import ScoringInput

from .config import HashedConfig

Pattern = Annotated[str, Field(min_length=1, max_length=300)]


class ClaimTopic(StrEnum):
    # Visible topics share their value with the Concept they are judged by.
    RUST = "rust"
    PAINT_BUBBLING = "paint_bubbling"
    DENT = "dent"
    SCRATCH = "scratch"
    GLASS_CRACK = "glass_crack"
    TIRE_WEAR = "tire_wear"
    FLUID_LEAK = "fluid_leak"
    HISTORY = "history"
    MILEAGE = "mileage"
    NEWNESS = "newness"
    MECHANICAL = "mechanical"
    AIR_CONDITIONING = "air_conditioning"
    BRAKES = "brakes"
    STRUCTURE = "structure"
    OTHER = "other"


class TopicKind(StrEnum):
    VISIBLE_DEFECT = "visible_defect"
    VISIBLE_CONDITION = "visible_condition"
    NOT_VISIBLE = "not_visible"


class ClaimPolarity(StrEnum):
    ABSENCE = "absence"      # "no rust", "rust free"
    PRESENCE = "presence"    # "some surface rust", "scratch on door"
    QUALITY = "quality"      # "good tires"
    NEUTRAL = "neutral"


class TopicRule(ContractModel):
    topic: ClaimTopic
    kind: TopicKind
    patterns: Annotated[tuple[Pattern, ...], Field(min_length=1)]
    requires_any: tuple[Pattern, ...] = ()
    defect_patterns: tuple[Pattern, ...] = ()
    zones: tuple[Zone, ...] = ()
    overrides_visible: bool = False
    rationale: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def coherent_rule(self) -> Self:
        if self.topic == ClaimTopic.OTHER:
            raise ValueError("other is implicit, not a configurable rule")
        _check_patterns((*self.patterns, *self.requires_any, *self.defect_patterns))
        if self.kind == TopicKind.NOT_VISIBLE:
            if not self.rationale:
                raise ValueError("not_visible topic requires a rationale")
            if self.zones or self.defect_patterns:
                raise ValueError("not_visible topic has no zones or defect patterns")
            return self
        if self.topic.value not in {concept.value for concept in Concept} or not self.zones:
            raise ValueError("visible topic requires a matching concept and zones")
        if self.overrides_visible or self.rationale:
            raise ValueError("visible topic cannot override or carry a fixed rationale")
        if self.kind == TopicKind.VISIBLE_CONDITION and not self.defect_patterns:
            raise ValueError("visible_condition topic requires defect patterns")
        return self

    @property
    def concept(self) -> Concept:
        return Concept(self.topic.value)


class LocationRule(ContractModel):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    zones: Annotated[tuple[Zone, ...], Field(min_length=1)]
    patterns: Annotated[tuple[Pattern, ...], Field(min_length=1)]
    side: bool = False
    side_dependent: bool = False

    @model_validator(mode="after")
    def coherent_location(self) -> Self:
        _check_patterns(self.patterns)
        if self.side and self.side_dependent:
            raise ValueError("a location cannot be both a side and side-dependent")
        return self


class Redaction(ContractModel):
    pattern: Pattern
    replacement: Annotated[str, Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def compiles(self) -> Self:
        _check_patterns((self.pattern,))
        return self


_RATIONALE_SLOTS = frozenset({"concept_label", "count", "coverage_clause"})


class Rationales(ContractModel):
    contradicted: str
    supported: str
    absence_unverified: str
    coverage_assessed: str
    coverage_incomplete: str
    presence_unmatched: str
    presence_partial: str
    minimizer: str
    location_unresolved: str
    neutral_visible: str
    other: str

    @model_validator(mode="after")
    def valid_templates(self) -> Self:
        for name, text in self.model_dump().items():
            if not text or len(text) > 500:
                raise ValueError(f"rationale {name} must be 1..500 characters")
            slots = {field for _, field, _, _ in Formatter().parse(text) if field is not None}
            if not slots <= _RATIONALE_SLOTS:
                raise ValueError(f"rationale {name} uses unknown slots")
        return self


class ClaimsConfig(HashedConfig):
    max_listing_chars: Annotated[int, Field(strict=True, ge=1, le=10000)]
    max_claims: Annotated[int, Field(strict=True, ge=1, le=200)]
    max_claim_chars: Annotated[int, Field(strict=True, ge=10, le=1000)]
    min_claim_chars: Annotated[int, Field(strict=True, ge=1, le=20)]
    max_cited_findings: Annotated[int, Field(strict=True, ge=1, le=50)]
    quality_contradiction_min_severity: Annotated[int, Field(strict=True, ge=1, le=5)]
    minimizer_max_severity: Annotated[int, Field(strict=True, ge=1, le=5)]
    no_listing_note: Annotated[str, Field(min_length=1, max_length=200)]
    truncated_note: Annotated[str, Field(min_length=1, max_length=200)]
    redactions: tuple[Redaction, ...]
    sentence_split_pattern: Pattern
    fragment_split_pattern: Pattern
    absence_patterns: Annotated[tuple[Pattern, ...], Field(min_length=1)]
    quality_patterns: tuple[Pattern, ...]
    minimizer_patterns: tuple[Pattern, ...]
    topics: Annotated[tuple[TopicRule, ...], Field(min_length=1)]
    locations: tuple[LocationRule, ...]
    rationales: Rationales

    @model_validator(mode="after")
    def coherent_config(self) -> Self:
        _check_patterns((self.sentence_split_pattern, self.fragment_split_pattern, *self.absence_patterns,
                         *self.quality_patterns, *self.minimizer_patterns))
        topics = [rule.topic for rule in self.topics]
        if len(set(topics)) != len(topics):
            raise ValueError("duplicate topic rule")
        if len({rule.name for rule in self.locations}) != len(self.locations):
            raise ValueError("duplicate location rule")
        return self


def _check_patterns(patterns: Sequence[str]) -> None:
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc


@lru_cache(maxsize=None)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _any(patterns: Sequence[str], text: str) -> bool:
    return any(_rx(pattern).search(text) for pattern in patterns)


def load_claims_config(path: str | Path) -> ClaimsConfig:
    return ClaimsConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

class ClaimClassification(ContractModel):
    topics: tuple[ClaimTopic, ...]
    polarity: ClaimPolarity
    zones: tuple[Zone, ...]
    minimizing: bool


class ListingClaim(ContractModel):
    claim_index: Count
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    topics: tuple[ClaimTopic, ...]
    polarity: ClaimPolarity


class ListingExtraction(ContractModel):
    claims: tuple[ListingClaim, ...]
    notes: tuple[str, ...]


_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_STRIP_CHARS = " \t-*•:–—\"'"
_URL_IN_MODEL_TEXT = re.compile(r"(?:https?://|www\.|ftp://)", re.IGNORECASE)


def normalize_listing_text(text: str, config: ClaimsConfig) -> str:
    """NFKC-fold, drop control/format characters, bound length, redact links."""
    folded = unicodedata.normalize("NFKC", text[: config.max_listing_chars * 2]).replace("\r", "\n")
    kept = "".join(ch for ch in folded if ch == "\n" or ch == "\t" or unicodedata.category(ch) not in _DROPPED_CATEGORIES)
    bounded = kept.replace("\t", " ")[: config.max_listing_chars]
    for rule in config.redactions:
        bounded = _rx(rule.pattern).sub(rule.replacement, bounded)
    return bounded


def listing_hash(listing_text: str | None, config: ClaimsConfig) -> str:
    """Hash of the normalized listing; absent text hashes as the empty string."""
    normalized = normalize_listing_text(listing_text, config) if listing_text else ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _matched_rules(text: str, config: ClaimsConfig) -> tuple[TopicRule, ...]:
    matched: list[TopicRule] = []
    for rule in config.topics:
        if _any(rule.patterns, text) and (not rule.requires_any or _any(rule.requires_any, text)):
            matched.append(rule)
    return tuple(matched)


def _location_zones(text: str, config: ClaimsConfig) -> tuple[Zone, ...]:
    hits = [rule for rule in config.locations if _any(rule.patterns, text)]
    sides = {zone for rule in hits if rule.side for zone in rule.zones}
    zones: set[Zone] = set()
    for rule in hits:
        if rule.side_dependent and sides:
            zones |= {zone for zone in rule.zones if zone in sides}
        else:
            zones |= set(rule.zones)
    return tuple(zone for zone in PART_ZONES if zone in zones)


def _polarity(text: str, rules: Sequence[TopicRule], config: ClaimsConfig) -> ClaimPolarity:
    visible = [rule for rule in rules if rule.kind != TopicKind.NOT_VISIBLE]
    if not visible:
        return ClaimPolarity.NEUTRAL
    if _any(config.absence_patterns, text):
        return ClaimPolarity.ABSENCE
    conditions = [rule for rule in visible if rule.kind == TopicKind.VISIBLE_CONDITION]
    if any(_any(rule.defect_patterns, text) for rule in conditions):
        return ClaimPolarity.PRESENCE
    if any(rule.kind == TopicKind.VISIBLE_DEFECT for rule in visible):
        return ClaimPolarity.PRESENCE
    if conditions and _any(config.quality_patterns, text):
        return ClaimPolarity.QUALITY
    return ClaimPolarity.NEUTRAL


def classify_claim(text: str, config: ClaimsConfig) -> ClaimClassification:
    """Pure topic/polarity/location classification of one claim string."""
    rules = _matched_rules(text, config)
    topics = tuple(rule.topic for rule in rules) or (ClaimTopic.OTHER,)
    return ClaimClassification(topics=topics, polarity=_polarity(text, rules, config),
                               zones=_location_zones(text, config), minimizing=_any(config.minimizer_patterns, text))


def _clean_fragment(fragment: str, config: ClaimsConfig) -> str | None:
    cleaned = " ".join(fragment.split()).strip(_STRIP_CHARS)[: config.max_claim_chars].rstrip(_STRIP_CHARS)
    return cleaned if sum(ch.isalnum() for ch in cleaned) >= config.min_claim_chars else None


def _is_topical(text: str, config: ClaimsConfig) -> bool:
    return bool(_matched_rules(text, config))


def _merge_fragments(sentence: str, config: ClaimsConfig) -> list[str]:
    """Split a sentence on commas; a topic-less lead-in joins the next topical fragment."""
    fragments = [item for item in (_clean_fragment(part, config) for part in _rx(config.fragment_split_pattern).split(sentence)) if item]
    merged: list[str] = []
    index = 0
    while index < len(fragments):
        current = fragments[index]
        following = fragments[index + 1] if index + 1 < len(fragments) else None
        if following is not None and not _is_topical(current, config) and _is_topical(following, config):
            joined = _clean_fragment(f"{current}, {following}", config)
            if joined:
                merged.append(joined)
            index += 2
            continue
        merged.append(current)
        index += 1
    return merged


def extract_listing_claims(listing_text: str | None, config: ClaimsConfig) -> ListingExtraction:
    """Split untrusted listing text into bounded, indexed candidate claims.

    Indices follow order of appearance in the normalized listing, so identical
    text and config always yield identical claims. Statements with no known
    topic are kept as `other` (adjudicated not_visible), never silently dropped.
    """
    if listing_text is None or not listing_text.strip():
        return ListingExtraction(claims=(), notes=(config.no_listing_note,))
    text = normalize_listing_text(listing_text, config)
    claims: list[ListingClaim] = []
    seen: set[str] = set()
    truncated = False
    for sentence in _rx(config.sentence_split_pattern).split(text):
        for fragment in _merge_fragments(sentence, config):
            key = fragment.casefold()
            if key in seen:
                continue
            if len(claims) >= config.max_claims:
                truncated = True
                break
            seen.add(key)
            classification = classify_claim(fragment, config)
            claims.append(ListingClaim(claim_index=len(claims), text=fragment, topics=classification.topics,
                                       polarity=classification.polarity))
        if truncated:
            break
    if not claims:
        return ListingExtraction(claims=(), notes=(config.no_listing_note,))
    return ListingExtraction(claims=tuple(claims), notes=(config.truncated_note,) if truncated else ())


# --------------------------------------------------------------------------
# Adjudication
# --------------------------------------------------------------------------

def eligible_findings(evidence: ScoringInput) -> tuple[Finding, ...]:
    """Verified, nonduplicate findings of this frozen session/runs, in stable ID order."""
    rows = (row for row in evidence.findings
            if row.status == FindingStatus.VERIFIED and row.duplicate_of is None
            and row.session_id == evidence.session_id and row.run_id in evidence.run_ids)
    return tuple(sorted(rows, key=lambda row: str(row.id)))


@dataclass(frozen=True)
class _Evaluation:
    verdict: ClaimVerdict
    rationale: str
    candidates: tuple[Finding, ...] = ()
    concepts: frozenset[Concept] = frozenset()


def _label(concepts: Sequence[Concept]) -> str:
    return " and ".join(concept.value.replace("_", " ") for concept in concepts)


def _topic_zones(rule: TopicRule, classification: ClaimClassification) -> tuple[Zone, ...]:
    if not classification.zones:
        return rule.zones
    return tuple(zone for zone in rule.zones if zone in classification.zones)


def _matches(rule: TopicRule, classification: ClaimClassification, findings: Sequence[Finding]) -> list[Finding]:
    zones = _topic_zones(rule, classification)
    return [row for row in findings if row.concept == rule.concept and row.zone in zones]


def _assessed(evidence: ScoringInput, finding: Finding) -> bool:
    return any(row.run_id == finding.run_id and row.zone == finding.zone and row.concept == finding.concept
               and row.state == CoverageState.ASSESSED for row in evidence.coverage)


def _coverage_complete(evidence: ScoringInput, rules: Sequence[TopicRule], classification: ClaimClassification) -> bool:
    for rule in rules:
        zones = _topic_zones(rule, classification)
        rows = [row for row in evidence.coverage if row.concept == rule.concept and row.zone in zones]
        if not rows or any(row.state != CoverageState.ASSESSED for row in rows):
            return False
    return True


def _evaluate_contradiction(classification: ClaimClassification, visible: Sequence[TopicRule],
                            findings: Sequence[Finding], config: ClaimsConfig) -> list[Finding]:
    if classification.polarity == ClaimPolarity.ABSENCE:
        threshold = 1
    elif classification.polarity == ClaimPolarity.QUALITY:
        threshold = config.quality_contradiction_min_severity
    else:
        return []
    return [row for rule in visible for row in _matches(rule, classification, findings)
            if row.severity is not None and row.severity >= threshold]


def _evaluate_presence(classification: ClaimClassification, visible: Sequence[TopicRule], findings: Sequence[Finding],
                       evidence: ScoringInput, config: ClaimsConfig, label: str) -> _Evaluation:
    text = config.rationales
    supporting: list[Finding] = []
    satisfied = True
    saw_unassessed = False
    unresolved = False
    for rule in visible:
        found = _matches(rule, classification, findings)
        good = [row for row in found if _assessed(evidence, row)]
        unresolved = unresolved or (bool(classification.zones) and not _topic_zones(rule, classification))
        saw_unassessed = saw_unassessed or (bool(found) and not good)
        satisfied = satisfied and bool(good)
        supporting.extend(good)
    concepts = frozenset(rule.concept for rule in visible)
    if not satisfied:
        reason = (text.location_unresolved if unresolved else text.presence_partial if saw_unassessed
                  else text.presence_unmatched)
        return _Evaluation(ClaimVerdict.NOT_VISIBLE, reason.format(concept_label=label, count=0, coverage_clause=""))
    if classification.minimizing and any((row.severity or 0) > config.minimizer_max_severity for row in supporting):
        return _Evaluation(ClaimVerdict.NOT_VISIBLE, text.minimizer.format(concept_label=label, count=0, coverage_clause=""))
    unique = tuple(sorted({row.id: row for row in supporting}.values(), key=lambda row: str(row.id)))
    return _Evaluation(ClaimVerdict.SUPPORTED, text.supported.format(concept_label=label, count=len(unique), coverage_clause=""),
                       unique, concepts)


def _evaluate(text: str, evidence: ScoringInput, findings: Sequence[Finding], config: ClaimsConfig) -> _Evaluation:
    classification = classify_claim(text, config)
    rules = [rule for rule in config.topics if rule.topic in classification.topics]
    visible = [rule for rule in rules if rule.kind != TopicKind.NOT_VISIBLE]
    hidden = [rule for rule in rules if rule.kind == TopicKind.NOT_VISIBLE]
    rationales = config.rationales
    label = _label([rule.concept for rule in visible])
    overriding = next((rule for rule in hidden if rule.overrides_visible), None)
    if overriding is not None:
        return _Evaluation(ClaimVerdict.NOT_VISIBLE, overriding.rationale or "")
    contradicting = _evaluate_contradiction(classification, visible, findings, config)
    if contradicting:
        unique = tuple(sorted({row.id: row for row in contradicting}.values(), key=lambda row: str(row.id)))
        concepts = frozenset(row.concept for row in unique)
        return _Evaluation(ClaimVerdict.CONTRADICTED,
                           rationales.contradicted.format(concept_label=_label(sorted(concepts, key=lambda item: item.value)),
                                                          count=len(unique), coverage_clause=""),
                           unique, concepts)
    if hidden:
        return _Evaluation(ClaimVerdict.NOT_VISIBLE, hidden[0].rationale or "")
    if not visible:
        return _Evaluation(ClaimVerdict.NOT_VISIBLE, rationales.other)
    if classification.polarity == ClaimPolarity.PRESENCE:
        return _evaluate_presence(classification, visible, findings, evidence, config, label)
    if classification.polarity == ClaimPolarity.NEUTRAL:
        return _Evaluation(ClaimVerdict.NOT_VISIBLE, rationales.neutral_visible.format(concept_label=label, count=0, coverage_clause=""))
    clause = rationales.coverage_assessed if _coverage_complete(evidence, visible, classification) else rationales.coverage_incomplete
    return _Evaluation(ClaimVerdict.NOT_VISIBLE, rationales.absence_unverified.format(concept_label=label, count=0, coverage_clause=clause))


def _cited(candidates: Sequence[Finding], limit: int) -> tuple[Finding, ...]:
    top = sorted(candidates, key=lambda row: (-(row.severity or 0), str(row.id)))[:limit]
    return tuple(sorted(top, key=lambda row: str(row.id)))


def _result(claim: ListingClaim, outcome: _Evaluation, config: ClaimsConfig) -> ClaimResult:
    cited = _cited(outcome.candidates, config.max_cited_findings) if outcome.verdict != ClaimVerdict.NOT_VISIBLE else ()
    frames = tuple(sorted({row.evidence.frame_key for row in cited}))
    return ClaimResult(claim_index=claim.claim_index, text=claim.text, verdict=outcome.verdict,
                       finding_ids=tuple(row.id for row in cited), evidence_frame_keys=frames,
                       rationale=outcome.rationale[:1000] or config.rationales.other)


def adjudicate_claims(claims: Sequence[ListingClaim], evidence: ScoringInput, config: ClaimsConfig) -> tuple[ClaimResult, ...]:
    """Deterministic verdicts over a frozen evidence set.

    - contradicted: only verified, nonduplicate same-session evidence of the
      claim's concept in the claim's area (absence/quality wording).
    - supported: only a presence claim backed by verified evidence whose
      concept coverage is `assessed`; absence of detection is never support.
    - everything else (history, newness, mechanical, A/C, brakes, structure,
      other, partial coverage) is not_visible.
    """
    findings = eligible_findings(evidence)
    return tuple(_result(claim, _evaluate(claim.text, evidence, findings, config), config)
                 for claim in sorted(claims, key=lambda item: item.claim_index))


# --------------------------------------------------------------------------
# Output assembly, model-output validation and fallback
# --------------------------------------------------------------------------

class ClaimsOutput(ContractModel):
    claims: tuple[ClaimResult, ...]
    notes: tuple[str, ...]
    source: Literal["deterministic", "model", "fallback"]
    errors: tuple[str, ...]
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    listing_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class ClaimValidationError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def _parse_claims(raw: object) -> list[ClaimResult]:
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise ClaimValidationError(["model claims must be a list"])
    parsed: list[ClaimResult] = []
    errors: list[str] = []
    for position, item in enumerate(raw):
        try:
            parsed.append(item if isinstance(item, ClaimResult) else ClaimResult.model_validate(item))
        except ValidationError as exc:
            errors.append(f"claim {position} is malformed: {len(exc.errors())} schema error(s)")
    if errors:
        raise ClaimValidationError(errors)
    return parsed


def _shape_errors(parsed: Sequence[ClaimResult], expected: Sequence[ListingClaim]) -> list[str]:
    by_index = {row.claim_index: row for row in expected}
    indices = [row.claim_index for row in parsed]
    errors: list[str] = []
    if len(set(indices)) != len(indices):
        errors.append("duplicate claim index")
    if set(indices) != by_index.keys():
        errors.append("claim indices do not match the extracted listing claims")
    for row in parsed:
        source = by_index.get(row.claim_index)
        if source is not None and row.text != source.text:
            errors.append(f"claim {row.claim_index} text differs from the listing claim")
        if _URL_IN_MODEL_TEXT.search(row.rationale):
            errors.append(f"claim {row.claim_index} rationale contains a link")
    return errors


def _rule_errors(row: ClaimResult, evidence: ScoringInput, findings: Sequence[Finding], config: ClaimsConfig) -> list[str]:
    outcome = _evaluate(row.text, evidence, findings, config)
    cited = set(row.finding_ids)
    allowed = {item.id for item in outcome.candidates}
    index = row.claim_index
    if row.verdict == ClaimVerdict.NOT_VISIBLE:
        if outcome.verdict == ClaimVerdict.CONTRADICTED:
            return [f"claim {index} omits a contradiction established by verified evidence"]
        return []
    if not row.evidence_frame_keys:
        return [f"claim {index} cites no evidence frames"]
    if outcome.verdict != row.verdict:
        return [f"claim {index} verdict {row.verdict.value} is not justified by the claim wording and evidence"]
    if not cited <= allowed:
        return [f"claim {index} cites evidence that does not establish its verdict"]
    by_id = {item.id: item for item in outcome.candidates}
    if {by_id[item].concept for item in cited} != outcome.concepts and row.verdict == ClaimVerdict.SUPPORTED:
        return [f"claim {index} does not cite evidence for every concept it asserts"]
    return []


def validate_model_claims(raw: object, expected: Sequence[ListingClaim], evidence: ScoringInput,
                          config: ClaimsConfig) -> tuple[ClaimResult, ...]:
    """Validate untrusted model claim output or raise `ClaimValidationError`.

    All-or-nothing: any rejected item rejects the whole list so no partial
    result can be written. Citations must belong to the provided same-session
    evidence whitelist (verified, nonduplicate); verdicts must be justified by
    the deterministic rules. Model output may be more conservative about
    supported claims but may not omit a contradiction.
    """
    parsed = _parse_claims(raw)
    errors = _shape_errors(parsed, expected)
    findings = eligible_findings(evidence)
    try:
        ClaimGroup(findings=findings, claims=tuple(parsed))
    except ValidationError as exc:
        errors.extend(f"citation rejected: {item['msg']}" for item in exc.errors())
    if not errors:
        for row in parsed:
            errors.extend(_rule_errors(row, evidence, findings, config))
    if errors:
        raise ClaimValidationError(errors)
    return tuple(sorted(parsed, key=lambda item: item.claim_index))


def resolve_claims(listing_text: str | None, evidence: ScoringInput, config: ClaimsConfig,
                   model_output: object | None = None) -> ClaimsOutput:
    """Claims for a snapshot: validated model output, else deterministic template path.

    `model_output=None` means no model was used. An invalid model output yields
    the deterministic claims with `source="fallback"` and the rejection reasons;
    nothing from the rejected output is kept. Fully offline.
    """
    extraction = extract_listing_claims(listing_text, config)
    deterministic = adjudicate_claims(extraction.claims, evidence, config)
    common = {"notes": extraction.notes, "config_hash": config.content_hash, "listing_hash": listing_hash(listing_text, config)}
    if model_output is None:
        return ClaimsOutput(claims=deterministic, source="deterministic", errors=(), **common)
    try:
        validated = validate_model_claims(model_output, extraction.claims, evidence, config)
    except ClaimValidationError as exc:
        return ClaimsOutput(claims=deterministic, source="fallback", errors=exc.errors, **common)
    return ClaimsOutput(claims=validated, source="model", errors=(), **common)


def claim_uuid_set(claims: Sequence[ClaimResult]) -> frozenset[UUID]:
    """Every finding ID cited by a claim list (for membership tables)."""
    return frozenset(item for claim in claims for item in claim.finding_ids)
