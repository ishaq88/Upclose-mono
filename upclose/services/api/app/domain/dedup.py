"""Candidate de-duplication across prompts and groups.

Pure rules from contract section 5. Two candidates are compared only when they share a
part, a concept and a camera frame index, and come from different concept runs. A track
inside one concept run is already one candidate. Boxes on different frames are never
compared as if aligned, and nothing is merged across parts, tires or sides.

Thresholds are uncalibrated design defaults loaded from versioned config.
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from itertools import combinations
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.base import Confidence, ContractModel, Count
from app.contracts.enums import Concept, FindingStatus
from app.contracts.evidence import BoundingBox, Finding

from .config import HashedConfig

DUPLICATE_REVIEW_FLAG = "duplicate_review"
MERGED_FLAG = "dedup_merged"
# Pipeline bookkeeping flags; clients must not present them as condition risk flags.
INTERNAL_FLAGS = frozenset({DUPLICATE_REVIEW_FLAG, MERGED_FLAG})


class DedupConfig(HashedConfig):
    iou_threshold: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
    # Overlap strictly above this, but below the threshold, is ambiguous.
    ambiguous_min_iou_exclusive: Annotated[Decimal, Field(ge=0, lt=1, allow_inf_nan=False)]
    merge_on_known_component: bool

    @model_validator(mode="after")
    def ordered_thresholds(self) -> Self:
        if self.ambiguous_min_iou_exclusive >= self.iou_threshold:
            raise ValueError("ambiguity floor must be below the merge threshold")
        return self


# ------------------------------------------------------------------- tracks

class FrameBox(ContractModel):
    frame_index: Count
    bbox: BoundingBox
    confidence: Confidence | None = None


class TrackBox(ContractModel):
    """One per-frame observation of a provider track."""
    concept_run_id: UUID
    track_id: Annotated[str, Field(min_length=1, max_length=128)]
    frame_index: Count
    bbox: BoundingBox
    confidence: Confidence | None = None


class TrackGroup(ContractModel):
    """All observations of one track in one concept run: exactly one candidate."""
    concept_run_id: UUID
    track_id: str
    frames: tuple[FrameBox, ...]
    representative: FrameBox

    @model_validator(mode="after")
    def representative_is_observed(self) -> Self:
        if self.representative not in self.frames:
            raise ValueError("representative frame is not an observation of the track")
        return self


def _area(bbox: BoundingBox) -> float:
    return bbox.w * bbox.h


def _representative(frames: Sequence[FrameBox]) -> FrameBox:
    # Highest confidence (unknown lowest), then largest box, then earliest frame.
    return max(frames, key=lambda row: (-1.0 if row.confidence is None else row.confidence,
                                        _area(row.bbox), -row.frame_index))


def collapse_tracks(boxes: Iterable[TrackBox]) -> tuple[TrackGroup, ...]:
    """Group per-frame observations by (concept run, track): one candidate per track."""
    grouped: dict[tuple[UUID, str], list[FrameBox]] = defaultdict(list)
    for box in boxes:
        grouped[(box.concept_run_id, box.track_id)].append(
            FrameBox(frame_index=box.frame_index, bbox=box.bbox, confidence=box.confidence))
    groups: list[TrackGroup] = []
    for (run_id, track_id), frames in sorted(grouped.items(), key=lambda item: (str(item[0][0]), item[0][1])):
        if len({row.frame_index for row in frames}) != len(frames):
            raise ValueError("a track cannot have two observations on one frame")
        ordered = tuple(sorted(frames, key=lambda row: row.frame_index))
        groups.append(TrackGroup(concept_run_id=run_id, track_id=track_id, frames=ordered,
                                 representative=_representative(ordered)))
    return tuple(groups)


class TrackFootprint(ContractModel):
    """A candidate finding plus every frame its track was observed on."""
    finding: Finding
    frames: tuple[FrameBox, ...]

    @model_validator(mode="after")
    def consistent_footprint(self) -> Self:
        if self.finding.status != FindingStatus.CANDIDATE:
            raise ValueError("only candidates are de-duplicated")
        indices = [row.frame_index for row in self.frames]
        if not indices or len(set(indices)) != len(indices):
            raise ValueError("footprint requires unique observed frames")
        evidence = self.finding.evidence
        if not any(row.frame_index == evidence.frame_index and row.bbox == evidence.bbox for row in self.frames):
            raise ValueError("candidate evidence is not one of its track observations")
        return self


def footprint_for(finding: Finding, group: TrackGroup) -> TrackFootprint:
    if finding.concept_run_id != group.concept_run_id or finding.track_id != group.track_id:
        raise ValueError("finding does not belong to this track")
    return TrackFootprint(finding=finding, frames=group.frames)


# ---------------------------------------------------------------- geometry

def iou_exact(a: BoundingBox, b: BoundingBox) -> Fraction:
    """Exact IoU of two normalized boxes; boxes must come from the same frame."""
    ax, ay, aw, ah = (Fraction(value) for value in (a.x, a.y, a.w, a.h))
    bx, by, bw, bh = (Fraction(value) for value in (b.x, b.y, b.w, b.h))
    width = min(ax + aw, bx + bw) - max(ax, bx)
    height = min(ay + ah, by + bh) - max(ay, by)
    if width <= 0 or height <= 0:
        return Fraction(0)
    intersection = width * height
    return intersection / (aw * ah + bw * bh - intersection)


def _rounded(value: Fraction) -> Decimal:
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ results

class MergeProvenance(ContractModel):
    duplicate_id: UUID
    survivor_id: UUID  # stable lowest ID in the merged group
    part_id: UUID
    concept: Concept
    reason: Literal["iou", "same_component"]
    frame_index: Count
    iou: Decimal | None
    iou_threshold: Decimal
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class AmbiguousOverlap(ContractModel):
    """Candidates retained but not safely mergeable; the concept stays unresolved."""
    part_id: UUID
    concept: Concept
    finding_ids: frozenset[UUID]
    reason: Literal["partial_overlap", "non_transitive_merge"]
    frame_index: Count | None = None
    iou: Decimal | None = None

    @model_validator(mode="after")
    def at_least_two(self) -> Self:
        if len(self.finding_ids) < 2:
            raise ValueError("ambiguity needs at least two findings")
        return self


class DedupResult(ContractModel):
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    survivor_ids: tuple[UUID, ...]
    merges: tuple[MergeProvenance, ...]
    ambiguous: tuple[AmbiguousOverlap, ...]


@dataclass(frozen=True)
class _Edge:
    kind: Literal["merge", "ambiguous"]
    reason: Literal["iou", "same_component", "partial_overlap"]
    frame_index: int
    iou: Fraction


def _compare(a: TrackFootprint, b: TrackFootprint, config: DedupConfig) -> _Edge | None:
    left, right = a.finding, b.finding
    if left.part_id != right.part_id or left.concept != right.concept or left.concept_run_id == right.concept_run_id:
        return None
    if left.component_id is not None and right.component_id is not None and left.component_id != right.component_id:
        return None  # known different components (e.g. two tires) never merge
    boxes_a = {row.frame_index: row.bbox for row in a.frames}
    boxes_b = {row.frame_index: row.bbox for row in b.frames}
    shared = sorted(boxes_a.keys() & boxes_b.keys())
    if not shared:
        return None  # no common camera frame: nothing may be compared
    # Best overlap, earliest frame on ties; only same-frame boxes are ever compared.
    best_frame, best = max(((frame, iou_exact(boxes_a[frame], boxes_b[frame])) for frame in shared),
                           key=lambda item: (item[1], -item[0]))
    threshold = Fraction(config.iou_threshold)
    if best >= threshold:
        return _Edge("merge", "iou", best_frame, best)
    if config.merge_on_known_component and left.component_id is not None and left.component_id == right.component_id:
        return _Edge("merge", "same_component", best_frame, best)
    if best > Fraction(config.ambiguous_min_iou_exclusive):
        return _Edge("ambiguous", "partial_overlap", best_frame, best)
    return None


def _validate(footprints: Sequence[TrackFootprint]) -> None:
    findings = [row.finding for row in footprints]
    if len({row.id for row in findings}) != len(findings):
        raise ValueError("duplicate candidate ID")
    if len({row.session_id for row in findings}) > 1:
        raise ValueError("candidates belong to different sessions")
    if len({(row.concept_run_id, row.track_id) for row in findings}) != len(findings):
        raise ValueError("a track must be one candidate; collapse tracks first")


def _components(ids: Sequence[UUID], pairs: Iterable[tuple[UUID, UUID]]) -> list[list[UUID]]:
    parent = {value: value for value in ids}

    def find(value: UUID) -> UUID:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    for left, right in pairs:
        parent[find(left)] = find(right)
    groups: dict[UUID, list[UUID]] = defaultdict(list)
    for value in ids:
        groups[find(value)].append(value)
    return [sorted(members) for members in groups.values()]


def dedup_candidates(footprints: Sequence[TrackFootprint], config: DedupConfig) -> DedupResult:
    """Merge same-object candidates across prompts; flag uncertain overlap. Order-independent."""
    _validate(footprints)
    ordered = sorted(footprints, key=lambda row: row.finding.id)
    by_id = {row.finding.id: row for row in ordered}
    edges: dict[tuple[UUID, UUID], _Edge] = {}
    for left, right in combinations(ordered, 2):
        edge = _compare(left, right, config)
        if edge is not None:
            edges[(left.finding.id, right.finding.id)] = edge  # ids ascending within a pair
    merge_edges = {pair: edge for pair, edge in edges.items() if edge.kind == "merge"}

    survivor_of: dict[UUID, UUID] = {value: value for value in by_id}
    merges: list[MergeProvenance] = []
    ambiguous: list[AmbiguousOverlap] = []
    for members in _components(sorted(by_id), merge_edges):
        if len(members) < 2:
            continue
        # Chain merging is unsafe: A~B and B~C do not imply A~C. Require every pair to match.
        if all(pair in merge_edges for pair in combinations(members, 2)):
            winner = members[0]
            for duplicate in members[1:]:
                edge = merge_edges[(winner, duplicate)]
                survivor_of[duplicate] = winner
                merges.append(MergeProvenance(
                    duplicate_id=duplicate, survivor_id=winner, part_id=by_id[winner].finding.part_id,
                    concept=by_id[winner].finding.concept, reason=edge.reason, frame_index=edge.frame_index,
                    iou=_rounded(edge.iou), iou_threshold=config.iou_threshold, config_hash=config.content_hash))
        else:
            first = by_id[members[0]].finding
            ambiguous.append(AmbiguousOverlap(part_id=first.part_id, concept=first.concept,
                                              finding_ids=frozenset(members), reason="non_transitive_merge"))

    pair_edges: dict[frozenset[UUID], _Edge] = {}
    for (left, right), edge in edges.items():
        if edge.kind != "ambiguous" or survivor_of[left] == survivor_of[right]:
            continue
        key = frozenset((survivor_of[left], survivor_of[right]))
        if key not in pair_edges or (edge.iou, -edge.frame_index) > (pair_edges[key].iou, -pair_edges[key].frame_index):
            pair_edges[key] = edge
    for key, edge in pair_edges.items():
        first = by_id[min(key)].finding
        ambiguous.append(AmbiguousOverlap(part_id=first.part_id, concept=first.concept, finding_ids=key,
                                          reason="partial_overlap", frame_index=edge.frame_index, iou=_rounded(edge.iou)))

    return DedupResult(
        config_hash=config.content_hash,
        survivor_ids=tuple(sorted(value for value, winner in survivor_of.items() if value == winner)),
        merges=tuple(sorted(merges, key=lambda row: row.duplicate_id)),
        ambiguous=tuple(sorted(ambiguous, key=lambda row: sorted(row.finding_ids))))


def apply_dedup(findings: Sequence[Finding], result: DedupResult) -> tuple[Finding, ...]:
    """Merged duplicates become rejected links; survivors keep merged flags and review marks."""
    by_id = {row.id: row for row in findings}
    if set(by_id) != set(result.survivor_ids) | {row.duplicate_id for row in result.merges} or len(by_id) != len(findings):
        raise ValueError("findings do not match the dedup result")
    survivor_of = {row.duplicate_id: row.survivor_id for row in result.merges}
    flags: dict[UUID, set[str]] = {key: set(row.flags) for key, row in by_id.items()}
    for duplicate_id, survivor_id in survivor_of.items():
        flags[survivor_id] |= {flag for flag in by_id[duplicate_id].flags if flag not in INTERNAL_FLAGS}
        flags[duplicate_id].add(MERGED_FLAG)
    for overlap in result.ambiguous:
        for finding_id in overlap.finding_ids:
            flags[finding_id].add(DUPLICATE_REVIEW_FLAG)
    updated: list[Finding] = []
    for finding_id in sorted(by_id):
        fields = by_id[finding_id].model_dump()
        fields["flags"] = tuple(sorted(flags[finding_id]))
        if finding_id in survivor_of:
            fields.update(status=FindingStatus.REJECTED, duplicate_of=survivor_of[finding_id])
        updated.append(Finding.model_validate(fields))
    return tuple(updated)


def unresolved_overlaps(ambiguous: Collection[AmbiguousOverlap], findings: Sequence[Finding],
                        adjudicated_distinct: Collection[frozenset[UUID]] = ()) -> tuple[AmbiguousOverlap, ...]:
    """Ambiguities still blocking a complete assessment.

    An overlap is resolved once at most one member is still live (not rejected), for example
    after the verifier rejects one as a duplicate or artifact, or when the caller records an
    explicit adjudication that the group members are distinct objects. Confirming every
    member without a link does not resolve it.
    """
    status = {row.id: row.status for row in findings}
    distinct = set(adjudicated_distinct)
    open_overlaps = []
    for overlap in ambiguous:
        live = [fid for fid in overlap.finding_ids if status.get(fid) != FindingStatus.REJECTED]
        if len(live) > 1 and overlap.finding_ids not in distinct:
            open_overlaps.append(overlap)
    return tuple(sorted(open_overlaps, key=lambda row: sorted(row.finding_ids)))
