"""Synthetic inputs validate design rules, not visual accuracy or shop prices."""

import json
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.contracts.enums import PART_ZONES, ComponentId, Concept, CoverageState, FindingStatus, Zone
from app.contracts.evidence import BoundingBox, Coverage, EvidenceRef, Finding
from app.contracts.scoring import ScoringInput
from app.domain import estimate_repairs, load_cost_config, load_scoring_config, score_assessment

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "tests/fixtures/legacy_config"
SCORING = load_scoring_config(LEGACY / "scoring.v1.json")  # original behaviour; the live config is covered in test_missing_inputs.py
COSTS = load_cost_config(ROOT / "config/costs.v1.json")
SESSION = UUID(int=1)


def finding(index: int = 100, *, zone: Zone = Zone.DRIVER_SIDE, concept: Concept = Concept.RUST,
            severity: int | None = 5, status: FindingStatus = FindingStatus.VERIFIED,
            component: ComponentId | None = ComponentId.DRIVER_ROCKER, flags: tuple[str, ...] = (),
            duplicate_of: UUID | None = None) -> Finding:
    part = PART_ZONES.index(zone) + 1
    return Finding(id=UUID(int=index), session_id=SESSION, run_id=UUID(int=10 + part), part_id=UUID(int=20 + part),
                   concept_run_id=UUID(int=30 + part), part_no=part, zone=zone, track_id=str(index), status=status,
                   concept=concept, evidence=EvidenceRef(frame_key="synthetic/frame.png", frame_index=0,
                   frame_width=100, frame_height=100, bbox=BoundingBox(x=0, y=0, w=.5, h=.5)), severity=severity,
                   evidence_note="Synthetic fixture", component_id=component, flags=flags, duplicate_of=duplicate_of)


def coverage(zone: Zone) -> list[Coverage]:
    row = next(row for row in SCORING.zones if row.zone == zone)
    return [Coverage(run_id=UUID(int=11 + PART_ZONES.index(zone)), zone=zone, concept=concept,
                     state=CoverageState.ASSESSED) for concept in row.required_concepts]


def inputs(findings: tuple[Finding, ...] = (), zones: tuple[Zone, ...] = (Zone.DRIVER_SIDE,),
           rows: list[Coverage] | None = None) -> ScoringInput:
    rows = rows if rows is not None else [item for zone in zones for item in coverage(zone)]
    return ScoringInput(session_id=SESSION, run_ids=frozenset([item.run_id for item in rows] + [item.run_id for item in findings]),
                        findings=findings, coverage=tuple(rows))


@pytest.mark.parametrize(("count", "expected"), [(1, "85"), (3, "55"), (5, "25"), (8, "25")])
def test_rust_vectors_flags_score_once(count: int, expected: str) -> None:
    result = score_assessment(inputs(tuple(finding(100 + index, flags=("high_risk",)) for index in range(count))), SCORING)
    assert result.zones[0].score == Decimal(expected)
    assert result.headline is None
    assert result.config_hash == SCORING.content_hash


def test_headline_exact_weighted_mean_final_half_up() -> None:
    data = inputs((finding(),), (Zone.DRIVER_SIDE, Zone.PASSENGER_SIDE, Zone.FRONT_REAR_BODY))
    result = score_assessment(data, SCORING)
    assert result.headline == Decimal("94.375")
    assert result.model_dump(mode="json")["headline"] == 94.4
    assert [row.zone for row in result.zones] == list(PART_ZONES)


def test_empty_success_is_100_but_absent_unsupported_zones_are_null() -> None:
    result = score_assessment(inputs(), SCORING)
    assert result.zones[0].score == 100
    assert result.zones[0].reason == "No visible findings detected in assessed footage."
    assert all(row.score is None for row in result.zones[1:])
    assert result.zones[4].state == CoverageState.NOT_ASSESSED
    empty = score_assessment(inputs(zones=()), SCORING)
    assert empty.headline is None and all(row.score is None for row in empty.zones)


@pytest.mark.parametrize("state", [CoverageState.PARTIAL, CoverageState.NOT_ASSESSED])
def test_failed_quality_or_unavailable_coverage_cannot_score_clean(state: CoverageState) -> None:
    rows = coverage(Zone.DRIVER_SIDE)
    rows[0] = rows[0].model_copy(update={"state": state, "reason": "Synthetic failure or unusable quality"})
    result = score_assessment(inputs((finding(),), rows=rows), SCORING)
    assert result.zones[0].score is None
    assert result.zones[0].state == CoverageState.PARTIAL


@pytest.mark.parametrize("status", [FindingStatus.CANDIDATE, FindingStatus.UNVERIFIED])
def test_unresolved_finding_overrides_assessed_coverage(status: FindingStatus) -> None:
    data = inputs((finding(), finding(101, status=status, severity=None)))
    assert score_assessment(data, SCORING).zones[0].score is None
    assert data.findings[0].severity == 5  # pure engine retains visible verified evidence


def test_missing_concept_coverage_or_finding_run_is_partial() -> None:
    rows = coverage(Zone.DRIVER_SIDE)
    assert score_assessment(inputs(rows=rows[1:]), SCORING).zones[0].score is None
    foreign_run = finding().model_copy(update={"run_id": UUID(int=999)})
    assert score_assessment(inputs((foreign_run,)), SCORING).zones[0].score is None


def test_other_run_cannot_hide_failed_concept() -> None:
    rows = coverage(Zone.DRIVER_SIDE)
    rows.append(Coverage(run_id=UUID(int=999), zone=Zone.DRIVER_SIDE, concept=Concept.RUST,
                         state=CoverageState.PARTIAL, reason="Synthetic failed repeat run"))
    assert score_assessment(inputs(rows=rows), SCORING).zones[0].score is None


def test_rejected_duplicate_and_artifact_are_not_scored_or_charged() -> None:
    data = inputs((finding(severity=3), finding(101, severity=None, status=FindingStatus.REJECTED, duplicate_of=UUID(int=100)),
                   finding(102, severity=None, status=FindingStatus.REJECTED)))
    assert score_assessment(data, SCORING).zones[0].score == 91
    repair = estimate_repairs(data, COSTS)
    assert len(repair.items) == 1 and repair.items[0].finding_ids == (UUID(int=100),)


@pytest.mark.parametrize(("severity", "low", "high"), [(1, 5000, 10000), (2, 5000, 10000), (3, 9000, 16000), (4, 13000, 22000), (5, 13000, 22000)])
def test_tire_severity_bands(severity: int, low: int, high: int) -> None:
    data = inputs((finding(zone=Zone.TIRES, concept=Concept.TIRE_WEAR, component=ComponentId.TIRE_FL, severity=severity),), (Zone.TIRES,))
    item = estimate_repairs(data, COSTS).items[0]
    assert (item.low_cents, item.high_cents) == (low, high)


def test_cosmetic_component_group_max_severity_and_distinct_components() -> None:
    a = finding(concept=Concept.SCRATCH, component=ComponentId.DRIVER_FRONT_DOOR, severity=2)
    b = finding(101, concept=Concept.PAINT_BUBBLING, component=ComponentId.DRIVER_FRONT_DOOR, severity=3)
    result = estimate_repairs(inputs((b, a)), COSTS)
    assert len(result.items) == 1
    assert result.items[0].finding_ids == (a.id, b.id)
    assert (result.subtotal_low_cents, result.subtotal_high_cents) == (15000, 50000)
    c = finding(102, concept=Concept.SCRATCH, component=ComponentId.DRIVER_REAR_DOOR, severity=3)
    assert estimate_repairs(inputs((a, b, c)), COSTS).subtotal_low_cents == 30000


@pytest.mark.parametrize(("concept", "component", "severity", "flags"), [
    (Concept.RUST, ComponentId.DRIVER_ROCKER, 5, ()),
    (Concept.RUST, ComponentId.DRIVER_ROCKER, 2, ("structural_rust",)),
    (Concept.RUST, ComponentId.DRIVER_ROCKER, 3, ("perforation",)),
    (Concept.RUST, ComponentId.HOOD, 3, ()),
    (Concept.DENT, ComponentId.DRIVER_FRONT_DOOR, 3, ()),
    (Concept.FLUID_LEAK, ComponentId.HOOD, 3, ()),
    (Concept.SCRATCH, None, 3, ()),
])
def test_unknown_repairs_are_null_with_reason(concept: Concept, component: ComponentId | None, severity: int, flags: tuple[str, ...]) -> None:
    result = estimate_repairs(inputs((finding(concept=concept, component=component, severity=severity, flags=flags),)), COSTS)
    assert result.items[0].low_cents is None and result.items[0].high_cents is None
    assert result.items[0].reason and result.unestimated_count == 1
    assert result.label.startswith("Subtotal of estimated repairs")
    assert "not a current shop quote" in result.label


def test_structural_component_cannot_get_surface_rocker_quote() -> None:
    result = estimate_repairs(inputs((finding(severity=2), finding(101, severity=5))), COSTS)
    assert all(item.low_cents is None for item in result.items)


def test_known_plus_unknown_is_only_subtotal() -> None:
    result = estimate_repairs(inputs((finding(concept=Concept.SCRATCH, component=ComponentId.HOOD, severity=3), finding(101, severity=5))), COSTS)
    assert (result.subtotal_low_cents, result.subtotal_high_cents, result.unestimated_count) == (15000, 50000, 1)
    assert result.table_version == COSTS.version


def test_deterministic_config_hash_and_input_order(tmp_path: Path) -> None:
    payload = json.loads((LEGACY / "scoring.v1.json").read_text())
    path = tmp_path / "scoring.json"
    path.write_text(json.dumps(dict(reversed(list(payload.items()))), indent=4))
    assert load_scoring_config(path).content_hash == SCORING.content_hash
    a, b = finding(severity=3), finding(101, severity=2)
    assert score_assessment(inputs((a, b)), SCORING) == score_assessment(inputs((b, a)), SCORING)
    assert estimate_repairs(inputs((a, b)), COSTS) == estimate_repairs(inputs((b, a)), COSTS)
    assert len(COSTS.content_hash) == 64


def test_config_rejects_missing_weight_duplicate_mapping_and_invalid_values() -> None:
    with pytest.raises(ValidationError):
        type(SCORING).model_validate({**SCORING.model_dump(), "zones": SCORING.zones[:-1]})
    with pytest.raises(ValidationError):
        type(COSTS).model_validate({**COSTS.model_dump(), "rows": COSTS.rows + COSTS.rows[:1]})
    with pytest.raises(ValidationError):
        type(SCORING).model_validate({**SCORING.model_dump(), "deduction_cap": "NaN"})


def test_decimal_context_does_not_change_results() -> None:
    data = inputs((finding(),), (Zone.DRIVER_SIDE, Zone.PASSENGER_SIDE, Zone.FRONT_REAR_BODY))
    with localcontext() as context:
        context.prec = 3
        assert score_assessment(data, SCORING).headline == Decimal("94.375")


def test_half_up_midpoint_cost_and_score_serialization() -> None:
    # Synthetic config values exercise tie rounding; they are not shop prices.
    payload = COSTS.model_dump()
    payload["rows"][0]["low_dollars"] = Decimal("95")
    payload["rows"][0]["high_dollars"] = Decimal("165")
    config = type(COSTS).model_validate(payload)
    data = inputs((finding(zone=Zone.TIRES, concept=Concept.TIRE_WEAR, component=ComponentId.TIRE_FL, severity=3),), (Zone.TIRES,))
    item = estimate_repairs(data, config).items[0]
    assert (item.low_cents, item.high_cents) == (10000, 17000)

    score_payload = SCORING.model_dump()
    next(row for row in score_payload["concepts"] if row["concept"] == Concept.RUST)["weight"] = Decimal("0.05")
    score_config = type(SCORING).model_validate(score_payload)
    score = score_assessment(inputs((finding(severity=1),)), score_config)
    assert score.zones[0].score == Decimal("99.95")
    assert score.model_dump(mode="json")["zones"][0]["score"] == 100.0


def test_serialized_outputs_stable_with_reordered_frozen_inputs() -> None:
    a, b = finding(severity=3), finding(101, severity=2)
    original = inputs((a, b))
    reordered = original.model_copy(update={"findings": (b, a), "coverage": tuple(reversed(original.coverage))})
    before = original.model_dump_json()
    assert score_assessment(original, SCORING).model_dump_json() == score_assessment(reordered, SCORING).model_dump_json()
    assert estimate_repairs(original, COSTS).model_dump_json() == estimate_repairs(reordered, COSTS).model_dump_json()
    assert original.model_dump_json() == before
