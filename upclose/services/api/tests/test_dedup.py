"""Synthetic geometry validates de-duplication rules, not provider accuracy."""

import itertools
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.contracts.enums import PART_ZONES, ComponentId, Concept, FindingStatus, Zone
from app.contracts.evidence import BoundingBox, EvidenceRef, Finding
from app.domain.dedup import (
    DUPLICATE_REVIEW_FLAG, INTERNAL_FLAGS, MERGED_FLAG, DedupConfig, FrameBox, TrackBox, TrackFootprint,
    apply_dedup, collapse_tracks, dedup_candidates, footprint_for, iou_exact, unresolved_overlaps,
)
from app.domain.rubrics import load_prompt_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_prompt_config(ROOT / "config/prompts.v1.json").dedup
SESSION = UUID(int=1)
FULL = (0.25, 0.25, 0.5, 0.5)  # dyadic values keep IoU arithmetic exact
HALF_HEIGHT = (0.25, 0.25, 0.5, 0.25)  # contained in FULL, IoU exactly 0.5
JUST_UNDER = (0.25, 0.25, 0.5, 0.2421875)  # contained in FULL, IoU 0.484375
THIRD = (0.5, 0.25, 0.5, 0.5)  # half-overlaps FULL horizontally, IoU 1/3
FAR = (0.0, 0.0, 0.125, 0.125)  # disjoint from FULL


def box(values: tuple[float, float, float, float]) -> BoundingBox:
    return BoundingBox(x=values[0], y=values[1], w=values[2], h=values[3])


def candidate(index: int, run: int, boxes: dict[int, tuple[float, float, float, float]], *, part_no: int = 1,
              concept: Concept = Concept.RUST, component: ComponentId | None = None, evidence_frame: int | None = None,
              track: str | None = None, flags: tuple[str, ...] = ()) -> TrackFootprint:
    frame = min(boxes) if evidence_frame is None else evidence_frame
    finding = Finding(
        id=UUID(int=index), session_id=SESSION, run_id=UUID(int=10 + part_no), part_id=UUID(int=20 + part_no),
        concept_run_id=UUID(int=1000 * part_no + run), part_no=part_no, zone=PART_ZONES[part_no - 1],
        track_id=track or f"t{index}", status=FindingStatus.CANDIDATE, concept=concept,
        evidence=EvidenceRef(frame_key=f"synthetic/p{part_no}/f{frame}.png", frame_index=frame, frame_width=100,
                             frame_height=100, bbox=box(boxes[frame])),
        evidence_note="Synthetic candidate", component_id=component, flags=flags)
    return TrackFootprint(finding=finding, frames=tuple(FrameBox(frame_index=key, bbox=box(value)) for key, value in sorted(boxes.items())))


def ids(*values: int) -> tuple[UUID, ...]:
    return tuple(UUID(int=value) for value in values)


# -------------------------------------------------------------------- config

def test_config_loads_with_stable_hash_and_validates_thresholds() -> None:
    again = DedupConfig.model_validate_json(json.dumps(CONFIG.model_dump(mode="json")))
    assert again.content_hash == CONFIG.content_hash and len(CONFIG.content_hash) == 64
    assert CONFIG.iou_threshold == Decimal("0.5")
    assert CONFIG.model_copy(update={"iou_threshold": Decimal("0.6")}).content_hash != CONFIG.content_hash
    with pytest.raises(ValidationError):
        DedupConfig(version="x", iou_threshold=Decimal("0.5"), ambiguous_min_iou_exclusive=Decimal("0.5"),
                    merge_on_known_component=True)


def test_iou_is_exact_at_the_boundary() -> None:
    assert iou_exact(box(FULL), box(HALF_HEIGHT)) == Fraction(1, 2)
    assert iou_exact(box(FULL), box(FULL)) == 1
    assert iou_exact(box(FULL), box(FAR)) == 0
    assert iou_exact(box(FULL), box(THIRD)) * 3 == 1


# -------------------------------------------------------------------- tracks

def test_track_observations_collapse_to_one_candidate_with_stable_representative() -> None:
    run = UUID(int=5)
    boxes = [TrackBox(concept_run_id=run, track_id="a", frame_index=index, bbox=box(FULL), confidence=conf)
             for index, conf in ((3, 0.5), (1, 0.9), (2, 0.9))]
    boxes.append(TrackBox(concept_run_id=run, track_id="b", frame_index=1, bbox=box(FAR)))
    groups = collapse_tracks(boxes)
    assert [row.track_id for row in groups] == ["a", "b"]
    assert [row.frame_index for row in groups[0].frames] == [1, 2, 3]
    assert groups[0].representative.frame_index == 1  # equal confidence and area: earliest frame
    assert groups[1].representative.confidence is None
    assert collapse_tracks(reversed(boxes)) == groups
    with pytest.raises(ValueError):
        collapse_tracks([boxes[0], boxes[0]])


def test_footprint_requires_matching_track_and_observed_evidence() -> None:
    row = candidate(1, 1, {1: FULL, 2: FULL})
    group = collapse_tracks([TrackBox(concept_run_id=row.finding.concept_run_id, track_id="t1", frame_index=1, bbox=box(FULL))])[0]
    assert footprint_for(row.finding, group).finding.id == row.finding.id
    with pytest.raises(ValueError):
        footprint_for(row.finding.model_copy(update={"track_id": "other"}), group)
    with pytest.raises(ValidationError):
        TrackFootprint(finding=row.finding, frames=(FrameBox(frame_index=9, bbox=box(FULL)),))
    with pytest.raises(ValidationError):
        TrackFootprint(finding=row.finding.model_copy(update={"status": FindingStatus.UNVERIFIED}), frames=row.frames)


# --------------------------------------------------------------------- merge

def test_same_frame_duplicate_patch_merges_with_provenance_lowest_id_wins() -> None:
    first = candidate(7, 1, {4: FULL})
    second = candidate(3, 2, {4: HALF_HEIGHT})  # IoU exactly 0.5 meets the threshold
    result = dedup_candidates([first, second], CONFIG)
    assert result.survivor_ids == ids(3) and not result.ambiguous
    (merge,) = result.merges
    assert (merge.duplicate_id, merge.survivor_id) == (UUID(int=7), UUID(int=3))
    assert merge.reason == "iou" and merge.frame_index == 4 and merge.iou == Decimal("0.5000")
    assert merge.iou_threshold == Decimal("0.5") and merge.config_hash == CONFIG.content_hash
    assert merge.concept == Concept.RUST and merge.part_id == first.finding.part_id


def test_merge_uses_shared_frame_even_when_tracks_differ_elsewhere() -> None:
    left = candidate(1, 1, {1: FAR, 2: FULL, 3: FAR})
    right = candidate(2, 2, {2: FULL, 4: FAR})
    (merge,) = dedup_candidates([left, right], CONFIG).merges
    assert merge.frame_index == 2


def test_apply_dedup_rejects_duplicate_links_it_and_unions_flags() -> None:
    first = candidate(7, 1, {4: FULL}, flags=("perforation_suspected",))
    second = candidate(3, 2, {4: FULL})
    result = dedup_candidates([first, second], CONFIG)
    survivor, duplicate = apply_dedup([first.finding, second.finding], result)
    assert survivor.id == UUID(int=3) and survivor.status == FindingStatus.CANDIDATE
    assert survivor.flags == ("perforation_suspected",)
    assert duplicate.status == FindingStatus.REJECTED and duplicate.duplicate_of == survivor.id
    assert MERGED_FLAG in duplicate.flags
    with pytest.raises(ValueError):
        apply_dedup([first.finding], result)
    assert INTERNAL_FLAGS == {DUPLICATE_REVIEW_FLAG, MERGED_FLAG}


# ---------------------------------------------------------- never-merge cases

def test_same_coordinates_on_different_frames_never_merge_or_flag() -> None:
    result = dedup_candidates([candidate(1, 1, {10: FULL}), candidate(2, 2, {11: FULL})], CONFIG)
    assert not result.merges and not result.ambiguous and result.survivor_ids == ids(1, 2)


def test_different_tires_never_merge_from_identical_coordinates() -> None:
    front = candidate(1, 1, {0: FULL}, part_no=4, concept=Concept.TIRE_WEAR, component=ComponentId.TIRE_FL)
    rear = candidate(2, 2, {0: FULL}, part_no=4, concept=Concept.TIRE_WEAR, component=ComponentId.TIRE_RL)
    result = dedup_candidates([front, rear], CONFIG)
    assert not result.merges and not result.ambiguous


def test_different_parts_never_merge_even_on_the_same_frame_index() -> None:
    result = dedup_candidates([candidate(1, 1, {0: FULL}, part_no=1), candidate(2, 2, {0: FULL}, part_no=2)], CONFIG)
    assert not result.merges and not result.ambiguous


def test_different_concepts_and_same_concept_run_are_not_compared() -> None:
    rust = candidate(1, 1, {0: FULL})
    dent = candidate(2, 2, {0: FULL}, concept=Concept.DENT)
    sibling = candidate(3, 1, {0: FULL})  # same concept run as `rust`, different track
    assert not dedup_candidates([rust, dent], CONFIG).merges
    result = dedup_candidates([rust, sibling], CONFIG)
    assert not result.merges and not result.ambiguous


def test_known_component_rule_needs_a_shared_frame_and_can_be_disabled() -> None:
    a = candidate(1, 1, {0: FAR}, component=ComponentId.DRIVER_ROCKER)
    b = candidate(2, 2, {0: FULL}, component=ComponentId.DRIVER_ROCKER)
    # Shipped default is off: disjoint patches on one component stay separate findings.
    assert not CONFIG.merge_on_known_component
    assert not dedup_candidates([a, b], CONFIG).merges
    on = CONFIG.model_copy(update={"merge_on_known_component": True})
    (merge,) = dedup_candidates([a, b], on).merges
    assert merge.reason == "same_component" and merge.iou == Decimal("0.0000")
    elsewhere = candidate(3, 3, {5: FULL}, component=ComponentId.DRIVER_ROCKER)
    assert not dedup_candidates([a, elsewhere], on).merges


# ----------------------------------------------------------------- ambiguity

def test_partial_overlap_is_retained_and_flagged_not_merged() -> None:
    first = candidate(1, 1, {2: FULL})
    second = candidate(2, 2, {2: THIRD})
    result = dedup_candidates([first, second], CONFIG)
    assert not result.merges and result.survivor_ids == ids(1, 2)
    (overlap,) = result.ambiguous
    assert overlap.reason == "partial_overlap" and overlap.finding_ids == frozenset(ids(1, 2))
    assert overlap.frame_index == 2 and overlap.iou == Decimal("0.3333")
    flagged = apply_dedup([first.finding, second.finding], result)
    assert all(DUPLICATE_REVIEW_FLAG in row.flags and row.status == FindingStatus.CANDIDATE for row in flagged)


def test_just_below_threshold_is_ambiguous_and_touching_edges_are_not() -> None:
    assert dedup_candidates([candidate(1, 1, {0: FULL}), candidate(2, 2, {0: JUST_UNDER})], CONFIG).ambiguous
    touching = (0.75, 0.25, 0.25, 0.5)  # shares only an edge with FULL
    assert not dedup_candidates([candidate(1, 1, {0: FULL}), candidate(2, 2, {0: touching})], CONFIG).ambiguous


def test_chain_overlap_is_not_merged_transitively() -> None:
    middle = (0.25, 0.25, 0.5, 0.5)
    left = (0.25, 0.25, 0.5, 0.25)  # IoU 0.5 with middle
    lower = (0.25, 0.5, 0.5, 0.25)  # IoU 0.5 with middle, disjoint from left
    rows = [candidate(1, 1, {0: left}), candidate(2, 2, {0: middle}), candidate(3, 3, {0: lower})]
    result = dedup_candidates(rows, CONFIG)
    assert not result.merges and result.survivor_ids == ids(1, 2, 3)
    (overlap,) = result.ambiguous
    assert overlap.reason == "non_transitive_merge" and overlap.finding_ids == frozenset(ids(1, 2, 3))


def test_ambiguity_is_reported_between_surviving_representatives() -> None:
    a = candidate(1, 1, {0: FULL})
    b = candidate(5, 2, {0: FULL})  # merges into 1
    c = candidate(9, 3, {0: (0.5, 0.25, 0.5, 0.5)})  # partial overlap with both
    result = dedup_candidates([c, b, a], CONFIG)
    assert [row.duplicate_id for row in result.merges] == [UUID(int=5)]
    (overlap,) = result.ambiguous
    assert overlap.finding_ids == frozenset(ids(1, 9))


def test_unresolved_overlap_lifecycle() -> None:
    first = candidate(1, 1, {2: FULL})
    second = candidate(2, 2, {2: THIRD})
    result = dedup_candidates([first, second], CONFIG)
    flagged = list(apply_dedup([first.finding, second.finding], result))
    assert len(unresolved_overlaps(result.ambiguous, flagged)) == 1
    both_confirmed = [row.model_copy(update={"status": FindingStatus.VERIFIED, "severity": 3}) for row in flagged]
    assert len(unresolved_overlaps(result.ambiguous, both_confirmed)) == 1  # confirming both does not resolve
    assert not unresolved_overlaps(result.ambiguous, both_confirmed, [frozenset(ids(1, 2))])
    one_rejected = [flagged[0], flagged[1].model_copy(update={"status": FindingStatus.REJECTED})]
    assert not unresolved_overlaps(result.ambiguous, one_rejected)


# ------------------------------------------------------------ order and input

def test_input_order_does_not_change_the_result() -> None:
    rows = [candidate(1, 1, {0: FULL}), candidate(2, 2, {0: HALF_HEIGHT}), candidate(3, 3, {0: THIRD}),
            candidate(4, 1, {7: FAR}, track="solo")]
    expected = dedup_candidates(rows, CONFIG)
    for order in itertools.permutations(rows):
        assert dedup_candidates(order, CONFIG) == expected


def test_invalid_candidate_sets_are_rejected() -> None:
    row = candidate(1, 1, {0: FULL})
    with pytest.raises(ValueError):
        dedup_candidates([row, row], CONFIG)
    same_track = candidate(2, 1, {0: FULL}, track="t1")  # same concept run and track as `row`
    with pytest.raises(ValueError):
        dedup_candidates([row, same_track], CONFIG)
    foreign = TrackFootprint(finding=candidate(3, 2, {0: FULL}).finding.model_copy(update={"session_id": UUID(int=99)}),
                             frames=candidate(3, 2, {0: FULL}).frames)
    with pytest.raises(ValueError):
        dedup_candidates([row, foreign], CONFIG)
