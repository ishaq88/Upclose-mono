"""Frozen report inputs (T07b): the immutable manifest, its builder/loader, deterministic stage
computations and the scope-filtered DTO builders T08a wraps.

Invariants (AGENTS.md 2, 3, 5, 8):

- The manifest is built ONCE per report revision, under the session and revision locks, from the
  persisted findings/coverage/part rows. Every later stage reads only the manifest plus the stored
  stage outputs, never mutable finding rows. `manifest_hash` is the SHA-256 of the canonical JSON of the
  stored manifest and is re-verified on every load.
- Scoring, repair grouping, claims and narration are pure functions of (manifest, config); the stored
  score payload embeds the manifest and config hashes, so a replay over the same snapshot and config is
  byte-identical.
- `build_free_report` reads Part-1 evidence and the Part-1 narration only; other zones contribute zone
  totals. `build_paid_report` is the only path that carries other-zone findings, repairs, claims and the
  full narration, and T08a must call it only from an active-entitlement branch.

Manifest `report-manifest-v1` (all values JSON; no timestamps, so a rebuild is byte-identical):

    schema_version, session_id, report_id, revision_id, revision_no
    finalized_selection   part selection recorded at finalize (carried over, not recomputed)
    provenance            synthetic | cached | live, derived from the runs' provider identities
    listing               {version, text, text_sha256}  frozen listing (untrusted text; purge scrubs it)
    parts[8]              per part: ids, state, raw_sha256 (content hash), failure_reason, quality_usable,
                          assess_run_id, run input_hash, run config_hashes, concept_runs, coverage and the
                          verified / unverified / rejected findings as contract DTOs
    configs               content hashes of the claims, narration, scoring and cost configs, the render
                          source digest, the pin-anchor version and the report-chain version
    code                  {digest, method}: source digest of app/ and config/ (outside Git; see `source_digest`)
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession

from app.contracts.base import ContractModel, ObjectKey, PartNumber
from app.contracts.enums import (PART_ZONES, ConceptRunState, FindingStatus, PartState,
                                 ReportState, Zone)
from app.contracts.evidence import ClaimResult, ConceptRun as ConceptRunContract, Coverage, Finding, NarratedStatement
from app.contracts.reports import (CandidateFinding, FreeFinding, FreeReport, LockedZoneSummary, PaidFinding, PaidReport, PublicClaim,
                                   PublicCoverage)
from app.contracts.scoring import RepairSummary, ScoreResult, ScoringInput
from app.contracts.sources import SOURCE_KINDS, FreeSourceCheck, PaidSourceCheck, SourceRecord
from app.contracts.video import VideoRange
from app.domain.claims import ClaimsConfig, eligible_findings, load_claims_config, resolve_claims
from app.domain.config import CostConfig, ScoringConfig, load_cost_config, load_scoring_config
from app.domain.dedup import INTERNAL_FLAGS
from app.domain.narration import NarrationConfig, NarrationOutcome, load_narration_config, part1_summary_text, narration_hash, resolve_part1_narration, resolve_full_narration
from app.domain.repairs import estimate_repairs
from app.domain.scoring import score_assessment
from app.models import (Artifact, Claim, ClaimEvidence, Coverage as CoverageRow, ConceptRun as ConceptRunRow,
                        Finding as FindingRow, EvidenceSource, Part, Report, ReportRevision, RevisionFinding, Session, VideoUpload)
from app.pipeline.orchestration import (CONFIG_DIR, FIXTURE_PROVIDERS, MASKED_FIXTURE_PROVIDERS, REPORT_CHAIN_VERSION, TERMINAL_PART_STATES,
                                        assessment_run, canonical_hash, to_contract_finding)
from app.pipeline.media import FrameManifest
from app.pipeline.vehicle_mask_contract import MaskedInput, load_vehicle_mask_marker
from app.report.anchors import ANCHOR_VERSION

MANIFEST_SCHEMA = "report-manifest-v1"
SCORE_KIND = "score-result-v1"
FREE_NARRATION_KIND = "free-narration-v1"
PAID_NARRATION_KIND = "paid-narration-v1"
FREE_ZONE = Zone.DRIVER_SIDE

Sha = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
Provenance = Literal["synthetic", "cached", "live"]

# Provider identities of cached replays of REAL evidence (T10b registers them). Anything that is neither a
# fixture identity nor registered here is labelled live, never synthetic or cached.
CACHED_PROVIDER_HASHES: frozenset[str] = frozenset()


class SnapshotNotReady(RuntimeError):
    """The session's parts are not all terminal; the barrier should have prevented this."""


class SnapshotIntegrityError(ValueError):
    """Stored manifest does not match its hash, or stage output disagrees with the manifest/config."""


class SnapshotIncomplete(ValueError):
    """A stage output a DTO needs has not been stored yet."""


class ReadyRevisionImmutable(RuntimeError):
    """Service guard: a revision that is not `building` is never written again."""


def require_building(revision: ReportRevision) -> None:
    if revision.state != ReportState.BUILDING:
        raise ReadyRevisionImmutable("only a building revision can be written")


# --------------------------------------------------------------------------- configs and digests


@dataclass(frozen=True, slots=True)
class ReportConfig:
    claims: ClaimsConfig
    narration: NarrationConfig
    scoring: ScoringConfig
    costs: CostConfig
    frozen_identity: ManifestConfigs | None = None

    def manifest_configs(self) -> ManifestConfigs:
        if self.frozen_identity is not None:
            return self.frozen_identity
        return ManifestConfigs(claims=self.claims.content_hash, narration=self.narration.content_hash,
                               scoring=self.scoring.content_hash, costs=self.costs.content_hash,
                               render=render_digest(), anchor_version=ANCHOR_VERSION,
                               chain_version=REPORT_CHAIN_VERSION)


@lru_cache(maxsize=1)
def load_report_config() -> ReportConfig:
    directory = Path(CONFIG_DIR)
    return ReportConfig(claims=load_claims_config(directory / "claims.v1.json"),
                        narration=load_narration_config(directory / "narration.v1.json"),
                        scoring=load_scoring_config(directory / "scoring.v1.json"),
                        costs=load_cost_config(directory / "costs.v1.json"))


_API_ROOT = Path(__file__).resolve().parents[2]
_DIGEST_SUFFIXES = frozenset({".py", ".html", ".css", ".js", ".json", ".svg"})
CODE_DIGEST_METHOD = ("sha256 of canonical JSON [[posix path relative to services/api, sha256 of bytes with CRLF folded to LF]]"
                      " for every .py/.html/.css/.js/.json/.svg file under app/ and config/, sorted by path,"
                      " excluding __pycache__; there is no Git checkout to name a revision")


def _tree_digest(roots: Iterable[Path]) -> str:
    entries: list[list[str]] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in _DIGEST_SUFFIXES:
                data = path.read_bytes().replace(b"\r\n", b"\n")
                entries.append([path.relative_to(_API_ROOT).as_posix(), hashlib.sha256(data).hexdigest()])
    return canonical_hash(sorted(entries))


@lru_cache(maxsize=1)
def source_digest() -> str:
    """Code revision outside Git: digest of the application source and config (see CODE_DIGEST_METHOD)."""
    return _tree_digest((_API_ROOT / "app", _API_ROOT / "config"))


@lru_cache(maxsize=1)
def render_digest() -> str:
    """Digest of everything that shapes the rendered report (renderer, anchors, templates, CSS)."""
    app = _API_ROOT / "app"
    return _tree_digest((app / "report", app / "templates" / "report", app / "static" / "report"))


# --------------------------------------------------------------------------- manifest models


class ManifestListing(ContractModel):
    version: Annotated[int, Field(strict=True, ge=1)]
    text: Annotated[str, Field(max_length=10000)] | None
    text_sha256: Sha


class ManifestConfigs(ContractModel):
    claims: Sha
    narration: Sha
    scoring: Sha
    costs: Sha
    render: Sha
    anchor_version: Annotated[str, Field(min_length=1, max_length=128)]
    chain_version: Annotated[str, Field(min_length=1, max_length=128)]


class ManifestConfigContent(ContractModel):
    claims: ClaimsConfig
    narration: NarrationConfig
    scoring: ScoringConfig
    costs: CostConfig


class ManifestCode(ContractModel):
    digest: Sha
    method: str


class ManifestSourceFrames(ContractModel):
    source_upload_id: UUID
    source_sha256: Sha
    start_ms: Annotated[int, Field(strict=True, ge=0, lt=180000)]
    end_ms: Annotated[int, Field(strict=True, gt=0, le=180000)]
    mapping_basis: Literal["user_declared"]
    frame_timestamps_ms: Annotated[tuple[Annotated[int, Field(strict=True, ge=0, le=180250)], ...], Field(min_length=1, max_length=3600)]

    @model_validator(mode="after")
    def coherent_source_frames(self) -> Self:
        if not 0 < self.end_ms - self.start_ms <= 60000:
            raise ValueError("invalid source interval")
        times = self.frame_timestamps_ms
        if times[0] < self.start_ms or times[-1] >= self.end_ms:
            raise ValueError("source frame outside its marked interval")
        if any(first >= second for first, second in zip(times, times[1:])):
            raise ValueError("source times must strictly increase")
        return self


class ManifestSourceVideo(ContractModel):
    source_upload_id: UUID
    source_sha256: Sha | None
    duration_ms: Annotated[int, Field(strict=True, gt=0, le=180250)] | None
    processing_state: Literal["ready", "failed"]
    reason: Annotated[str, Field(max_length=240)] | None
    segments: tuple[VideoRange, ...]


class ManifestMaskedFrame(ContractModel):
    frame_key: ObjectKey
    frame_sha256: Sha
    frame_index: Annotated[int, Field(strict=True, ge=0)]
    timestamp_ms: Annotated[int, Field(strict=True, ge=0)]


class ManifestVehicleMask(ContractModel):
    """Compact private provenance; provider masks and full source manifest stay private artifacts."""
    version: Literal["vehicle-mask-v1"] = "vehicle-mask-v1"
    marker_sha256: Sha
    input_hash: Sha
    config_hash: Sha
    source_manifest_sha256: Sha
    width: Annotated[int, Field(strict=True, gt=0, le=3840)]
    height: Annotated[int, Field(strict=True, gt=0, le=3840)]
    media: Annotated[tuple[MaskedInput, ...], Field(min_length=1, max_length=32)]
    evidence_frames: tuple[ManifestMaskedFrame, ...]

    @model_validator(mode="after")
    def coherent_mapping(self) -> Self:
        kinds = {item.kind for item in self.media}
        if len(kinds) != 1 or ("video" in kinds and len(self.media) != 1):
            raise ValueError("frozen masked media mode is ambiguous")
        if "video" in kinds and self.media[0].frame_indices != tuple(range(len(self.media[0].frame_indices))):
            raise ValueError("frozen masked video must contain every source frame")
        sources = [item.source_key for item in self.media]
        artifacts = [key for item in self.media for key in (item.masked_key, item.mask_key)]
        if len(set(sources)) != len(sources) or len(set(artifacts)) != len(artifacts) or set(sources) & set(artifacts):
            raise ValueError("frozen masked artifacts must be distinct")
        frames: dict[int, tuple[int, str | None]] = {}
        for item in self.media:
            for index, timestamp in zip(item.frame_indices, item.timestamps_ms):
                if index in frames:
                    raise ValueError("frozen masked frame has ambiguous membership")
                frames[index] = (timestamp, item.source_key if item.kind == "image" else None)
        keys: set[str] = set()
        for frame in self.evidence_frames:
            mapping = frames.get(frame.frame_index)
            if (frame.frame_key in keys or mapping is None or frame.timestamp_ms != mapping[0]
                    or (mapping[1] is not None and frame.frame_key != mapping[1])):
                raise ValueError("frozen evidence frame lacks exact masked membership")
            keys.add(frame.frame_key)
        return self


class ManifestPart(ContractModel):
    part_id: UUID
    part_no: PartNumber
    zone: Zone
    state: PartState
    raw_sha256: Sha | None
    failure_reason: Annotated[str, Field(max_length=240)] | None
    quality_usable: bool | None
    source_video: ManifestSourceFrames | None = None
    vehicle_mask: ManifestVehicleMask | None = None
    assess_run_id: UUID | None
    input_hash: Sha | None
    config_hashes: dict[str, str]
    concept_runs: tuple[ConceptRunContract, ...]
    coverage: tuple[Coverage, ...]
    verified: tuple[Finding, ...]
    unverified: tuple[Finding, ...]
    rejected: tuple[Finding, ...]

    @model_validator(mode="after")
    def coherent_part(self) -> Self:
        if self.zone != PART_ZONES[self.part_no - 1]:
            raise ValueError("part number and zone disagree")
        if self.state not in TERMINAL_PART_STATES:
            raise ValueError("a snapshot only contains terminal parts")
        buckets = ((self.verified, {FindingStatus.VERIFIED}), (self.rejected, {FindingStatus.REJECTED}),
                   (self.unverified, {FindingStatus.UNVERIFIED, FindingStatus.CANDIDATE}))
        for findings, allowed in buckets:
            if any(row.status not in allowed or row.part_id != self.part_id for row in findings):
                raise ValueError("finding is in the wrong bucket or part")
            if self.source_video is not None and any(
                row.evidence.frame_index >= len(self.source_video.frame_timestamps_ms) for row in findings
            ):
                raise ValueError("finding frame is outside its frozen source timeline")
        if self.assess_run_id is None and (self.coverage or self.concept_runs or self.verified or self.unverified or self.rejected):
            raise ValueError("evidence requires an assessment run")
        requires_mask = "vehicle_mask" in self.config_hashes
        has_results = bool(self.findings) or any(row.state == ConceptRunState.SUCCEEDED for row in self.concept_runs)
        if requires_mask and has_results and self.vehicle_mask is None:
            raise ValueError("masked assessment results require frozen mask provenance")
        if self.vehicle_mask is not None:
            binding = self.vehicle_mask
            hashes = {key: value for key, value in self.config_hashes.items() if key != "media"}
            if (not requires_mask or self.assess_run_id is None or binding.input_hash != self.input_hash
                    or binding.config_hash != canonical_hash(hashes)):
                raise ValueError("frozen mask provenance differs from assessment identity")
            frames = {frame.frame_key: frame for frame in binding.evidence_frames}
            if set(frames) != {finding.evidence.frame_key for finding in self.findings}:
                raise ValueError("frozen mask evidence membership differs from findings")
            for finding in self.findings:
                evidence = finding.evidence
                if (frames[evidence.frame_key].frame_index != evidence.frame_index
                        or (evidence.frame_width, evidence.frame_height) != (binding.width, binding.height)):
                    raise ValueError("finding differs from its exact masked source frame")
        return self

    @property
    def findings(self) -> tuple[Finding, ...]:
        return (*self.verified, *self.unverified, *self.rejected)


class ReportManifest(ContractModel):
    schema_version: Literal["report-manifest-v1"] = MANIFEST_SCHEMA
    session_id: UUID
    report_id: UUID
    revision_id: UUID
    revision_no: Annotated[int, Field(strict=True, ge=1)]
    finalized_selection: dict[str, object]
    provenance: Provenance
    listing: ManifestListing
    parts: tuple[ManifestPart, ...]
    configs: ManifestConfigs
    config_content: ManifestConfigContent
    code: ManifestCode
    source_video: ManifestSourceVideo | None = None
    sources: tuple[SourceRecord, ...] = ()

    @model_validator(mode="after")
    def complete_and_owned(self) -> Self:
        if [row.part_no for row in self.parts] != list(range(1, 9)):
            raise ValueError("manifest requires parts 1..8 in order")
        for name in ("claims", "narration", "scoring", "costs"):
            if getattr(self.config_content, name).content_hash != getattr(self.configs, name):
                raise ValueError("embedded configuration does not match its frozen hash")
        if hashlib.sha256((self.listing.text or "").encode("utf-8")).hexdigest() != self.listing.text_sha256:
            raise ValueError("listing does not match its frozen hash")
        kinds = [row.kind for row in self.sources]
        if kinds != sorted(set(kinds)):
            raise ValueError("sources must be unique per kind and ordered by kind")
        seen: set[UUID] = set()
        for part in self.parts:
            if part.source_video is not None:
                if (self.source_video is None
                    or part.source_video.source_upload_id != self.source_video.source_upload_id
                    or part.source_video.source_sha256 != self.source_video.source_sha256
                    or not any(section.part_no == part.part_no
                               and section.start_ms == part.source_video.start_ms
                               and section.end_ms == part.source_video.end_ms
                               for section in self.source_video.segments)):
                    raise ValueError("part source provenance disagrees with the frozen source")
                if part.quality_usable is True or any(row.state.value == "assessed" for row in part.coverage):
                    raise ValueError("user-declared mapping cannot establish complete coverage")
            for finding in part.findings:
                if finding.session_id != self.session_id or finding.id in seen:
                    raise ValueError("finding is foreign or duplicated")
                if part.assess_run_id is None or finding.run_id != part.assess_run_id:
                    raise ValueError("finding is outside its part's run")
                seen.add(finding.id)
        return self

    def part(self, part_no: int) -> ManifestPart:
        return self.parts[part_no - 1]

    @property
    def all_findings(self) -> tuple[Finding, ...]:
        return tuple(row for part in self.parts for row in part.findings)


def evidence_input(manifest: ReportManifest, parts: Sequence[int] | None = None) -> ScoringInput:
    """Frozen `ScoringInput` over the manifest (all parts, or only the listed part numbers)."""
    chosen = [part for part in manifest.parts if parts is None or part.part_no in parts]
    return ScoringInput(session_id=manifest.session_id,
                        run_ids=frozenset(part.assess_run_id for part in chosen if part.assess_run_id is not None),
                        findings=tuple(row for part in chosen for row in part.findings),
                        coverage=tuple(row for part in chosen for row in part.coverage))


# --------------------------------------------------------------------------- manifest build / freeze / load


def provenance_label(config_hashes: Iterable[Mapping[str, str]], default: Provenance) -> Provenance:
    """Derived from the runs' frozen provider identities; `default` applies only when no run exists."""
    labels: set[Provenance] = set()
    for hashes in config_hashes:
        identities = {hashes.get("segmenter"), hashes.get("verifier")}
        fixture_identities = set(FIXTURE_PROVIDERS.values()) | set(MASKED_FIXTURE_PROVIDERS.values())
        if identities <= fixture_identities:
            labels.add("synthetic")
        elif identities - fixture_identities <= CACHED_PROVIDER_HASHES:
            labels.add("cached")
        else:
            labels.add("live")
    for label in ("live", "cached", "synthetic"):
        if label in labels:
            return label  # type: ignore[return-value]
    return default


def _manifest_part(db: DBSession, part: Part) -> tuple[ManifestPart, Mapping[str, str] | None]:
    if part.state not in TERMINAL_PART_STATES:
        raise SnapshotNotReady("every part must be terminal before the snapshot")
    run = assessment_run(db, part.id, lock=False)
    verified: list[Finding] = []
    unverified: list[Finding] = []
    rejected: list[Finding] = []
    concept_runs: list[ConceptRunContract] = []
    coverage: list[Coverage] = []
    if run is not None:
        for row in db.scalars(select(FindingRow).where(FindingRow.run_id == run.id, FindingRow.part_id == part.id).order_by(FindingRow.id)):
            finding = to_contract_finding(row, part)
            bucket = (verified if finding.status == FindingStatus.VERIFIED else
                      rejected if finding.status == FindingStatus.REJECTED else unverified)
            bucket.append(finding)
        for row in db.scalars(select(ConceptRunRow).where(ConceptRunRow.run_id == run.id).order_by(ConceptRunRow.prompt_id)):
            try:
                state = ConceptRunState(row.state)
            except ValueError as error:
                raise SnapshotNotReady("a concept run is not terminal") from error
            concept_runs.append(ConceptRunContract(id=row.id, run_id=row.run_id, part_id=row.part_id, zone=part.zone,
                                                   concept=row.concept, prompt_id=row.prompt_id, state=state, reason=row.reason))
        for row in db.scalars(select(CoverageRow).where(CoverageRow.run_id == run.id).order_by(CoverageRow.zone, CoverageRow.concept)):
            coverage.append(Coverage(run_id=row.run_id, zone=row.zone, concept=row.concept, state=row.state, reason=row.reason))
    usable = (part.quality or {}).get("usable_for_scoring")
    source_metadata = (part.quality or {}).get("source_video")
    source_frames = None if source_metadata is None else ManifestSourceFrames.model_validate(source_metadata)
    if source_frames is not None and part.source_video_id != source_frames.source_upload_id:
        raise SnapshotIntegrityError("source timeline does not belong to this part")
    mask_binding = None
    findings = (*verified, *unverified, *rejected)
    has_results = bool(findings) or any(row.state == ConceptRunState.SUCCEEDED for row in concept_runs)
    if run is not None and "vehicle_mask" in run.config_hashes and has_results:
        try:
            marker = load_vehicle_mask_marker(db, run)
            timeline = FrameManifest.from_json(json.dumps(marker.source_manifest))
            frame_keys = {finding.evidence.frame_key for finding in findings}
            artifacts = {row.key: row for row in db.scalars(select(Artifact).where(
                Artifact.session_id == part.session_id, Artifact.key.in_(frame_keys), Artifact.kind == "frame"))}
            if set(artifacts) != frame_keys:
                raise ValueError("frozen original evidence artifact is missing")
            evidence_frames: dict[str, ManifestMaskedFrame] = {}
            for finding in findings:
                evidence = finding.evidence
                if not 0 <= evidence.frame_index < timeline.frame_count:
                    raise ValueError("finding is outside original mask timeline")
                frame = ManifestMaskedFrame(frame_key=evidence.frame_key, frame_sha256=artifacts[evidence.frame_key].sha256,
                    frame_index=evidence.frame_index, timestamp_ms=round(timeline.time_seconds(evidence.frame_index) * 1000))
                if evidence.frame_key in evidence_frames and evidence_frames[evidence.frame_key] != frame:
                    raise ValueError("original evidence key refers to different frames")
                evidence_frames[evidence.frame_key] = frame
            mask_binding = ManifestVehicleMask(marker_sha256=canonical_hash(marker.model_dump(mode="json")),
                input_hash=marker.input_hash, config_hash=marker.config_hash,
                source_manifest_sha256=marker.source_manifest_sha256, width=timeline.width, height=timeline.height,
                media=marker.media, evidence_frames=tuple(evidence_frames[key] for key in sorted(evidence_frames)))
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise SnapshotIntegrityError("invalid persisted vehicle mask provenance") from error
    record = ManifestPart(
        part_id=part.id, part_no=part.part_no, zone=part.zone, state=part.state, raw_sha256=part.raw_sha256,
        failure_reason=part.failure_reason, quality_usable=usable if isinstance(usable, bool) else None,
        source_video=source_frames, vehicle_mask=mask_binding,
        assess_run_id=None if run is None else run.id, input_hash=None if run is None else run.input_hash,
        config_hashes={} if run is None else {key: str(value) for key, value in run.config_hashes.items()},
        concept_runs=tuple(concept_runs), coverage=tuple(coverage), verified=tuple(verified),
        unverified=tuple(unverified), rejected=tuple(rejected))
    return record, None if run is None else record.config_hashes


def _manifest_sources(db: DBSession, session_id: UUID) -> tuple[SourceRecord, ...]:
    """Plugin evidence frozen with the report; raw data stays in its row, bound by a re-verified hash."""
    records: list[SourceRecord] = []
    for row in db.scalars(select(EvidenceSource).where(EvidenceSource.session_id == session_id)
                          .order_by(EvidenceSource.kind)):
        if canonical_hash(row.data) != row.data_sha256:
            raise SnapshotIntegrityError("evidence source data does not match its stored hash")
        summary = row.summary
        if summary.get("state") == "processing":  # processing never completed: freeze it as not assessed
            summary = {**summary, "state": "not_assessed", "reason": "Audio processing did not complete."}
        records.append(SourceRecord(kind=row.kind, provenance=row.provenance, device=row.device,
                                    captured_at=row.captured_at, data_sha256=row.data_sha256, summary=summary))
    return tuple(records)


def build_manifest(db: DBSession, revision: ReportRevision, config: ReportConfig, default_provenance: Provenance) -> ReportManifest:
    """Read the persisted evidence into a manifest. Caller holds the session and revision locks.

    Pure function of the persisted rows: calling it twice with no change in between returns equal manifests."""
    owner = db.get(Session, revision.session_id)
    if owner is None:
        raise SnapshotNotReady("session is missing")
    parts = list(db.scalars(select(Part).where(Part.session_id == owner.id).order_by(Part.part_no)))
    records = [_manifest_part(db, part) for part in parts]
    text = owner.listing_text
    selection = dict((revision.manifest or {}).get("finalized_selection") or {})
    source = db.scalar(select(VideoUpload).where(VideoUpload.session_id == owner.id, VideoUpload.state == "accepted"))
    frozen_source = None
    if source is not None:
        if source.processing_state not in ("ready", "failed"):
            raise SnapshotNotReady("source video is still processing")
        frozen_source = ManifestSourceVideo(source_upload_id=source.id, source_sha256=source.raw_sha256,
            duration_ms=source.duration_ms, processing_state=source.processing_state, reason=source.reason,
            segments=tuple(VideoRange.model_validate(item) for item in source.segments))
    return ReportManifest(
        session_id=owner.id, report_id=revision.report_id, revision_id=revision.id, revision_no=revision.revision_no,
        finalized_selection=selection,
        source_video=frozen_source,
        sources=_manifest_sources(db, owner.id),
        provenance=provenance_label((hashes for _, hashes in records if hashes is not None), default_provenance),
        listing=ManifestListing(version=owner.listing_version, text=text,
                                text_sha256=hashlib.sha256((text or "").encode("utf-8")).hexdigest()),
        parts=tuple(record for record, _ in records), configs=config.manifest_configs(),
        config_content=ManifestConfigContent(claims=config.claims, narration=config.narration, scoring=config.scoring, costs=config.costs),
        code=ManifestCode(digest=source_digest(), method=CODE_DIGEST_METHOD))


def manifest_payload(manifest: ReportManifest) -> dict[str, object]:
    payload = manifest.model_dump(mode="json")
    # Additive provenance must not change the canonical bytes of pre-T12 frozen reports.
    if manifest.source_video is None:
        payload.pop("source_video", None)
    if not manifest.sources:
        payload.pop("sources", None)
    for source in payload.get("sources", ()):
        if source["summary"].get("model_verdict") is None:
            source["summary"].pop("model_verdict", None)  # keeps frozen manifests from before model verdicts byte-identical
    for part in payload["parts"]:
        if part.get("source_video") is None:
            part.pop("source_video", None)
        if part.get("vehicle_mask") is None:
            part.pop("vehicle_mask", None)
    return payload


def is_frozen(revision: ReportRevision) -> bool:
    return revision.manifest_hash is not None


def freeze_manifest(db: DBSession, revision: ReportRevision, manifest: ReportManifest) -> str:
    """Store the manifest and its hash on a building revision and record every referenced finding.

    Idempotent for an identical manifest; a frozen revision is never overwritten with different content."""
    require_building(revision)
    payload = manifest_payload(manifest)
    digest = canonical_hash(payload)
    if is_frozen(revision):
        if revision.manifest_hash != digest:
            raise SnapshotIntegrityError("revision is already frozen with a different manifest")
        return digest
    revision.manifest = payload
    revision.manifest_hash = digest
    db.flush()
    for finding in manifest.all_findings:
        db.execute(insert(RevisionFinding).values(revision_id=revision.id, finding_id=finding.id, session_id=revision.session_id)
                   .on_conflict_do_nothing(index_elements=[RevisionFinding.revision_id, RevisionFinding.finding_id]))
    db.flush()
    return digest


def load_manifest(revision: ReportRevision) -> ReportManifest:
    """Verified, typed manifest of a frozen revision (hash and identity are re-checked)."""
    if not is_frozen(revision) or revision.manifest is None:
        raise SnapshotIncomplete("revision has no frozen manifest")
    if canonical_hash(revision.manifest) != revision.manifest_hash:
        raise SnapshotIntegrityError("stored manifest does not match its hash")
    manifest = ReportManifest.model_validate(revision.manifest)
    if (manifest.revision_id != revision.id or manifest.session_id != revision.session_id
            or manifest.report_id != revision.report_id or manifest.revision_no != revision.revision_no):
        raise SnapshotIntegrityError("manifest belongs to another revision")
    return manifest


def check_configs(manifest: ReportManifest, config: ReportConfig) -> None:
    """Invariant 8: a stage never computes with configs other than the ones the snapshot froze."""
    if (manifest.configs != config.manifest_configs() or any(
            getattr(config, name).content_hash != getattr(manifest.configs, name)
            for name in ("claims", "narration", "scoring", "costs"))):
        raise SnapshotIntegrityError("loaded configuration differs from the configuration frozen in the manifest")


# --------------------------------------------------------------------------- deterministic stage outputs


class StoredScore(ContractModel):
    """`report_revisions.score_result`: scores plus repairs, bound to the manifest and config hashes."""
    kind: Literal["score-result-v1"] = SCORE_KIND
    manifest_hash: Sha
    scoring_config_hash: Sha
    cost_config_hash: Sha
    scores: ScoreResult
    repairs: RepairSummary


def compute_score(manifest: ReportManifest, manifest_hash: str, config: ReportConfig) -> StoredScore:
    check_configs(manifest, config)
    if canonical_hash(manifest_payload(manifest)) != manifest_hash:
        raise SnapshotIntegrityError("score manifest does not match its hash")
    inputs = evidence_input(manifest)
    return StoredScore(manifest_hash=manifest_hash, scoring_config_hash=config.scoring.content_hash,
                       cost_config_hash=config.costs.content_hash,
                       scores=score_assessment(inputs, config.scoring), repairs=estimate_repairs(inputs, config.costs))


def score_payload(score: StoredScore) -> dict[str, object]:
    return score.model_dump(mode="json")


class StoredFreeNarration(ContractModel):
    kind: Literal["free-narration-v1"] = FREE_NARRATION_KIND
    manifest_hash: Sha
    outcome: NarrationOutcome


def claims_digest(claims: Sequence[ClaimResult]) -> str:
    """Association rows have set semantics; digest their canonical claim ordering."""
    records = []
    for claim in sorted(claims, key=lambda row: row.claim_index):
        record = claim.model_dump(mode="json")
        record["finding_ids"] = sorted(record["finding_ids"])
        record["evidence_frame_keys"] = sorted(set(record["evidence_frame_keys"]))
        records.append(record)
    return canonical_hash(records)


class ClaimsMeta(ContractModel):
    source: Literal["deterministic", "model", "fallback"]
    notes: tuple[Annotated[str, Field(max_length=200)], ...]
    errors: tuple[str, ...]
    config_hash: Sha
    listing_hash: Sha
    claim_count: Annotated[int, Field(strict=True, ge=0)]
    claims_hash: Sha


class StoredPaidNarration(ContractModel):
    """`report_revisions.paid_narration`: written by claims (the `claims` metadata, `narration` null) and
    completed by narrate."""
    kind: Literal["paid-narration-v1"] = PAID_NARRATION_KIND
    manifest_hash: Sha
    claims: ClaimsMeta
    narration: NarrationOutcome | None = None


# --------------------------------------------------------------------------- snapshot loading


@dataclass(frozen=True, slots=True)
class RevisionSnapshot:
    revision_id: UUID
    report_id: UUID
    session_id: UUID
    slug: str
    state: ReportState
    manifest: ReportManifest
    manifest_hash: str
    score: StoredScore | None
    free_narration: StoredFreeNarration | None
    paid_narration: StoredPaidNarration | None
    claims: tuple[PublicClaim, ...]
    rendered_at: datetime | None


def stored_claims(db: DBSession, revision_id: UUID) -> tuple[PublicClaim, ...]:
    rows = list(db.scalars(select(Claim).where(Claim.report_revision_id == revision_id).order_by(Claim.claim_index)))
    evidence: dict[UUID, list[UUID]] = {}
    for link in db.scalars(select(ClaimEvidence).where(ClaimEvidence.report_revision_id == revision_id)):
        evidence.setdefault(link.claim_id, []).append(link.finding_id)
    return tuple(PublicClaim(claim_index=row.claim_index, text=row.text, verdict=row.verdict, rationale=row.rationale,
                             finding_ids=tuple(sorted(evidence.get(row.id, ()), key=str))) for row in rows)


def load_snapshot(db: DBSession, revision: ReportRevision, slug: str | None = None, *, config: ReportConfig | None = None) -> RevisionSnapshot:
    """Everything a DTO builder may read: the verified manifest and the stored stage outputs."""
    manifest = load_manifest(revision)
    if config is None:
        content = manifest.config_content
        config = ReportConfig(content.claims, content.narration, content.scoring, content.costs, manifest.configs)
    check_configs(manifest, config)
    if slug is None:
        report = db.get(Report, revision.report_id)
        if report is None or report.session_id != revision.session_id:
            raise SnapshotIntegrityError("report does not own this revision")
        slug = report.share_slug
    score = None if revision.score_result is None else StoredScore.model_validate(revision.score_result)
    free = None if revision.free_narration is None else StoredFreeNarration.model_validate(revision.free_narration)
    paid = None if revision.paid_narration is None else StoredPaidNarration.model_validate(revision.paid_narration)
    for stored in (score, free, paid):
        if stored is not None and stored.manifest_hash != revision.manifest_hash:
            raise SnapshotIntegrityError("stage output was computed from a different manifest")
    claims = stored_claims(db, revision.id)
    if score is not None and score != compute_score(manifest, str(revision.manifest_hash), config):
        raise SnapshotIntegrityError("stored score does not replay from frozen evidence")
    findings = {row.id: row for row in manifest.all_findings}
    claim_inputs: list[ClaimResult] = []
    links = list(db.scalars(select(ClaimEvidence).where(ClaimEvidence.report_revision_id == revision.id)))
    for link in links:
        finding = findings.get(link.finding_id)
        if finding is None or link.frame_key != finding.evidence.frame_key or link.session_id != revision.session_id:
            raise SnapshotIntegrityError("claim evidence is outside the frozen revision")
    for row in claims:
        if not set(row.finding_ids) <= findings.keys():
            raise SnapshotIntegrityError("claim cites a finding outside the frozen revision")
        claim_inputs.append(ClaimResult(**row.model_dump(exclude={"finding_ids"}), finding_ids=row.finding_ids,
            evidence_frame_keys=tuple(sorted({findings[fid].evidence.frame_key for fid in row.finding_ids}))))
    if paid is not None:
        validated = resolve_claims(manifest.listing.text, evidence_input(manifest), config.claims, claim_inputs)
        if (validated.claims != tuple(claim_inputs) or paid.claims.config_hash != config.claims.content_hash
                or paid.claims.listing_hash != validated.listing_hash or paid.claims.claim_count != len(claims)
                or paid.claims.claims_hash != claims_digest(claim_inputs)):
            raise SnapshotIntegrityError("stored claims disagree with frozen evidence or metadata")
    elif claims:
        raise SnapshotIntegrityError("claims have no stored metadata")
    for outcome, scope in ((None if free is None else free.outcome, "free"),
                           (None if paid is None else paid.narration, "paid")):
        if outcome is None:
            continue
        if score is None:
            raise SnapshotIncomplete("narration requires a stored score")
        resolved = (resolve_part1_narration(evidence_input(manifest, (1,)), score.scores, config.narration, outcome.statements)
                    if scope == "free" else resolve_full_narration(evidence_input(manifest), score.scores,
                        score.repairs, claim_inputs, config.narration, outcome.statements))
        if (outcome.scope != resolved.scope or outcome.config_hash != config.narration.content_hash
                or outcome.statements_hash != narration_hash(outcome.statements) or resolved.statements != outcome.statements):
            raise SnapshotIntegrityError("stored narration disagrees with frozen evidence or scope")
    return RevisionSnapshot(revision_id=revision.id, report_id=revision.report_id, session_id=revision.session_id, slug=slug,
                            state=revision.state, manifest=manifest, manifest_hash=str(revision.manifest_hash), score=score,
                            free_narration=free, paid_narration=paid, claims=claims,
                            rendered_at=revision.rendered_at)


# --------------------------------------------------------------------------- scope-filtered DTO builders


def _public_flags(finding: Finding) -> tuple[str, ...]:
    return tuple(flag for flag in finding.flags if flag not in INTERNAL_FLAGS)


def _fields(finding: Finding, manifest: ReportManifest) -> dict[str, object]:
    # `evidence_available` stays false until the T08b privacy-approved derivative exists.
    source = manifest.part(finding.part_no).source_video
    return dict(id=finding.id, concept=finding.concept, severity=finding.severity, confidence=finding.confidence,
                evidence_note=finding.evidence_note, subzone=finding.subzone, component_id=finding.component_id,
                flags=_public_flags(finding), bbox=finding.evidence.bbox, evidence_available=False,
                source_timestamp_ms=None if source is None else source.frame_timestamps_ms[finding.evidence.frame_index],
                mapping_basis=None if source is None else source.mapping_basis)


def public_coverage_reason(part: ManifestPart, default: str | None) -> str | None:
    if part.source_video is not None:
        return "video_zone_unverified"
    if part.failure_reason == "VIDEO_ZONE_UNCONFIRMED":
        return "video_zone_unconfirmed"
    return default


def free_source_checks(manifest: ReportManifest) -> tuple[FreeSourceCheck, ...]:
    """Per kind: checked or not, plus a flag count. Missing or unassessed is never a pass."""
    by_kind = {row.kind: row for row in manifest.sources}
    return tuple(FreeSourceCheck(kind=kind, checked=kind in by_kind and by_kind[kind].summary.state == "assessed",
                                 flag_count=by_kind[kind].summary.flag_count if kind in by_kind else 0)  # flags survive "not checked"
                 for kind in SOURCE_KINDS)


def paid_source_checks(manifest: ReportManifest) -> tuple[PaidSourceCheck, ...]:
    by_kind = {row.kind: row for row in manifest.sources}
    return tuple(PaidSourceCheck(kind=kind, checked=kind in by_kind and by_kind[kind].summary.state == "assessed",
                                 source=by_kind.get(kind)) for kind in SOURCE_KINDS)


def build_free_report(snapshot: RevisionSnapshot) -> FreeReport:
    """Free scope: Part-1 findings, score, coverage and summary; other zones as locked totals only."""
    if snapshot.score is None or snapshot.free_narration is None:
        raise SnapshotIncomplete("free report needs the stored score and free narration")
    manifest = snapshot.manifest
    part1 = manifest.part(1)
    zone_score = next(row for row in snapshot.score.scores.zones if row.zone == FREE_ZONE)
    findings = tuple(FreeFinding(**_fields(row, manifest)) for row in eligible_findings(evidence_input(manifest, (1,))))
    locked = tuple(LockedZoneSummary(zone=part.zone, finding_count=sum(
        row.duplicate_of is None for row in part.verified)) for part in manifest.parts[1:] if part.state == PartState.PROCESSED)
    return FreeReport(slug=snapshot.slug, revision_id=snapshot.revision_id, state=snapshot.state,
                      coverage=(PublicCoverage(zone=zone_score.zone, state=zone_score.state,
                                               reason=public_coverage_reason(part1, zone_score.reason)),),
                      part1_score=zone_score.score, findings=findings, locked_zones=locked,
                      part1_summary=part1_summary_text(snapshot.free_narration.outcome.statements),
                      additional_checks=free_source_checks(manifest))


def build_paid_report(snapshot: RevisionSnapshot) -> PaidReport:
    """Full scope. Call only for an active report entitlement; never derive the free view by hiding fields."""
    if snapshot.score is None or snapshot.paid_narration is None or snapshot.paid_narration.narration is None:
        raise SnapshotIncomplete("paid report needs the stored score, repairs, claims and paid narration")
    zones = snapshot.score.scores.zones
    findings = tuple(PaidFinding(zone=row.zone, **_fields(row, snapshot.manifest)) for row in sorted(
        eligible_findings(evidence_input(snapshot.manifest)), key=lambda item: (item.part_no, str(item.id))))
    candidates = tuple(CandidateFinding(zone=row.zone, id=row.id, concept=row.concept, confidence=row.confidence,
        evidence_note=row.evidence_note, bbox=row.evidence.bbox, evidence_available=False,
        source_timestamp_ms=None if snapshot.manifest.part(row.part_no).source_video is None else snapshot.manifest.part(row.part_no).source_video.frame_timestamps_ms[row.evidence.frame_index],
        mapping_basis=None if snapshot.manifest.part(row.part_no).source_video is None else snapshot.manifest.part(row.part_no).source_video.mapping_basis) for row in sorted(
        (row for part in snapshot.manifest.parts for row in part.unverified if row.status == FindingStatus.UNVERIFIED and row.duplicate_of is None),
        key=lambda item: (item.part_no, str(item.id))))
    return PaidReport(slug=snapshot.slug, revision_id=snapshot.revision_id, state=snapshot.state,
                      coverage=tuple(PublicCoverage(zone=row.zone, state=row.state,
                          reason=public_coverage_reason(snapshot.manifest.part(PART_ZONES.index(row.zone) + 1), row.reason)) for row in zones),
                      scores=snapshot.score.scores, findings=findings, candidates=candidates, repairs=snapshot.score.repairs, claims=snapshot.claims,
                      narration=snapshot.paid_narration.narration.statements, rendered_at=snapshot.rendered_at,
                      additional_checks=paid_source_checks(snapshot.manifest))


def build_reports(snapshot: RevisionSnapshot) -> tuple[FreeReport, PaidReport]:
    return build_free_report(snapshot), build_paid_report(snapshot)
