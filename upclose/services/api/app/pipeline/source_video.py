"""Lease-fenced source-video ingest. No inferred zones and no live provider calls.

External work produces attempt-scoped objects. A single Publication commits source state,
all derived parts, ingest runs, artifacts and downstream jobs under the queue's session/
lease fence. Retries may leave unlinked objects; every key stays under the session purge
prefix. The source-upload reconciler settles dead jobs and releases pending reservations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.session_storage import fenced_storage
from app.adapters.storage import ObjectStorage, StorageError
from app.contracts.enums import PartState, PrivacyState, SessionState, UploadState, Zone
from app.contracts.video import VideoRange
from app.jobs.queue import Failure, Lease
from app.jobs.worker import Publication, StageFailure, StageRegistry
from app.models import Artifact, Job, Part, PipelineRun, Session, VideoUpload
from app.pipeline.ingest import (
    FanoutPlanner, IngestOutcome, PublicationRejected, attempt_prefix,
    extract_and_store_frames, plan_downstream_after_ingest, scratch_root_for,
)
from app.pipeline.media import (
    MediaError, MediaReason, build_manifest, load_media_config, plan_frame_indices, probe, scratch_dir,
)
from app.pipeline.orchestration import canonical_hash
from app.pipeline.quality import measure_quality
from app.pipeline.source_mapping import (
    MAPPING_UNAVAILABLE, MAPPING_UNVERIFIED, MappingInput, UnavailableAreaMapper,
    load_source_config, source_config_hash, source_media_config, validate_ranges,
)
from app.pipeline.source_media import normalize_source_range
from app.services.video_uploads import source_input_hash
from app.settings import Settings


@dataclass(frozen=True, slots=True)
class ReservedPart:
    id: UUID
    part_no: int
    zone: Zone
    run_id: UUID


@dataclass(frozen=True, slots=True)
class SourcePlan:
    id: UUID
    session_id: UUID
    object_key: str
    expected_bytes: int
    input_hash: str
    config_matches: bool
    segments: tuple[VideoRange, ...]
    parts: tuple[ReservedPart, ...]


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    kind: str
    key: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ProcessedPart:
    part: ReservedPart
    quality: dict[str, object]
    artifacts: tuple[ArtifactSpec, ...]
    outcome: IngestOutcome


class SourceFailure(Exception):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class SourceVideoHandler:
    def __init__(self, factory: sessionmaker[DBSession], storage: ObjectStorage, settings: Settings,
                 *, fanout: FanoutPlanner = plan_downstream_after_ingest) -> None:
        self.factory = factory
        self.storage = fenced_storage(factory, storage)
        self.settings = settings
        self.media = load_media_config()
        self.config = load_source_config()
        self.config_hash = source_config_hash()
        self.source_media = source_media_config(self.config, self.media)
        self.fanout = fanout

    def __call__(self, lease: Lease) -> Publication:
        plan = self._load(lease)
        if isinstance(plan, Publication):
            return plan
        if not plan.config_matches:
            return self._failed(plan, "VIDEO_CONFIG_MISMATCH")
        written: list[str] = []
        try:
            with scratch_dir(scratch_root_for(self.settings)) as scratch:
                raw_sha256, duration_ms, parts = self._process(lease, plan, scratch, written)
        except SourceFailure as error:
            self._discard(written)
            return self._retry_or_fail(lease, plan, error.reason, error.retryable)
        except MediaError as error:
            self._discard(written)
            retry = error.reason in (MediaReason.TIMEOUT, MediaReason.RESOURCE_LIMIT)
            return self._retry_or_fail(lease, plan, f"VIDEO_MEDIA_{error.reason.name}", retry)
        except StorageError:
            self._discard(written)
            return self._retry_or_fail(lease, plan, "VIDEO_STORAGE_UNAVAILABLE", True)
        except Exception:
            self._discard(written)
            return self._retry_or_fail(lease, plan, "VIDEO_PROCESSING_FAILED", True)
        return self._success(plan, raw_sha256, duration_ms, parts)

    def _load(self, lease: Lease) -> SourcePlan | Publication:
        invalid = StageFailure(Failure("INVALID_INPUT", retryable=False))
        try:
            source_id = UUID(lease.subject_id)
        except ValueError:
            raise invalid from None
        if lease.stage != "source_ingest" or lease.subject_kind != "session" or lease.session_id is None:
            raise invalid
        with self.factory() as db:
            owner = db.get(Session, lease.session_id)
            source = db.get(VideoUpload, source_id)
            job = db.get(Job, lease.job_id)
            if (owner is None or owner.state != SessionState.CAPTURING or owner.capture_mode != "single_video"
                    or source is None or source.session_id != owner.id or source.state != UploadState.ACCEPTED
                    or job is None or job.session_id != owner.id or job.subject_id != str(source.id)
                    or job.stage != "source_ingest" or job.input_hash != source_input_hash(source)):
                raise invalid
            if source.processing_state in ("ready", "failed"):
                return Publication()
            if source.processing_state != "queued":
                raise invalid
            segments = tuple(VideoRange.model_validate(value) for value in source.segments)
            parts = list(db.scalars(select(Part).where(Part.session_id == owner.id,
                                                       Part.source_video_id == source.id).order_by(Part.part_no)))
            if ({part.part_no for part in parts} != {segment.part_no for segment in segments}
                    or any(part.state != PartState.PENDING or part.raw_key is not None or part.active_upload_id
                           for part in parts)):
                raise invalid
            return SourcePlan(source.id, owner.id, source.object_key, source.expected_bytes, job.input_hash,
                              job.config_hash == self.config_hash, segments,
                              tuple(ReservedPart(part.id, part.part_no, part.zone,
                                                 uuid5(source.id, f"ingest:{part.id}:{job.input_hash}:{job.config_hash}"))
                                    for part in parts))

    def _process(self, lease: Lease, plan: SourcePlan, scratch: Path,
                 written: list[str]) -> tuple[str, int, tuple[ProcessedPart, ...]]:
        self._deadline(lease)
        source = scratch / "source.bin"
        try:
            downloaded = self.storage.download_to(plan.object_key, source, plan.expected_bytes)
        except StorageError as error:
            if error.code in ("404", "NoSuchKey", "NotFound", "ObjectTooLarge"):
                raise SourceFailure("VIDEO_OBJECT_MISSING_OR_CHANGED") from None
            raise
        if downloaded.size_bytes != plan.expected_bytes:
            raise SourceFailure("VIDEO_OBJECT_MISSING_OR_CHANGED")
        info = probe(source, self.source_media, scratch)
        duration_ms = max(1, round(info.duration_s * 1000))
        try:
            validate_ranges(plan.segments, duration_ms, self.config)
        except ValueError:
            raise SourceFailure("VIDEO_RANGE_INVALID") from None
        self._deadline(lease)
        if not plan.segments:
            mapping = UnavailableAreaMapper().map(MappingInput(downloaded.sha256, duration_ms))
            if mapping.state != "unavailable" or mapping.ranges:
                raise SourceFailure("VIDEO_MAPPING_UNAVAILABLE")
            return downloaded.sha256, duration_ms, ()
        manifest = build_manifest(source, self.source_media, scratch, proxy_sha256=downloaded.sha256,
                                  width=info.display_width, height=info.display_height)
        derived: list[ProcessedPart] = []
        by_number = {part.part_no: part for part in plan.parts}
        for segment in plan.segments:
            self._deadline(lease)
            part = by_number[segment.part_no]
            work = scratch / f"part-{part.part_no}"
            work.mkdir(mode=0o700)
            result = normalize_source_range(source, work, self.media, info, manifest, segment, downloaded.sha256)
            normalized = result.normalized
            quality = measure_quality(normalized.path, normalized.manifest, work, self.media,
                                      source_display_size=(info.display_width, info.display_height)).to_json_dict()
            quality["media_usable_for_scoring"] = quality["usable_for_scoring"]
            quality["usable_for_scoring"] = False
            quality["source_video"] = {
                "source_upload_id": str(plan.id), "source_sha256": downloaded.sha256,
                "start_ms": segment.start_ms, "end_ms": segment.end_ms,
                "mapping_basis": "user_declared", "frame_timestamps_ms": list(result.frame_timestamps_ms),
            }
            warnings = quality["warnings"]
            assert isinstance(warnings, list)
            warnings.append({"code": "source_mapping_unverified", "severity": "strong", "blocks_scoring": True,
                             "message": "The marked area is user-declared and has not been visually verified.",
                             "value": None, "threshold": None})
            prefix = attempt_prefix(plan.session_id, part.id, part.run_id, lease.attempt)
            proxy_key, manifest_key = prefix + "proxy.mp4", prefix + "manifest.json"
            manifest_path = work / "manifest.json"
            manifest_path.write_text(normalized.manifest.to_canonical_json(), encoding="utf-8")
            self._put(lease, proxy_key, normalized.path, "video/mp4", written)
            self._put(lease, manifest_key, manifest_path, "application/json", written)
            artifacts = [ArtifactSpec("proxy", proxy_key, normalized.output_sha256),
                         ArtifactSpec("derivative", manifest_key, normalized.manifest.content_hash)]
            frames = ()
            frame_plan = self.media.plan_for(part.part_no)
            if frame_plan.mode == "stills":
                frames = extract_and_store_frames(self.storage, normalized.path, normalized.manifest,
                                                  plan_frame_indices(normalized.manifest, frame_plan), prefix,
                                                  work, self.media, before_each=lambda: self._deadline(lease))
                written.extend(frame.key for frame in frames)
                artifacts.extend(ArtifactSpec("frame", frame.key, frame.sha256) for frame in frames)
            outcome = IngestOutcome(plan.session_id, part.id, part.part_no, part.zone, part.run_id, False,
                                    downloaded.sha256, proxy_key, manifest_key, frames)
            derived.append(ProcessedPart(part, quality, tuple(artifacts), outcome))
        self._deadline(lease)
        return downloaded.sha256, duration_ms, tuple(derived)

    @staticmethod
    def _deadline(lease: Lease) -> None:
        if datetime.now(timezone.utc) >= lease.deadline - timedelta(seconds=5):
            raise SourceFailure("VIDEO_PROCESSING_TIMEOUT", retryable=True)

    def _put(self, lease: Lease, key: str, path: Path, mime: str, written: list[str]) -> None:
        self._deadline(lease)
        written.append(key)
        self.storage.put_file(key, path, mime)

    def _discard(self, keys: list[str]) -> None:
        for key in keys:
            try:
                self.storage.delete_object(key)
            except StorageError:
                pass

    def _retry_or_fail(self, lease: Lease, plan: SourcePlan, reason: str, retryable: bool) -> Publication:
        if retryable and lease.attempt < 3:
            raise StageFailure(Failure("TRANSIENT"))
        return self._failed(plan, reason)

    @staticmethod
    def _lock(db: DBSession, plan: SourcePlan) -> tuple[VideoUpload, list[Part]]:
        source = db.scalar(select(VideoUpload).where(VideoUpload.id == plan.id,
                                                     VideoUpload.session_id == plan.session_id).with_for_update())
        parts = list(db.scalars(select(Part).where(Part.session_id == plan.session_id,
                                                   Part.source_video_id == plan.id).order_by(Part.part_no)
                               .with_for_update()))
        if (source is None or source.state != UploadState.ACCEPTED or source.processing_state != "queued"
                or source_input_hash(source) != plan.input_hash
                or {part.id for part in parts} != {part.id for part in plan.parts}
                or any(part.state != PartState.PENDING or part.raw_key is not None or part.active_upload_id
                       for part in parts)):
            raise PublicationRejected("source or reserved parts changed")
        return source, parts

    def _failed(self, plan: SourcePlan, reason: str) -> Publication:
        def publish(db: DBSession) -> None:
            source, parts = self._lock(db, plan)
            source.processing_state, source.reason = "failed", reason
            for part in parts:
                part.source_video_id = None
            db.flush()
        return Publication(publish)

    def _success(self, plan: SourcePlan, raw_sha256: str, duration_ms: int,
                 processed: tuple[ProcessedPart, ...]) -> Publication:
        extras = tuple(self.fanout(item.outcome) for item in processed)

        def publish(db: DBSession) -> None:
            source, parts = self._lock(db, plan)
            source.raw_sha256, source.duration_ms = raw_sha256, duration_ms
            source.processing_state = "ready"
            source.reason = MAPPING_UNVERIFIED if processed else MAPPING_UNAVAILABLE
            by_id = {part.id: part for part in parts}
            for item in processed:
                part = by_id[item.part.id]
                part.state, part.raw_sha256, part.quality = PartState.PROCESSING, raw_sha256, item.quality
                part.failure_reason = None
                hashes = {"media": self.media.content_hash, "source_video": self.config_hash}
                db.add(PipelineRun(id=item.part.run_id, session_id=plan.session_id, part_id=part.id,
                                   subject_key=f"ingest:{part.id}", input_hash=canonical_hash({
                                       "source_input": plan.input_hash, "source_sha256": raw_sha256,
                                       "part_no": part.part_no}), config_hashes=hashes,
                                   config_hash=canonical_hash(hashes), state="running"))
                db.add_all(Artifact(session_id=plan.session_id, kind=spec.kind, key=spec.key, sha256=spec.sha256,
                                    privacy_state=PrivacyState.PENDING) for spec in item.artifacts)
            db.flush()
            for extra in extras:
                if extra.publish:
                    extra.publish(db)
        return Publication(publish, tuple(spec for extra in extras for spec in extra.downstream))


def register_source_video_handlers(registry: StageRegistry, factory: sessionmaker[DBSession],
                                   storage: ObjectStorage, settings: Settings, *,
                                   fanout: FanoutPlanner = plan_downstream_after_ingest) -> SourceVideoHandler:
    handler = SourceVideoHandler(factory, storage, settings, fanout=fanout)
    registry.register("source_ingest", handler)
    return handler
