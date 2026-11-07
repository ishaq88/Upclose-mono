"""Synthetic inputs validate template/validator rules, not visual accuracy or shop prices."""

import random
import re
import socket
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.contracts.enums import PART_ZONES, ClaimVerdict, ComponentId, Concept, CoverageState, FindingStatus, Zone
from app.contracts.evidence import BoundingBox, ClaimResult, Coverage, EvidenceRef, Finding, NarratedStatement
from app.contracts.scoring import ScoringInput
from app.domain import estimate_repairs, load_cost_config, load_scoring_config, score_assessment
from app.domain.claims import load_claims_config, resolve_claims
from app.domain.narration import (
    NarrationConfig, NarrationInputError, NarrationScope, NarrationValidationError, load_narration_config,
    narrate_full, narrate_part1, narration_hash, part1_summary_text, resolve_full_narration, resolve_part1_narration,
    validate_model_narration,
)

ROOT = Path(__file__).resolve().parents[1]
NARRATION = load_narration_config(ROOT / "config/narration.v1.json")
SCORING = load_scoring_config(ROOT / "config/scoring.v1.json")
COSTS = load_cost_config(ROOT / "config/costs.v1.json")
CLAIMS = load_claims_config(ROOT / "config/claims.v1.json")
SESSION = UUID(int=1)
FREE_PHRASE = "No visible findings detected in assessed footage"


def finding(index: int = 100, *, zone: Zone = Zone.DRIVER_SIDE, concept: Concept = Concept.RUST, severity: int | None = 3,
            status: FindingStatus = FindingStatus.VERIFIED, component: ComponentId | None = None,
            flags: tuple[str, ...] = (), confidence: float | None = None, note: str = "Synthetic fixture") -> Finding:
    part = PART_ZONES.index(zone) + 1
    return Finding(id=UUID(int=index), session_id=SESSION, run_id=UUID(int=10 + part), part_id=UUID(int=20 + part),
                   concept_run_id=UUID(int=30 + part), part_no=part, zone=zone, track_id=str(index), status=status,
                   concept=concept, evidence=EvidenceRef(frame_key=f"synthetic/frame_{index}.png", frame_index=index,
                   frame_width=100, frame_height=100, bbox=BoundingBox(x=0, y=0, w=.5, h=.5)), severity=severity,
                   evidence_note=note, component_id=component, flags=flags, confidence=confidence)


def coverage(zone: Zone, state: CoverageState = CoverageState.ASSESSED) -> list[Coverage]:
    row = next(row for row in SCORING.zones if row.zone == zone)
    reason = None if state == CoverageState.ASSESSED else "Synthetic gap"
    return [Coverage(run_id=UUID(int=11 + PART_ZONES.index(zone)), zone=zone, concept=concept, state=state, reason=reason)
            for concept in row.required_concepts]


def evidence(findings: tuple[Finding, ...] = (), zones: tuple[Zone, ...] = (Zone.DRIVER_SIDE,),
             rows: list[Coverage] | None = None) -> ScoringInput:
    rows = rows if rows is not None else [item for zone in zones for item in coverage(zone)]
    return ScoringInput(session_id=SESSION, run_ids=frozenset([r.run_id for r in rows] + [f.run_id for f in findings]),
                        findings=findings, coverage=tuple(rows))


def part1(data: ScoringInput) -> tuple[NarratedStatement, ...]:
    return narrate_part1(data, score_assessment(data, SCORING), NARRATION)


def full(data: ScoringInput, listing: str | None = None) -> tuple[NarratedStatement, ...]:
    claims = resolve_claims(listing, data, CLAIMS).claims
    return narrate_full(data, score_assessment(data, SCORING), estimate_repairs(data, COSTS), claims, NARRATION)


def text_of(statements: tuple[NarratedStatement, ...]) -> str:
    return " ".join(item.text for item in statements)


ALL_ZONES = (Zone.DRIVER_SIDE, Zone.PASSENGER_SIDE, Zone.FRONT_REAR_BODY, Zone.TIRES, Zone.ENGINE_BAY, Zone.UNDERBODY)


def rich_evidence() -> ScoringInput:
    rows = (
        finding(100, component=ComponentId.DRIVER_ROCKER, severity=2, confidence=0.9),
        finding(200, zone=Zone.PASSENGER_SIDE, severity=5, flags=("structural_rust",), note="SECRETNOTE passenger rocker"),
        finding(300, zone=Zone.TIRES, concept=Concept.TIRE_WEAR, severity=4, component=ComponentId.TIRE_FL),
        finding(400, zone=Zone.UNDERBODY, concept=Concept.FLUID_LEAK, severity=3),
        finding(500, zone=Zone.ENGINE_BAY, concept=Concept.FLUID_LEAK, severity=1),
        finding(600, zone=Zone.FRONT_REAR_BODY, concept=Concept.GLASS_CRACK, severity=3, component=ComponentId.WINDSHIELD),
    )
    return evidence(rows, ALL_ZONES)


# --- config -----------------------------------------------------------------

def test_config_hash_and_validation() -> None:
    assert len(NARRATION.content_hash) == 64
    assert load_narration_config(ROOT / "config/narration.v1.json").content_hash == NARRATION.content_hash
    payload = NARRATION.model_dump()
    assert NarrationConfig.model_validate({**payload, "max_statements": 79}).content_hash != NARRATION.content_hash
    with pytest.raises(ValidationError):
        NarrationConfig.model_validate({**payload, "templates": payload["templates"][1:]})
    bad = [dict(row) for row in payload["templates"]]
    bad[2]["text"] = "{zone_label} {unknown_slot}"
    with pytest.raises(ValidationError):
        NarrationConfig.model_validate({**payload, "templates": bad})
    with pytest.raises(ValidationError):
        NarrationConfig.model_validate({**payload, "zero_finding_phrase": "Everything is fine"})


# --- free Part-1 summary ----------------------------------------------------

def test_free_summary_contains_only_part1_evidence() -> None:
    data = rich_evidence()
    statements = part1(data)
    blob = text_of(statements)
    cited = {item for statement in statements for item in statement.finding_ids}
    assert cited == {UUID(int=100)}
    for forbidden in ("SECRETNOTE", "Passenger", "passenger", "Tires", "tire", "Underbody", "underbody", "Engine", "engine",
                      "Front/rear", "$", "claim", "Claim", "listing", "Overall", "overall", "headline", "estimate", "repair"):
        assert forbidden not in blob
    assert "driver" in blob.lower()
    for other in (200, 300, 400, 500, 600):
        assert str(UUID(int=other)) not in blob
    assert not any(re.search(pattern, blob, re.IGNORECASE) for pattern in NARRATION.free_forbidden_patterns)


def test_free_summary_ignores_other_zone_changes_exactly() -> None:
    base = evidence((finding(100),), (Zone.DRIVER_SIDE,))
    noisy = evidence((finding(100), finding(200, zone=Zone.PASSENGER_SIDE, severity=5), finding(300, zone=Zone.UNDERBODY)),
                     ALL_ZONES)
    assert part1(base) == part1(noisy)
    # The free summary also does not depend on the other zones' scores.
    score = score_assessment(noisy, SCORING)
    assert narrate_part1(base, score, NARRATION) == part1(base)


def test_free_summary_numbers_come_from_score_result() -> None:
    data = evidence((finding(100, severity=5), finding(101, severity=2)))
    statements = part1(data)
    assert statements[0].text == "Driver side visual-condition index: 79.0 of 100 (provisional)."
    assert "2 verified findings are listed; highest severity 5 of 5" in text_of(statements)
    summary = part1_summary_text(statements)
    assert summary.startswith("Driver side visual-condition index") and len(summary) <= 2000


def test_zero_finding_wording_and_no_damage_free() -> None:
    statements = part1(evidence())
    blob = text_of(statements)
    assert FREE_PHRASE in blob and "100.0" in blob
    assert "damage free" not in blob.lower() and "damage-free" not in blob.lower()
    assert "provisional" in blob and "not a safety certification" in blob


@pytest.mark.parametrize("state", [CoverageState.PARTIAL, CoverageState.NOT_ASSESSED])
def test_partial_coverage_states_gap_and_never_claims_clean(state: CoverageState) -> None:
    rows = coverage(Zone.DRIVER_SIDE)
    rows[0] = rows[0].model_copy(update={"state": state, "reason": "Synthetic RAWREASON"})
    for findings in ((), (finding(100),)):
        blob = text_of(part1(evidence(findings, rows=rows)))
        assert FREE_PHRASE not in blob and "RAWREASON" not in blob
        assert "no score is given" in blob and "Coverage gaps: rust (" in blob
        assert ("absence of other findings cannot be concluded" in blob) == bool(findings)


def test_missing_zone_evidence_is_not_a_clean_summary() -> None:
    blob = text_of(part1(evidence(zones=())))
    assert FREE_PHRASE not in blob and "no score is given" in blob and NARRATION.gaps_unrecorded_text in blob


def test_confidence_only_mentioned_when_present_and_labeled_uncalibrated() -> None:
    with_conf = text_of(part1(evidence((finding(100, confidence=0.8),))))
    assert "uncalibrated" in with_conf and "0.8" not in with_conf
    assert "confidence" not in text_of(part1(evidence((finding(100),)))).lower()


# --- full narration ---------------------------------------------------------

def test_full_narration_numbers_and_citations() -> None:
    data = rich_evidence()
    statements = full(data)
    blob = text_of(statements)
    score = score_assessment(data, SCORING)
    assert f"Overall provisional visual-condition index: {score.headline:.1f} of 100" in blob
    ids = {row.id for row in data.findings}
    assert {item for st in statements for item in st.finding_ids} <= ids
    assert "Driver side: visual-condition index 94.0 of 100 (provisional)." in blob
    assert "Severity 5 of 5 rust finding: Passenger side" in blob and "flagged as high risk" in blob
    assert statements[1].text.startswith("1 verified finding is flagged as high risk") and statements[1].finding_ids == (UUID(int=200),)
    assert "Dash and electrical: no score because coverage is not assessed." in blob
    assert "Coverage gaps:" in blob and "Interior (not assessed)" in blob
    assert "not a safety certification" in blob and "uncalibrated" in blob


def test_full_costs_subtotal_and_unknown_never_zero() -> None:
    data = rich_evidence()
    blob = text_of(full(data))
    repairs = estimate_repairs(data, COSTS)
    assert repairs.unestimated_count > 0
    assert "Subtotal of estimated repairs:" in blob and "not a current shop quote" in blob and COSTS.version in blob
    low, high = repairs.subtotal_low_cents // 100, repairs.subtotal_high_cents // 100
    assert f"${low:,} to ${high:,}" in blob
    only_unknown = evidence((finding(100, severity=5),))
    assert "No repair cost estimate is available" in text_of(full(only_unknown)) and "$0" not in text_of(full(only_unknown))
    assert "No repair estimates were produced." in text_of(full(evidence()))
    known = evidence((finding(100, zone=Zone.TIRES, concept=Concept.TIRE_WEAR, component=ComponentId.TIRE_FL, severity=3),), (Zone.TIRES,))
    assert "Estimated repairs: $90 to $160." in text_of(full(known))


def test_full_zero_finding_zone_wording_and_partial_zone() -> None:
    clean = text_of(full(evidence()))
    assert f"Driver side: {FREE_PHRASE}." in clean and "damage free" not in clean.lower()
    rows = coverage(Zone.DRIVER_SIDE, CoverageState.PARTIAL)
    partial = text_of(full(evidence((finding(100),), rows=rows)))
    assert "Driver side: no score because coverage is partial; 1 verified finding listed" in partial
    assert FREE_PHRASE not in partial
    assert "No overall index is given because too few zones were assessed (0 assessed)" in partial


def test_dedup_bookkeeping_flags_are_not_high_risk() -> None:
    bookkeeping = text_of(full(evidence((finding(100, flags=("dedup_merged", "duplicate_review")),))))
    assert "high risk" not in bookkeeping
    risky = text_of(full(evidence((finding(100, flags=("dedup_merged", "structural_rust")),))))
    assert "flagged as high risk" in risky


def test_full_claims_statements() -> None:
    data = rich_evidence()
    none = text_of(full(data))
    assert "No listing claims supplied." in none
    statements = full(data, "No rust. Runs great")
    blob = text_of(statements)
    assert "Listing claims checked: 2. Contradicted by verified evidence: 1. Supported by verified evidence: 0. Not visible in the footage: 1." in blob
    assert "Listing claim 1 is contradicted by verified evidence." in blob
    assert "Runs great" not in blob  # untrusted listing text is never echoed


def test_full_rejects_foreign_citations() -> None:
    data = evidence((finding(100),))
    bad = [ClaimResult(claim_index=0, text="x", verdict=ClaimVerdict.CONTRADICTED, finding_ids=(UUID(int=999),),
                       evidence_frame_keys=("k",), rationale="r")]
    with pytest.raises(NarrationInputError):
        narrate_full(data, score_assessment(data, SCORING), estimate_repairs(data, COSTS), bad, NARRATION)


def test_duplicates_and_unverified_never_narrated() -> None:
    rows = (finding(100), finding(101, status=FindingStatus.REJECTED, severity=None, note="DUPNOTE"),
            finding(102, status=FindingStatus.UNVERIFIED, severity=None))
    statements = full(evidence(rows))
    cited = {item for st in statements for item in st.finding_ids}
    assert cited == {UUID(int=100)}


# --- model narration validation / fallback ----------------------------------

def _free_template(data: ScoringInput) -> tuple[NarratedStatement, ...]:
    return part1(data)


def test_valid_model_narration_accepted_and_template_self_consistent() -> None:
    data = rich_evidence()
    template = part1(data)
    allowed = frozenset({UUID(int=100)})
    assert validate_model_narration(list(template), scope=NarrationScope.FREE, allowed_ids=allowed,
                                    template_statements=template, config=NARRATION) == template
    score = score_assessment(data, SCORING)
    raw = [{"text": "Driver side visual-condition index: 94.0 of 100 (provisional).", "finding_ids": [UUID(int=100)]}]
    outcome = resolve_part1_narration(data, score, NARRATION, raw)
    assert outcome.source == "model" and outcome.statements[0].finding_ids == (UUID(int=100),)
    paid = resolve_full_narration(data, score, estimate_repairs(data, COSTS), (), NARRATION, list(full(data)))
    assert paid.source == "model"


@pytest.mark.parametrize("raw", [
    [{"text": "A claim.", "finding_ids": [UUID(int=999)]}],                               # unknown ID
    [{"text": "Rust seen.", "finding_ids": [UUID(int=200)]}],                              # other-zone ID in free scope
    [{"text": "Driver side index: 99.0 of 100.", "finding_ids": []}],                      # invented number
    [{"text": "The car is damage free.", "finding_ids": []}],                              # assurance wording
    [{"text": "There is no damage on this side.", "finding_ids": []}],                     # absence without required wording
    [{"text": "Model confidence is high.", "finding_ids": []}],                            # unlabeled confidence
    [{"text": "See http://evil.example", "finding_ids": []}],                              # link
    [{"text": "Passenger side has severe rust.", "finding_ids": []}],                      # other zone text
    [{"text": "Repairs cost $5,000.", "finding_ids": []}],                                 # cost in free scope
    [{"text": "Driver side is fine.", "finding_ids": [], "extra": 1}],                     # unknown field
    [], "text", {"text": "x"},                                                             # malformed shape
])
def test_invalid_free_model_narration_falls_back_to_template(raw: object) -> None:
    data = rich_evidence()
    score = score_assessment(data, SCORING)
    outcome = resolve_part1_narration(data, score, NARRATION, raw)
    assert outcome.source == "fallback" and outcome.errors
    assert outcome.statements == part1(data)
    with pytest.raises(NarrationValidationError):
        validate_model_narration(raw, scope=NarrationScope.FREE, allowed_ids=frozenset({UUID(int=100)}),
                                 template_statements=part1(data), config=NARRATION)


def test_paid_validation_allows_all_verified_but_not_foreign_or_unverified_ids() -> None:
    data = evidence((finding(100), finding(101, status=FindingStatus.REJECTED, severity=None)))
    score, repairs = score_assessment(data, SCORING), estimate_repairs(data, COSTS)
    for ids in ([UUID(int=101)], [UUID(int=999)]):
        outcome = resolve_full_narration(data, score, repairs, (), NARRATION, [{"text": "Note.", "finding_ids": ids}])
        assert outcome.source == "fallback"
    ok = resolve_full_narration(data, score, repairs, (), NARRATION, [{"text": "Rust finding noted.", "finding_ids": [UUID(int=100)]}])
    assert ok.source == "model"


def test_paid_model_cannot_invent_arithmetic() -> None:
    data = rich_evidence()
    score, repairs = score_assessment(data, SCORING), estimate_repairs(data, COSTS)
    raw = [{"text": "Total repairs will be about $7,777.", "finding_ids": []}]
    assert resolve_full_narration(data, score, repairs, (), NARRATION, raw).source == "fallback"


def test_fallback_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    data = rich_evidence()
    score = score_assessment(data, SCORING)
    assert resolve_part1_narration(data, score, NARRATION, [{"bad": True}]).source == "fallback"
    assert resolve_full_narration(data, score, estimate_repairs(data, COSTS), (), NARRATION, "junk").source == "fallback"


# --- determinism ------------------------------------------------------------

def test_two_runs_identical_output_and_hashes() -> None:
    data = rich_evidence()
    score, repairs = score_assessment(data, SCORING), estimate_repairs(data, COSTS)
    claims = resolve_claims("No rust. Engine runs great", data, CLAIMS).claims
    a = resolve_full_narration(data, score, repairs, claims, NARRATION)
    b = resolve_full_narration(data, score, repairs, claims, NARRATION)
    assert a == b and a.statements_hash == b.statements_hash == narration_hash(a.statements)
    assert a.config_hash == NARRATION.content_hash and a.source == "template"
    assert part1(data) == part1(data)
    changed = NarrationConfig.model_validate({**NARRATION.model_dump(), "max_statements": 79})
    assert changed.content_hash != a.config_hash


def test_input_order_independence() -> None:
    rows = list(rich_evidence().findings)
    cover = list(rich_evidence().coverage)
    baseline_full = full(evidence(tuple(rows), rows=cover), "No rust. Dent on driver door")
    baseline_free = part1(evidence(tuple(rows), rows=cover))
    rng = random.Random(11)
    for _ in range(10):
        rng.shuffle(rows)
        rng.shuffle(cover)
        data = evidence(tuple(rows), rows=cover)
        assert full(data, "No rust. Dent on driver door") == baseline_full
        assert part1(data) == baseline_free
