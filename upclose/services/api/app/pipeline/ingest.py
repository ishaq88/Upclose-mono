"""Ingest stage handler (T05a): accepted raw upload -> private proxy, manifest and frame artifacts.

Protocol, in order, per claimed `part_run`/`ingest` job:

1. Short read-only transaction: load and validate the run, part, upload and session (no locks).
2. Outside any transaction: download the raw object into private scratch (bounded by the accepted
   size, hashed while streaming), probe/normalize/measure with the media layer, and upload the proxy
   and manifest (plus, for stills-mode parts only, the planned stills) under keys unique to this
   attempt. Video-mode parts store no frames at ingest; `extract_and_store_frames` serves later stages.
3. Return a `Publication`. The queue then runs it inside the lease-fenced, session-locked completion
   transaction: part hash/quality/state, run state and artifact rows commit all-or-nothing together
   with the job, so a worker that lost its lease or whose session is deleting publishes nothing.

Objects written by an attempt that never publishes (lost lease, crash) are orphans: their keys carry
the attempt identity and no row references them, so they are never linked to a report. Garbage
collection is a known gap; every such key lives under `media/{session}/{part}/{run}/`.

Failure policy: input-caused media rejections and a missing/changed raw object fail the part
terminally. Timeouts, resource limits, storage/network errors and unexpected errors are retried by the
queue; on the last attempt the part fails with a coded reason instead of staying `uploaded`. A valid
but poor clip is processed normally with `usable_for_scoring=False` recorded in `parts.quality`.
"""
from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.storage import ObjectStorage, StorageError
from app.adapters.session_storage import fenced_storage
from app.contracts.enums import PartState, PrivacyState, SessionState, UploadState, Zone
from app.jobs.queue import Failure, Lease
from app.jobs.worker import Publication, StageFailure, StageRegistry
from app.models import Artifact, Part, PipelineRun, Session, Upload
from app.pipeline.media import (FrameManifest, MediaConfig, MediaError, MediaReason, extract_frames,
                                load_media_config, scratch_dir, sweep_stale_scratch)
from app.pipeline.quality import normalize_and_assess
from app.services.uploads import MEDIA_CONFIG_PATH, ingest_input_hash
from app.settings import Settings

MAX_ATTEMPTS = 3  # the queue's total attempt budget (initial + two retries)
DEADLINE_MARGIN = timedelta(seconds=5)
_LIVE_SESSION_STATES = (SessionState.CAPTURING, SessionState.FINALIZED)
_SETTLED_PART_STATES = (PartState.PROCESSING, PartState.PROCESSED, PartState.FAILED, PartState.SKIPPED)

# Part failure reasons are stable machine codes (<= 240 chars, never paths or media content).
CONFIG_MISMATCH = "INGEST_CONFIG_MISMATCH"
OBJECT_MISSING = "INGEST_OBJECT_MISSING"
OBJECT_CHANGED = "INGEST_OBJECT_CHANGED"
STORAGE_UNAVAILABLE = "INGEST_STORAGE_UNAVAILABLE"
DEADLINE_EXCEEDED = "INGEST_TIMEOUT"
INTERNAL_ERROR = "INGEST_INTERNAL_ERROR"
_RETRYABLE_MEDIA = {MediaReason.TIMEOUT: "MEDIA_TIMEOUT", MediaReason.RESOURCE_LIMIT: "MEDIA_RESOURCE_LIMIT"}
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}


def media_failure_reason(reason: MediaReason) -> str:
    return f"MEDIA_{reason.name}"


# --------------------------------------------------------------------------- value types


@dataclass(frozen=True, slots=True)
class FrameArtifact:
    frame_index: int
    pts: int
    key: str
    width: int
    height: int
    sha256: str


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """Immutable facts a successful ingest committed; the input of the T05b fan-out seam.

    `frames` means frames stored AT INGEST: the planned stills of stills-mode parts (4/5/8, provider
    inputs) and empty for video-mode parts, whose evidence frames are extracted later, only for the
    indices candidates cite, with `extract_and_store_frames`."""
    session_id: UUID
    part_id: UUID
    part_no: int
    zone: Zone
    run_id: UUID
    usable_for_scoring: bool
    raw_sha256: str
    proxy_key: str
    manifest_key: str
    frames: tuple[FrameArtifact, ...]


FanoutPlanner = Callable[[IngestOutcome], Publication]


def plan_downstream_after_ingest(outcome: IngestOutcome) -> Publication:
    """T05b fan-out seam. Runs inside the ingest publication transaction (session and job already
    locked): creates the part's assessment run, one concept run per runnable prompt and one `segment` job
    per concept run, atomically with the part's `uploaded -> processing` transition. A part with nothing to
    run completes immediately. Fixture providers; `orchestration.install_pipeline_handlers` wires the
    explicit context used by the worker."""
    from app.pipeline.orchestration import OrchestrationContext, plan_downstream

    return plan_downstream(OrchestrationContext(), outcome)


@dataclass(frozen=True, slots=True)
class _Plan:
    session_id: UUID
    run_id: UUID
    part_id: UUID
    part_no: int
    zone: Zone
    raw_key: str
    expected_bytes: int
    config_matches: bool


@dataclass(frozen=True, slots=True)
class _ArtifactSpec:
    kind: str
    key: str
    sha256: str


@dataclass(frozen=True, slots=True)
class _Processed:
    raw_sha256: str
    quality: dict[str, object]
    artifacts: tuple[_ArtifactSpec, ...]
    outcome: IngestOutcome


class _Terminal(Exception):
    """Input-caused or otherwise deterministic: fail the part now."""

    def __init__(self, code: str, raw_sha256: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.raw_sha256 = raw_sha256


class _Retryable(Exception):
    """Environmental: retried by the queue; fails the part on the last attempt."""

    def __init__(self, code: str, queue_code: str = "TRANSIENT", raw_sha256: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.queue_code = queue_code
        self.raw_sha256 = raw_sha256


class PublicationRejected(Exception):
    """The part/run no longer match what this attempt processed; the job retries and converges."""


# --------------------------------------------------------------------------- keys


def attempt_prefix(plan_session: UUID, part_id: UUID, run_id: UUID, attempt: int) -> str:
    """Unique per run and attempt, so a stale attempt can never overwrite a published object."""
    return f"media/{plan_session}/{part_id}/{run_id}/a{attempt}/"


def scratch_root_for(settings: Settings) -> Path:
    return Path(settings.ingest_scratch_root) if settings.ingest_scratch_root else Path(tempfile.gettempdir()) / "upclose-ingest"


def sweep_ingest_scratch(settings: Settings) -> int:
    """Worker-start helper: remove scratch directories left by a crashed process."""
    removed = sweep_stale_scratch(scratch_root_for(settings), timedelta(seconds=settings.ingest_scratch_sweep_age_seconds))
    return len(removed)


# --------------------------------------------------------------------------- frame storage


def frame_key(key_prefix: str, frame_index: int) -> str:
    return f"{key_prefix}frames/frame_{frame_index:06d}.jpg"


def extract_and_store_frames(storage: ObjectStorage, proxy: Path, manifest: FrameManifest, indices: Sequence[int],
                             key_prefix: str, scratch: Path, config: MediaConfig, *,
                             before_each: Callable[[], None] | None = None) -> tuple[FrameArtifact, ...]:
    """Extract exactly `indices` from a local copy of the proxy and store each as a private JPEG.

    Reusable by the stills path at ingest and by the later verify/evidence stage, which downloads the
    proxy and passes only the frame indices candidates cite. `key_prefix` must be attempt-scoped
    (`attempt_prefix(...)` for ingest; the caller's own run/attempt prefix later) so a key is written
    once and a stale attempt can never overwrite a published object. Returns one record per index in
    ascending order, with the manifest pts, dimensions and SHA-256; the caller links them as artifact
    rows in its own fenced publication. Objects already written are deleted if a later step fails.
    Raises `MediaError` (extraction) or `StorageError` (writes).
    """
    frames = extract_frames(proxy, manifest, indices, scratch, config)
    written: list[str] = []
    try:
        records = []
        for frame in frames:
            if before_each is not None:
                before_each()
            key = frame_key(key_prefix, frame.frame_index)
            written.append(key)  # recorded first: a put that raised may still have written the object
            storage.put_file(key, frame.path, "image/jpeg")
            records.append(FrameArtifact(frame.frame_index, frame.pts, key, frame.width, frame.height, frame.sha256))
        return tuple(records)
    except BaseException:
        for key in written:
            try:
                storage.delete_object(key)
            except StorageError:
                pass  # left as an unlinked orphan under the attempt prefix
        raise


# --------------------------------------------------------------------------- handler


class IngestHandler:
    def __init__(self, factory: sessionmaker[DBSession], storage: ObjectStorage, settings: Settings, *,
                 config: MediaConfig | None = None, fanout: FanoutPlanner = plan_downstream_after_ingest) -> None:
        self.factory = factory
        self.storage = fenced_storage(factory, storage)
        self.settings = settings
        self.config = config or load_media_config(MEDIA_CONFIG_PATH)
        self.fanout = fanout
        self._swept = False

    def __call__(self, lease: Lease) -> Publication:
        if not self._swept:
            self._swept = True
            sweep_ingest_scratch(self.settings)
        plan = self._load(lease)
        if isinstance(plan, Publication):
            return plan
        if not plan.config_matches:
            # Invariant 8: never silently run a different media config than the run froze.
            return self._failure_publication(plan, CONFIG_MISMATCH, None)
        uploaded: list[str] = []
        try:
            with scratch_dir(scratch_root_for(self.settings)) as scratch:
                processed = self._process(lease, plan, scratch, uploaded)
        except _Terminal as error:
            self._discard(uploaded)
            return self._failure_publication(plan, error.code, error.raw_sha256)
        except _Retryable as error:
            self._discard(uploaded)
            return self._retry_or_fail(lease, plan, error)
        except Exception as error:  # unexpected: never a clean result, never an unbounded retry
            self._discard(uploaded)
            logging.warning("Ingest attempt %s failed unexpectedly: %s", lease.attempt, type(error).__name__)
            return self._retry_or_fail(lease, plan, _Retryable(INTERNAL_ERROR, "INTERNAL"))
        return self._success_publication(plan, processed)

    # ---- phase 1: validate durable state

    def _load(self, lease: Lease) -> _Plan | Publication:
        terminal = StageFailure(Failure("INVALID_INPUT", retryable=False))
        try:
            run_id = UUID(lease.subject_id)
        except ValueError:
            raise terminal from None
        if lease.session_id is None:
            raise terminal
        with self.factory() as db:
            owner = db.get(Session, lease.session_id)
            run = db.scalar(select(PipelineRun).where(PipelineRun.id == run_id, PipelineRun.session_id == lease.session_id))
            part = None if run is None or run.part_id is None else db.scalar(
                select(Part).where(Part.id == run.part_id, Part.session_id == lease.session_id))
            if owner is None or owner.state not in _LIVE_SESSION_STATES or run is None or part is None:
                raise terminal
            if run.subject_key != f"ingest:{part.id}":
                raise terminal
            if part.state in _SETTLED_PART_STATES:
                return Publication()  # an earlier attempt already published (or the part ended): converge
            if part.state != PartState.UPLOADED or part.raw_key is None:
                raise terminal
            upload = db.scalar(select(Upload).where(Upload.session_id == part.session_id, Upload.part_id == part.id,
                                                    Upload.object_key == part.raw_key))
            if upload is None or upload.state != UploadState.ACCEPTED or ingest_input_hash(upload) != run.input_hash:
                raise terminal
            return _Plan(session_id=part.session_id, run_id=run.id, part_id=part.id, part_no=part.part_no, zone=part.zone,
                         raw_key=part.raw_key, expected_bytes=upload.expected_bytes,
                         config_matches=run.config_hashes.get("media") == self.config.content_hash)

    # ---- phase 2: external work, no database transaction open

    def _process(self, lease: Lease, plan: _Plan, scratch: Path, uploaded: list[str]) -> _Processed:
        source = scratch / "source.bin"
        self._check_deadline(lease)
        raw_sha256 = self._download(plan, source)
        self._check_deadline(lease)
        try:
            analysis = normalize_and_assess(source, scratch, self.config, plan.part_no)
            normalized = analysis.normalized
            if normalized.source_sha256 != raw_sha256:
                raise RuntimeError("scratch copy changed after download")
        except MediaError as error:
            raise self._media_error(error, raw_sha256) from None
        self._check_deadline(lease)
        prefix = attempt_prefix(plan.session_id, plan.part_id, plan.run_id, lease.attempt)
        manifest_bytes = normalized.manifest.to_canonical_json().encode("utf-8")
        manifest_path = scratch / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        proxy_key, manifest_key = f"{prefix}proxy.mp4", f"{prefix}manifest.json"
        artifacts = [_ArtifactSpec("proxy", proxy_key, normalized.output_sha256),
                     _ArtifactSpec("derivative", manifest_key, normalized.manifest.content_hash)]
        self._put(lease, proxy_key, normalized.path, "video/mp4", uploaded)
        self._put(lease, manifest_key, manifest_path, "application/json", uploaded)
        refs: tuple[FrameArtifact, ...] = ()
        if self.config.plan_for(plan.part_no).mode == "stills":  # video parts store no frames at ingest
            try:
                refs = extract_and_store_frames(
                    self.storage, normalized.path, normalized.manifest, analysis.planned_frame_indices,
                    prefix, scratch, self.config, before_each=lambda: self._check_deadline(lease))
            except MediaError as error:
                raise self._media_error(error, raw_sha256) from None
            except StorageError:
                raise _Retryable(STORAGE_UNAVAILABLE) from None
            artifacts.extend(_ArtifactSpec("frame", ref.key, ref.sha256) for ref in refs)
        outcome = IngestOutcome(session_id=plan.session_id, part_id=plan.part_id, part_no=plan.part_no, zone=plan.zone,
                                run_id=plan.run_id, usable_for_scoring=analysis.quality.usable_for_scoring,
                                raw_sha256=raw_sha256, proxy_key=proxy_key, manifest_key=manifest_key, frames=refs)
        return _Processed(raw_sha256, analysis.quality.to_json_dict(), tuple(artifacts), outcome)

    def _download(self, plan: _Plan, target: Path) -> str:
        try:
            downloaded = self.storage.download_to(plan.raw_key, target, plan.expected_bytes)
        except StorageError as error:
            if error.code in _MISSING_CODES:
                raise _Terminal(OBJECT_MISSING) from None
            if error.code == "ObjectTooLarge":
                raise _Terminal(OBJECT_CHANGED) from None
            raise _Retryable(STORAGE_UNAVAILABLE) from None
        if downloaded.size_bytes != plan.expected_bytes:
            raise _Terminal(OBJECT_CHANGED, downloaded.sha256)
        return downloaded.sha256

    def _put(self, lease: Lease, key: str, path: Path, content_type: str, uploaded: list[str]) -> None:
        self._check_deadline(lease)
        uploaded.append(key)  # recorded first: a put that raised may still have written the object
        try:
            self.storage.put_file(key, path, content_type)
        except StorageError:
            raise _Retryable(STORAGE_UNAVAILABLE) from None

    @staticmethod
    def _check_deadline(lease: Lease) -> None:
        if datetime.now(timezone.utc) >= lease.deadline - DEADLINE_MARGIN:
            raise _Retryable(DEADLINE_EXCEEDED)

    @staticmethod
    def _media_error(error: MediaError, raw_sha256: str) -> _Terminal | _Retryable:
        if error.reason in _RETRYABLE_MEDIA:
            return _Retryable(_RETRYABLE_MEDIA[error.reason], raw_sha256=raw_sha256)
        return _Terminal(media_failure_reason(error.reason), raw_sha256)

    def _discard(self, keys: list[str]) -> None:
        for key in keys:
            try:
                self.storage.delete_object(key)
            except StorageError:
                pass  # left as an unlinked orphan under the run prefix

    def _retry_or_fail(self, lease: Lease, plan: _Plan, error: _Retryable) -> Publication:
        if lease.attempt < MAX_ATTEMPTS:
            raise StageFailure(Failure(error.queue_code))
        return self._failure_publication(plan, error.code, error.raw_sha256)  # budget spent: fail, never stall

    # ---- phase 3: atomic publication (runs inside the queue's fenced completion transaction)

    def _success_publication(self, plan: _Plan, processed: _Processed) -> Publication:
        extra = self.fanout(processed.outcome)

        def publish(db: DBSession) -> None:
            part, run = self._lock_rows(db, plan)
            part.raw_sha256 = processed.raw_sha256
            part.quality = processed.quality
            part.failure_reason = None
            part.state = PartState.PROCESSING
            run.state = "running"
            db.add_all(Artifact(session_id=plan.session_id, kind=spec.kind, key=spec.key, sha256=spec.sha256,
                                privacy_state=PrivacyState.PENDING) for spec in processed.artifacts)
            db.flush()
            if extra.publish is not None:
                extra.publish(db)

        return Publication(publish=publish, downstream=extra.downstream)

    def _failure_publication(self, plan: _Plan, reason: str, raw_sha256: str | None) -> Publication:
        def publish(db: DBSession) -> None:
            part, run = self._lock_rows(db, plan)
            part.raw_sha256 = raw_sha256
            part.quality = None
            part.failure_reason = reason
            part.state = PartState.FAILED
            run.state = "failed"
            db.flush()

        return Publication(publish=publish)

    @staticmethod
    def _lock_rows(db: DBSession, plan: _Plan) -> tuple[Part, PipelineRun]:
        part = db.scalar(select(Part).where(Part.id == plan.part_id, Part.session_id == plan.session_id).with_for_update())
        run = db.scalar(select(PipelineRun).where(PipelineRun.id == plan.run_id, PipelineRun.session_id == plan.session_id).with_for_update())
        if (part is None or run is None or part.state != PartState.UPLOADED or part.raw_key != plan.raw_key
                or run.state != "pending"):
            raise PublicationRejected("part or run changed since the attempt started")
        return part, run


def register_ingest_handlers(registry: StageRegistry, factory: sessionmaker[DBSession], storage: ObjectStorage,
                             settings: Settings, *, config: MediaConfig | None = None,
                             fanout: FanoutPlanner = plan_downstream_after_ingest) -> IngestHandler:
    """Install the `ingest` stage. Root wires this into the worker; tests build their own registry."""
    handler = IngestHandler(factory, storage, settings, config=config, fanout=fanout)
    registry.register("ingest", handler)
    return handler
