"""Segment stage: one claimed `concept_run` -> candidate findings.

Per claimed `concept_run`/`segment` job:

1. Short read-only transaction: validate the concept run, part, assessment run and frozen config (invariant
   8: a run replays only under the config it was created with).
2. Outside any transaction: download the ingest manifest, call the configured Segmenter (live requests
   require budget dispatch), collapse tracks to one candidate per track (`collapse_tracks`), and, for
   video-mode parts, lazily extract ONLY the cited evidence frames from the proxy under an attempt-scoped
   prefix `media/{session}/{part}/{run}/seg/{concept_run}/a{attempt}/` with `extract_and_store_frames`.
   Stills-mode parts reuse the stills stored at ingest.
3. Return a `Publication`. Inside the queue's lease-fenced, session-locked transaction it links the new
   frame artifacts, inserts candidate findings plus their track footprints, marks the concept run
   `succeeded` (zero candidates is a success that drains normally) and evaluates the part barrier. A stale
   worker, or one whose session started deleting, publishes nothing; its objects stay unlinked orphans under
   the attempt prefix (purge deletes by the `media/{session}/` prefix).

Verification is NOT scheduled here: duplicate candidates of sibling concept runs must be merged first, so
the last concept run to finish triggers dedup and verify scheduling (`orchestration.advance_part`).

Failure policy mirrors ingest: transient errors use the queue's three attempts, then the concept run ends
`failed` (coverage partial) instead of the job stalling; provider-access/price/budget problems end
`unsupported`/`budget_exhausted` immediately. Other concept runs are unaffected.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.meta import (FrameRef, MaskRaster, MediaRef, ProviderFailure, ProviderUsage, SegmentObservation,
                               SegmentResult, Segmenter)
from app.adapters.storage import ObjectStorage, StorageError
from app.contracts.enums import FindingStatus, PartState, PrivacyState, SessionState
from app.contracts.evidence import BoundingBox
from app.domain.dedup import TrackBox, TrackGroup, collapse_tracks
from app.jobs.queue import Failure, Lease
from app.jobs.worker import Publication, StageFailure
from app.models import Artifact, ConceptRun, Finding as FindingRow, Observation, Part, PipelineRun, Session
from app.pipeline.ingest import FrameArtifact, PublicationRejected, extract_and_store_frames, scratch_root_for
from app.pipeline.media import FrameManifest, MediaConfig, MediaError, MediaReason, load_media_config, scratch_dir
from app.pipeline.orchestration import (ASSESS_INPUT, TRACK_FOOTPRINT, OrchestrationContext, advance_part,
                                        observations)
from app.services.uploads import MEDIA_CONFIG_PATH
from app.settings import Settings
from app.pipeline.vehicle_mask_contract import VehicleMaskMarker, load_vehicle_mask_marker
from app.services.budgets import (PRICE_VERSION as BUDGET_PRICE_VERSION, BudgetError, BudgetService,
                                  ReservationRequest, UsageUnits, documented_prices, price_usage)

MAX_ATTEMPTS = 3
MAX_CANDIDATES_PER_CONCEPT_RUN = 64  # design default; exceeding it fails the concept run, never truncates silently
DEADLINE_MARGIN_SECONDS = 5
_LIVE = (SessionState.CAPTURING, SessionState.FINALIZED)

SegmenterFactory = Callable[[int], Segmenter]

# Reasons stored on concept runs (<= 240 chars; stable codes only).
CONFIG_MISMATCH = "SEGMENT_CONFIG_MISMATCH"
TOO_MANY_CANDIDATES = "SEGMENT_TOO_MANY_CANDIDATES"
ARTIFACT_UNAVAILABLE = "SEGMENT_INPUT_UNAVAILABLE"
DEADLINE = "SEGMENT_TIMEOUT"
STORAGE = "SEGMENT_STORAGE_UNAVAILABLE"
INTERNAL = "SEGMENT_INTERNAL_ERROR"
# Provider failures that are not retryable: the capability or budget is absent, so re-calling cannot help.
_NOT_RETRYABLE = {"model_unverified": "unsupported", "price_unverified": "unsupported",
                  "standard_access_unverified": "unsupported", "live_transport_disabled": "unsupported",
                  "budget_not_reserved": "budget_exhausted"}
_QUEUE_CODE = {"provider_timeout": "PROVIDER_TIMEOUT", "provider_failed": "TRANSIENT", "incomplete_response": "TRANSIENT"}
_RETRYABLE_MEDIA = {MediaReason.TIMEOUT: "SEGMENT_MEDIA_TIMEOUT", MediaReason.RESOURCE_LIMIT: "SEGMENT_MEDIA_RESOURCE_LIMIT"}


# --------------------------------------------------------------------------- fixture segmenter (synthetic)


@dataclass(frozen=True, slots=True)
class ScriptedTrack:
    """One synthetic track: `points` are (time fraction 0..1, x, y, w, h, confidence or None)."""
    track_id: str
    points: tuple[tuple[float, float, float, float, float, float | None], ...]


class ScriptedSegmenter:
    """Deterministic fixture Segmenter. Every result is labelled `synthetic`; it proves orchestration only,
    never perception quality. `failures` maps a prompt id to a `ProviderFailure` code to raise."""

    def __init__(self, script: Mapping[str, Sequence[ScriptedTrack]] | None = None,
                 failures: Mapping[str, str] | None = None) -> None:
        self.script = dict(script or {})
        self.failures = dict(failures or {})

    def segment(self, media_ref: MediaRef, prompt_id: str, request_id: UUID) -> SegmentResult:
        if prompt_id in self.failures:
            raise ProviderFailure(self.failures[prompt_id])
        frames = sorted(media_ref.frames, key=lambda row: row.frame_index)
        width, height = media_ref.display_size
        found: list[SegmentObservation] = []
        for track in self.script.get(prompt_id, ()):
            for fraction, x, y, w, h, confidence in track.points:
                frame = frames[min(int(fraction * len(frames)), len(frames) - 1)]
                found.append(SegmentObservation(
                    track_id=track.track_id, frame_index=frame.frame_index, frame_key=frame.frame_key,
                    timestamp_ms=frame.timestamp_ms, frame_width=width, frame_height=height,
                    bbox=BoundingBox(x=x, y=y, w=w, h=h), mask=MaskRaster(width=1, height=1, pixels=b"\x01"),
                    confidence=confidence))
        usage = ProviderUsage()  # fixture mode has zero live spend; unknown live usage is never implied
        return SegmentResult(request_id=request_id, prompt_id=prompt_id, provenance="synthetic",
                             observations=tuple(sorted(found, key=lambda o: (o.frame_index, int(o.track_id)))), usage=usage)


# Default dev script: part 1 only. Two prompts see the same synthetic rust patch on the same frame, so the
# fixture pipeline exercises cross-prompt dedup. Everything else is an empty (zero-candidate) success.
DEFAULT_SCRIPT: Mapping[int, Mapping[str, tuple[ScriptedTrack, ...]]] = {
    1: {"rust_a": (ScriptedTrack("1", ((0.3, 0.30, 0.40, 0.20, 0.15, 0.8), (0.5, 0.32, 0.41, 0.20, 0.15, 0.7))),),
        "rust_b": (ScriptedTrack("1", ((0.3, 0.31, 0.40, 0.20, 0.15, 0.6),)),)},
}


def default_segmenter_factory(part_no: int) -> Segmenter:
    return ScriptedSegmenter(DEFAULT_SCRIPT.get(part_no, {}))


# --------------------------------------------------------------------------- internals



VEHICLE_PROMPT_ID = "vehicle"  # transport-level text "car"; not an assessment concept
VEHICLE_PARTS = (1, 2, 3)  # body zones where a vehicle mask is meaningful
CONTAINMENT_MIN = 0.5  # provisional; measured on labeled candidates before freeze


def containment_fraction(mask: MaskRaster, bbox: Mapping[str, float], coverage: bytes | None, width: int, height: int) -> float | None:
    """Fraction of mask pixels inside vehicle coverage. None means no basis to judge."""
    extent = (round((bbox["x"] + bbox["w"]) * width) - round(bbox["x"] * width),
              round((bbox["y"] + bbox["h"]) * height) - round(bbox["y"] * height))
    if (coverage is None or len(coverage) != width * height or (mask.width, mask.height) != extent
            or not any(coverage)):
        return None
    left = round(bbox["x"] * width)
    top = round(bbox["y"] * height)
    inside = total = 0
    for index, pixel in enumerate(mask.pixels):
        if not pixel:
            continue
        total += 1
        gx, gy = left + index % mask.width, top + index // mask.width
        if 0 <= gx < width and 0 <= gy < height and coverage[gy * width + gx]:
            inside += 1
    return None if total == 0 else inside / total

@dataclass(frozen=True, slots=True)
class _Plan:
    session_id: UUID
    run_id: UUID
    part_id: UUID
    part_no: int
    concept_run_id: UUID
    concept: object
    prompt_id: str
    prompt_text: str
    mode: str
    facts: dict
    vehicle_mask: VehicleMaskMarker | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    id: UUID
    track_id: str
    frame_index: int
    frame_key: str
    width: int
    height: int
    bbox: dict[str, float]
    confidence: float | None
    footprint: list[dict]
    note: str
    mask_key: str | None = None
    mask_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _Segmented:
    candidates: tuple[_Candidate, ...]
    new_frames: tuple[FrameArtifact, ...]


class _Terminal(Exception):
    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state, self.reason = state, reason


class _Retryable(Exception):
    def __init__(self, reason: str, queue_code: str = "TRANSIENT") -> None:
        super().__init__(reason)
        self.reason, self.queue_code = reason, queue_code


class SegmentHandler:
    def __init__(self, factory: sessionmaker[DBSession], storage: ObjectStorage, settings: Settings,
                 ctx: OrchestrationContext, *, segmenter_factory: SegmenterFactory = default_segmenter_factory,
                 media_config: MediaConfig | None = None, budget_service: BudgetService | None = None) -> None:
        from app.adapters.session_storage import fenced_storage
        self.factory, self.storage, self.settings, self.ctx = factory, fenced_storage(factory, storage), settings, ctx
        self.segmenter_factory = segmenter_factory
        self.media_config = media_config or load_media_config(MEDIA_CONFIG_PATH)
        self.budget_service = budget_service
        self._vehicle_cache: dict[tuple, dict[int, bytes]] = {}

    def __call__(self, lease: Lease) -> Publication:
        plan = self._load(lease)
        if isinstance(plan, Publication):
            return plan
        stored: list[str] = []
        try:
            with scratch_dir(scratch_root_for(self.settings)) as scratch:
                segmented = self._segment(lease, plan, scratch, stored)
        except _Terminal as error:
            self._discard(stored)
            return self._finish(plan, error.state, error.reason, _Segmented((), ()), lease.job_id)
        except _Retryable as error:
            self._discard(stored)
            return self._retry_or_fail(lease, plan, error)
        except Exception as error:  # unexpected: never a clean result, never an unbounded retry
            self._discard(stored)
            logging.warning("Segment attempt %s failed unexpectedly: %s", lease.attempt, type(error).__name__)
            return self._retry_or_fail(lease, plan, _Retryable(INTERNAL, "INTERNAL"))
        return self._finish(plan, "succeeded", None, segmented, lease.job_id)

    # ---- phase 1

    def _load(self, lease: Lease) -> _Plan | Publication:
        terminal = StageFailure(Failure("INVALID_INPUT", retryable=False))
        try:
            concept_run_id = UUID(lease.subject_id)
        except ValueError:
            raise terminal from None
        if lease.session_id is None:
            raise terminal
        with self.factory() as db:
            owner = db.get(Session, lease.session_id)
            cr = db.scalar(select(ConceptRun).where(ConceptRun.id == concept_run_id, ConceptRun.session_id == lease.session_id))
            if owner is None or owner.state not in _LIVE or cr is None:
                raise terminal
            run = db.scalar(select(PipelineRun).where(PipelineRun.id == cr.run_id, PipelineRun.session_id == lease.session_id))
            part = db.scalar(select(Part).where(Part.id == cr.part_id, Part.session_id == lease.session_id))
            if run is None or part is None or run.subject_key != f"assess:{part.id}":
                raise terminal
            if cr.state != "pending" or part.state != PartState.PROCESSING:
                return Publication()  # an earlier attempt already published (or the part ended): converge
            markers = observations(db, run.id, ASSESS_INPUT)
            if len(markers) != 1:
                raise terminal
            expected = self.ctx.config_hashes
            config_ok = all(run.config_hashes.get(key) == expected[key] for key in expected)
            entry = self.ctx.config.prompts.prompt(cr.prompt_id) if cr.prompt_id in {p.prompt_id for p in self.ctx.config.prompts.prompts} else None
            plan = _Plan(session_id=part.session_id, run_id=run.id, part_id=part.id, part_no=part.part_no,
                         concept_run_id=cr.id, concept=cr.concept, prompt_id=cr.prompt_id,
                         prompt_text="" if entry is None else entry.text,
                         mode=self.ctx.config.prompts.part(part.part_no).media, facts=dict(markers[0].value))
            if not config_ok or entry is None:
                return self._finish(plan, "failed", CONFIG_MISMATCH, _Segmented((), ()), lease.job_id)
            if "vehicle_mask" in run.config_hashes:
                try:
                    plan = replace(plan, vehicle_mask=load_vehicle_mask_marker(db, run))
                except ValueError:
                    return self._finish(plan, "failed", "VEHICLE_MASK_UNAVAILABLE", _Segmented((), ()), lease.job_id)
            return plan

    # ---- phase 2 (no transaction open)

    def _segment(self, lease: Lease, plan: _Plan, scratch: Path, stored: list[str]) -> _Segmented:
        self._check_deadline(lease)
        manifest = self._manifest(plan, scratch)
        if plan.vehicle_mask is not None:
            # Validate the persisted mask response and the exact bytes for every input
            # before any damage request. The live transport checks the hash again on
            # the bytes it actually submits, closing a download/dispatch race.
            for index, item in enumerate(plan.vehicle_mask.media):
                for key, digest in ((item.masked_key, item.masked_sha256), (item.mask_key, item.mask_sha256)):
                    path = scratch / f"binding-{index}"
                    try:
                        downloaded = self.storage.download_to(key, path, 24 * 1024 * 1024)
                    except StorageError:
                        raise _Terminal("failed", "VEHICLE_MASK_UNAVAILABLE") from None
                    if downloaded.sha256 != digest:
                        raise _Terminal("failed", "VEHICLE_MASK_UNAVAILABLE")
                    path.unlink(missing_ok=True)
        result_boxes, provenance, masks, coverage = self._call_segmenter(lease, plan, manifest)
        try:
            groups = collapse_tracks(result_boxes)
        except ValueError:
            raise _Retryable("SEGMENT_SCHEMA_INVALID", "SCHEMA_INVALID") from None
        if len(groups) > MAX_CANDIDATES_PER_CONCEPT_RUN:
            raise _Terminal("failed", TOO_MANY_CANDIDATES)
        if not groups:
            return _Segmented((), ())
        keys: dict[int, tuple[str, int, int]] = {}
        new_frames: tuple[FrameArtifact, ...] = ()
        wanted = sorted({group.representative.frame_index for group in groups})
        if plan.mode == "stills":
            by_index = {item["frame_index"]: item["key"] for item in plan.facts["frames"]}
            if any(index not in by_index for index in wanted):
                raise _Terminal("failed", ARTIFACT_UNAVAILABLE)
            keys = {index: (by_index[index], manifest.width, manifest.height) for index in wanted}
        else:
            new_frames = self._extract(lease, plan, manifest, wanted, scratch, stored)
            keys = {frame.frame_index: (frame.key, frame.width, frame.height) for frame in new_frames}
        candidates = tuple(self._candidate(plan, provenance, group, keys) for group in groups)
        candidates = tuple(self._store_mask(lease, plan, item, masks.get((item.track_id, item.frame_index)), scratch, stored) for item in candidates)
        kept: list[_Candidate] = []
        for item in candidates:
            mask = masks.get((item.track_id, item.frame_index))
            fraction = None if mask is None or coverage is None else containment_fraction(
                mask, item.bbox, coverage.get(item.frame_index), item.width, item.height)
            if fraction is not None and fraction < CONTAINMENT_MIN:
                # The provisional mask is advisory, not a validated rejection verdict.
                # Keep the evidence on the normal verification path; never erase a
                # real defect merely because the vehicle mask missed it.
                item = replace(item, note="Outside estimated vehicle area; containment is provisional, candidate requires verification.")
            kept.append(item)
        return _Segmented(tuple(kept), new_frames)

    def _manifest(self, plan: _Plan, scratch: Path) -> FrameManifest:
        target = scratch / "manifest.json"
        try:
            self.storage.download_to(plan.facts["manifest_key"], target, 16 * 1024 * 1024)
        except StorageError as error:
            if error.code in {"404", "NoSuchKey", "NotFound"}:
                raise _Terminal("failed", ARTIFACT_UNAVAILABLE) from None
            raise _Retryable(STORAGE) from None
        try:
            manifest = FrameManifest.from_json(target.read_text(encoding="utf-8"))
        except (ValueError, KeyError, TypeError):
            raise _Terminal("failed", ARTIFACT_UNAVAILABLE) from None
        if manifest.content_hash != plan.facts["manifest_sha256"]:
            raise _Terminal("failed", ARTIFACT_UNAVAILABLE)
        return manifest

    def _run_request(self, lease: Lease, plan: _Plan, segmenter: Segmenter, media: MediaRef, request_id: UUID,
                     prompt_id: str | None = None) -> SegmentResult:
        from app.pipeline.sam_dispatch import run_sam_request
        return run_sam_request(lease, segmenter, media, request_id, prompt_id or plan.prompt_id, self.budget_service)


    def _vehicle_coverage(self, lease: Lease, plan: _Plan, segmenter: Segmenter,
                            media: MediaRef, request_id: UUID) -> dict[int, bytes] | None:
        """Per-frame vehicle coverage for body-zone video, or None (no basis to judge).

        One budgeted vehicle request per assessment run + media, cached on the handler;
        any provider failure leaves candidates on the normal unverified path."""
        if plan.mode != "video" or plan.part_no not in VEHICLE_PARTS:
            return None
        cache_key = (plan.run_id, media.media_key)
        if cache_key in self._vehicle_cache:
            return self._vehicle_cache[cache_key]
        try:
            result = self._run_request(lease, plan, segmenter, media, request_id, prompt_id=VEHICLE_PROMPT_ID)
        except ProviderFailure:
            return None
        width, height = media.stored_width, media.stored_height
        coverage: dict[int, bytearray] = {}
        for item in result.observations:
            frame = coverage.setdefault(item.frame_index, bytearray(width * height))
            left = round(item.bbox.x * width)
            top = round(item.bbox.y * height)
            extent = (round((item.bbox.x + item.bbox.w) * width) - left,
                      round((item.bbox.y + item.bbox.h) * height) - top)
            if (item.mask.width, item.mask.height) != extent:
                return None  # invalid geometry cannot justify a containment decision
            for index, pixel in enumerate(item.mask.pixels):
                if not pixel:
                    continue
                gx, gy = left + index % item.mask.width, top + index // item.mask.width
                if 0 <= gx < width and 0 <= gy < height:
                    frame[gy * width + gx] = 1
        frozen = {frame: bytes(values) for frame, values in coverage.items()}
        if len(self._vehicle_cache) > 4:
            self._vehicle_cache.clear()
        self._vehicle_cache[cache_key] = frozen
        return frozen

    def _call_segmenter(self, lease: Lease, plan: _Plan, manifest: FrameManifest) -> tuple[list[TrackBox], str, dict[tuple[str, int], MaskRaster], dict[int, bytes] | None]:
        segmenter = self.segmenter_factory(plan.part_no)
        request_id = uuid5(NAMESPACE_URL, f"{lease.job_id}:{lease.attempt}")
        boxes: list[TrackBox] = []
        masks: dict[tuple[str, int], MaskRaster] = {}
        provenance = "synthetic"
        try:
            if plan.mode == "video":
                binding = None if plan.vehicle_mask is None else plan.vehicle_mask.media[0]
                media = MediaRef(media_key=plan.facts["proxy_key"] if binding is None else binding.masked_key,
                                 content_sha256=None if binding is None else binding.masked_sha256,
                                 kind="video", stored_width=manifest.width,
                                 stored_height=manifest.height,
                                 frames=tuple(FrameRef(frame_index=index, timestamp_ms=round(manifest.time_seconds(index) * 1000),
                                                       frame_key=f"pending/frame_{index}")
                                              for index in range(manifest.frame_count)))
                result = self._run_request(lease, plan, segmenter, media, request_id)
                provenance = result.provenance
                boxes += [self._box(plan, item, item.track_id, item.frame_index) for item in result.observations]
                masks.update({(item.track_id, item.frame_index): item.mask for item in result.observations})
            else:  # stills: one image request per stored still
                for still in plan.facts["frames"]:
                    self._check_deadline(lease)
                    binding = None if plan.vehicle_mask is None else next(
                        item for item in plan.vehicle_mask.media if item.source_key == still["key"])
                    media_key = still["key"] if binding is None else binding.masked_key
                    media = MediaRef(media_key=media_key, content_sha256=None if binding is None else binding.masked_sha256,
                                     kind="image", stored_width=manifest.width,
                                     stored_height=manifest.height,
                                     frames=(FrameRef(frame_index=0, timestamp_ms=0, frame_key=media_key),))
                    result = self._run_request(lease, plan, segmenter, media, uuid5(request_id, str(still["frame_index"])))
                    provenance = result.provenance
                    boxes += [self._box(plan, item, f"s{still['frame_index']}-{item.track_id}", still["frame_index"])
                              for item in result.observations]
                    masks.update({(f"s{still['frame_index']}-{item.track_id}", still["frame_index"]): item.mask for item in result.observations})
        except ProviderFailure as failure:
            logging.warning("Segment request failed code=%s http_status=%s http_status_class=%s", failure.code, failure.http_status, failure.http_status_class)
            if failure.code in _NOT_RETRYABLE:
                raise _Terminal(_NOT_RETRYABLE[failure.code], f"SEGMENT_{failure.code.upper()}") from None
            if not failure.retryable:  # a billed response we reject would be rejected (and billed) again
                raise _Terminal("failed", f"SEGMENT_{failure.code.upper()}"[:240]) from None
            raise _Retryable(f"SEGMENT_{failure.code.upper()}"[:240], _QUEUE_CODE.get(failure.code, "SCHEMA_INVALID")) from None
        coverage = None
        if plan.vehicle_mask is None and plan.mode == "video" and getattr(segmenter, "requires_budget", False):
            coverage = self._vehicle_coverage(lease, plan, segmenter, media, request_id)
        return boxes, provenance, masks, coverage

    def _store_mask(self, lease: Lease, plan: _Plan, candidate: _Candidate, mask: MaskRaster | None,
                    scratch: Path, stored: list[str]) -> _Candidate:
        # Persist the exact box-local SAM raster as a PNG artifact. Failure leaves the
        # candidate with its bbox only; the derivative then falls back to a box outline.
        if mask is None:
            return candidate
        extent = (round((candidate.bbox["x"] + candidate.bbox["w"]) * candidate.width) - round(candidate.bbox["x"] * candidate.width),
                  round((candidate.bbox["y"] + candidate.bbox["h"]) * candidate.height) - round(candidate.bbox["y"] * candidate.height))
        if (mask.width, mask.height) != extent:
            return candidate  # only an exact box-local raster may annotate the evidence frame
        try:
            from PIL import Image

            path = scratch / f"mask-{candidate.id}.png"
            Image.frombytes("L", (mask.width, mask.height),
                            bytes(255 if pixel else 0 for pixel in mask.pixels)).save(path, format="PNG")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            key = (f"media/{plan.session_id}/{plan.part_id}/{plan.run_id}/seg/{plan.concept_run_id}"
                   f"/a{lease.attempt}/mask_{candidate.id}.png")
            self.storage.put_file(key, path, "image/png")
            stored.append(key)
            return replace(candidate, mask_key=key, mask_sha256=digest)
        except (StorageError, OSError, ValueError):
            return candidate

    @staticmethod
    def _box(plan: _Plan, item: SegmentObservation, track_id: str, frame_index: int) -> TrackBox:
        return TrackBox(concept_run_id=plan.concept_run_id, track_id=track_id, frame_index=frame_index,
                        bbox=item.bbox, confidence=item.confidence)

    def _extract(self, lease: Lease, plan: _Plan, manifest: FrameManifest, indices: list[int], scratch: Path,
                 stored: list[str]) -> tuple[FrameArtifact, ...]:
        proxy = scratch / "proxy.mp4"
        try:
            downloaded = self.storage.download_to(plan.facts["proxy_key"], proxy, self.settings.pipeline_proxy_max_bytes)
        except StorageError as error:
            if error.code in {"404", "NoSuchKey", "NotFound", "ObjectTooLarge"}:
                raise _Terminal("failed", ARTIFACT_UNAVAILABLE) from None
            raise _Retryable(STORAGE) from None
        if downloaded.sha256 != plan.facts["proxy_sha256"]:
            raise _Terminal("failed", ARTIFACT_UNAVAILABLE)
        prefix = f"media/{plan.session_id}/{plan.part_id}/{plan.run_id}/seg/{plan.concept_run_id}/a{lease.attempt}/"
        try:
            frames = extract_and_store_frames(self.storage, proxy, manifest, indices, prefix, scratch,
                                              self.media_config, before_each=lambda: self._check_deadline(lease))
        except MediaError as error:
            if error.reason in _RETRYABLE_MEDIA:
                raise _Retryable(_RETRYABLE_MEDIA[error.reason]) from None
            raise _Terminal("failed", f"SEGMENT_MEDIA_{error.reason.name}") from None
        except StorageError:
            raise _Retryable(STORAGE) from None
        stored.extend(frame.key for frame in frames)
        return frames

    @staticmethod
    def _candidate(plan: _Plan, provenance: str, group: TrackGroup, keys: dict[int, tuple[str, int, int]]) -> _Candidate:
        rep = group.representative
        key, width, height = keys[rep.frame_index]
        return _Candidate(
            id=uuid4(), track_id=group.track_id, frame_index=rep.frame_index, frame_key=key, width=width, height=height,
            bbox=rep.bbox.model_dump(), confidence=rep.confidence,
            footprint=[{"frame_index": f.frame_index, "bbox": f.bbox.model_dump(), "confidence": f.confidence}
                       for f in group.frames],
            note=f"Unverified {provenance} segmentation candidate ({plan.prompt_text}); not a confirmed finding."[:240])

    @staticmethod
    def _check_deadline(lease: Lease) -> None:
        if (lease.deadline - datetime.now(timezone.utc)).total_seconds() <= DEADLINE_MARGIN_SECONDS:
            raise _Retryable(DEADLINE, "TRANSIENT")

    def _discard(self, keys: list[str]) -> None:
        for key in keys:
            try:
                self.storage.delete_object(key)
            except StorageError:
                pass  # unlinked orphan under the attempt prefix

    def _retry_or_fail(self, lease: Lease, plan: _Plan, error: _Retryable) -> Publication:
        if lease.attempt < MAX_ATTEMPTS:
            raise StageFailure(Failure(error.queue_code))
        return self._finish(plan, "failed", error.reason, _Segmented((), ()), lease.job_id)  # budget spent: fail, never stall

    # ---- phase 3: runs inside the queue's fenced completion transaction

    def _finish(self, plan: _Plan, state: str, reason: str | None, segmented: _Segmented,
                job_id: UUID | None = None) -> Publication:
        ctx = self.ctx

        def publish(db: DBSession) -> None:
            part = db.scalar(select(Part).where(Part.id == plan.part_id, Part.session_id == plan.session_id).with_for_update())
            cr = db.scalar(select(ConceptRun).where(ConceptRun.id == plan.concept_run_id,
                                                    ConceptRun.session_id == plan.session_id).with_for_update())
            if part is None or cr is None or part.state != PartState.PROCESSING or cr.state != "pending":
                raise PublicationRejected("concept run or part changed since the attempt started")
            db.add_all(Artifact(session_id=plan.session_id, kind="frame", key=frame.key, sha256=frame.sha256,
                                privacy_state=PrivacyState.PENDING) for frame in segmented.new_frames)
            db.add_all(Artifact(session_id=plan.session_id, kind="mask", key=item.mask_key, sha256=item.mask_sha256,
                                privacy_state=PrivacyState.PENDING) for item in segmented.candidates if item.mask_key and item.mask_sha256)
            db.flush()  # findings reference artifact keys
            for item in segmented.candidates:
                db.add(FindingRow(id=item.id, session_id=plan.session_id, run_id=plan.run_id, part_id=plan.part_id,
                                  concept_run_id=plan.concept_run_id, track_id=item.track_id, frame_key=item.frame_key,
                                  frame_index=item.frame_index, frame_width=item.width, frame_height=item.height,
                                  bbox=item.bbox, mask_key=item.mask_key, status=FindingStatus.CANDIDATE, concept=plan.concept, confidence=_dec(item.confidence),
                                  evidence_note=item.note, flags=[]))
            db.flush()
            for item in segmented.candidates:
                db.add(Observation(session_id=plan.session_id, run_id=plan.run_id, kind=TRACK_FOOTPRINT,
                                   frame_key=item.frame_key,
                                   value={"finding_id": str(item.id), "frames": item.footprint}))
            cr.state = state
            cr.reason = reason
            db.flush()
            advance_part(db, ctx, plan.part_id, job_id)

        return Publication(publish=publish)


def _dec(value: float | None):  # type: ignore[no-untyped-def]
    from decimal import Decimal
    return None if value is None else Decimal(str(value))


def register_segment_handler(registry, factory: sessionmaker[DBSession], storage: ObjectStorage, settings: Settings,  # type: ignore[no-untyped-def]
                             ctx: OrchestrationContext, *, segmenter_factory: SegmenterFactory = default_segmenter_factory,
                             media_config: MediaConfig | None = None, budget_service: BudgetService | None = None) -> SegmentHandler:
    handler = SegmentHandler(factory, storage, settings, ctx, segmenter_factory=segmenter_factory, media_config=media_config, budget_service=budget_service)
    registry.register("segment", handler)
    return handler
