"""Owner source-video multipart lifecycle and bounded crash reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from uuid import UUID, uuid4

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.session_storage import fenced_storage
from app.adapters.storage import NoSuchUpload, ObjectStorage, StorageError, StoredPiece
from app.contracts.api import UploadedPiece, UploadUrl, UploadUrlsResponse
from app.contracts.enums import PartState, SessionState, UploadState
from app.contracts.video import (CreateVideoUploadRequest, CreateVideoUploadResponse,
    VideoAcceptedResponse, VideoSourceResponse, VideoUploadResponse)
from app.jobs.queue import JobSpec, enqueue
from app.models import Job, Part, Session, Upload, VideoUpload
from app.settings import Settings
from .auth import owner_session
from .idempotency import execute_idempotent
from .problems import ProblemError, conflict, not_found, validation
from .uploads import (CHUNK_SIZE_BYTES, canonical_hash, chunk_count, consume_url_refresh,
    incomplete, piece_size, storage_unavailable, too_large, verify_pieces)

_HELD = (UploadState.OPEN, UploadState.COMPLETING, UploadState.ACCEPTED)


def source_config_hash() -> str:
    from app.pipeline.source_mapping import source_config_hash as config_hash
    return config_hash()


def source_input_hash(upload: VideoUpload) -> str:
    return canonical_hash({"upload_id": str(upload.id), "object_key": upload.object_key,
        "expected_bytes": upload.expected_bytes, "content_type": upload.content_type,
        "segments": upload.segments})


def accept_source(db: DBSession, upload: VideoUpload) -> None:
    """Caller holds owner lock and has verified the finalized storage object."""
    if upload.state != UploadState.COMPLETING:
        raise conflict()
    upload.accepted_at = db.scalar(select(func.clock_timestamp()))
    upload.state = UploadState.ACCEPTED
    upload.processing_state = "queued"
    db.flush()
    enqueue(db, JobSpec(upload.session_id, "session", str(upload.id), "source_ingest",
                        source_input_hash(upload), source_config_hash()))


def release_reservations(db: DBSession, upload: VideoUpload) -> None:
    for part in db.scalars(select(Part).where(Part.session_id == upload.session_id,
            Part.source_video_id == upload.id, Part.state == PartState.PENDING).with_for_update()):
        part.source_video_id = None


@dataclass(frozen=True)
class SourceAttempt:
    id: UUID
    session_id: UUID
    state: UploadState
    object_key: str
    provider_upload_id: str
    expected_bytes: int

    @classmethod
    def of(cls, upload: VideoUpload) -> SourceAttempt:
        return cls(upload.id, upload.session_id, upload.state, upload.object_key,
                   upload.provider_upload_id, upload.expected_bytes)


class VideoUploadService:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage):
        self.sessions, self.settings = sessions, settings
        self.storage = fenced_storage(sessions, storage)

    def create(self, token: str, session_id: UUID, request: CreateVideoUploadRequest,
               idempotency_key: str | None) -> CreateVideoUploadResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            self._capturing(owner)
            if owner.capture_mode != "single_video":
                raise conflict()
            def start() -> dict[str, object]:
                return self._start(db, owner, request).model_dump(mode="json")
            result = start() if idempotency_key is None else execute_idempotent(db,
                authority_id=owner.id, route=f"/sessions/{owner.id}/video", key=idempotency_key,
                body=request.model_dump(mode="json"), operation=start)
            return CreateVideoUploadResponse.model_validate(result)

    def _start(self, db: DBSession, owner: Session, request: CreateVideoUploadRequest) -> CreateVideoUploadResponse:
        if db.scalar(select(VideoUpload.id).where(VideoUpload.session_id == owner.id,
                VideoUpload.state.in_(_HELD)).limit(1)):
            raise conflict()
        parts = list(db.scalars(select(Part).where(Part.session_id == owner.id,
            Part.part_no.in_([segment.part_no for segment in request.segments])).with_for_update()))
        if len(parts) != len(request.segments) or any(part.state != PartState.PENDING or
                part.source_video_id is not None for part in parts):
            raise conflict()
        held = db.scalar(select(func.coalesce(func.sum(Upload.expected_bytes), 0)).where(
            Upload.session_id == owner.id, Upload.state.in_(_HELD)))
        if int(held) + request.content_length > self.settings.session_upload_limit_bytes:
            raise too_large("The session upload limit would be exceeded.")
        upload_id = uuid4()
        key = f"raw/{owner.id}/source/{upload_id}"
        try:
            provider_id = self.storage.create_multipart(key, request.content_type)
        except StorageError:
            raise storage_unavailable() from None
        try:
            db.add(VideoUpload(id=upload_id, session_id=owner.id, object_key=key,
                provider_upload_id=provider_id, expected_bytes=request.content_length,
                content_type=request.content_type,
                segments=[segment.model_dump(mode="json") for segment in request.segments]))
            db.flush()
            for part in parts:
                part.source_video_id = upload_id
            db.flush()
        except Exception:
            try:
                self.storage.abort(key, provider_id)
            except StorageError:
                pass
            raise
        return CreateVideoUploadResponse(upload_id=upload_id, chunk_count=chunk_count(request.content_length))

    def current(self, token: str, session_id: UUID) -> VideoSourceResponse:
        with self.sessions() as db:
            owner = owner_session(db, token, session_id)
            upload = db.scalar(select(VideoUpload).where(VideoUpload.session_id == owner.id,
                                                        VideoUpload.state.in_(_HELD)))
            return VideoSourceResponse(source=self._response(upload) if upload else None)

    def get(self, token: str, upload_id: UUID) -> VideoUploadResponse:
        with self.sessions() as db:
            owner = owner_session(db, token)
            upload = self._upload(db, owner, upload_id)
            response, attempt = self._response(upload), SourceAttempt.of(upload)
        pieces = self._list(attempt) if attempt.state in (UploadState.OPEN, UploadState.COMPLETING) else ()
        return response.model_copy(update={"pieces": tuple(UploadedPiece(part_number=piece.part_number,
            etag=piece.etag, size_bytes=piece.size_bytes) for piece in pieces
            if 1 <= piece.part_number <= chunk_count(attempt.expected_bytes)
            and 0 < piece.size_bytes <= CHUNK_SIZE_BYTES)})

    def urls(self, token: str, upload_id: UUID, part_numbers: tuple[int, ...]) -> UploadUrlsResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state != UploadState.OPEN:
                raise conflict()
            if any(number < 1 or number > chunk_count(upload.expected_bytes) for number in part_numbers):
                raise validation()
            consume_url_refresh(db, owner.id, self.settings.upload_url_refresh_limit_per_minute)
            ttl = self.settings.upload_url_ttl_seconds
            expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
            try:
                signed = [(number, self.storage.presign_piece(upload.object_key, upload.provider_upload_id,
                    number, piece_size(upload.expected_bytes, number), ttl)) for number in part_numbers]
            except StorageError:
                raise storage_unavailable() from None
            return UploadUrlsResponse(items=tuple(UploadUrl(part_number=number, url=piece.url,
                expires_at=expires, headers=piece.headers) for number, piece in signed))

    def complete(self, token: str, upload_id: UUID) -> VideoAcceptedResponse:
        with self.sessions() as db:
            owner = owner_session(db, token)
            upload = self._upload(db, owner, upload_id)
            self._capturing(owner)
            if upload.state != UploadState.OPEN:
                return self._accepted(upload)
            attempt = SourceAttempt.of(upload)
        pieces = self._list(attempt)
        verify_pieces(attempt.expected_bytes, pieces)
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state != UploadState.OPEN:
                return self._accepted(upload)
            upload.state = UploadState.COMPLETING
        try:
            self.storage.complete(attempt.object_key, attempt.provider_upload_id, pieces)
        except StorageError as error:
            if error.definitive and not isinstance(error, NoSuchUpload):
                with self.sessions.begin() as db:
                    owner = owner_session(db, token, lock=True)
                    self._capturing(owner)
                    upload = self._upload(db, owner, upload_id)
                    if upload.state == UploadState.COMPLETING:
                        upload.state = UploadState.OPEN
                raise incomplete("Object storage rejected the uploaded pieces.") from None
            raise storage_unavailable() from None
        try:
            actual = self.storage.head_size(attempt.object_key)
        except StorageError:
            # Completion may already be durable. Never reopen based on a failed HEAD.
            raise storage_unavailable() from None
        if actual != attempt.expected_bytes:
            raise incomplete("The completed object does not match the declared size.")
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state == UploadState.COMPLETING:
                accept_source(db, upload)
            return self._accepted(upload)

    def abort(self, token: str, upload_id: UUID) -> None:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state == UploadState.ABORTED:
                return
            if upload.state != UploadState.OPEN:
                raise conflict()
            try:
                self.storage.abort(upload.object_key, upload.provider_upload_id)
            except StorageError:
                raise storage_unavailable() from None
            upload.state = UploadState.ABORTED
            release_reservations(db, upload)

    def _list(self, attempt: SourceAttempt) -> tuple[StoredPiece, ...]:
        try:
            return self.storage.list_pieces(attempt.object_key, attempt.provider_upload_id)
        except NoSuchUpload:
            if attempt.state == UploadState.COMPLETING:
                return ()
            raise conflict() from None
        except StorageError:
            raise storage_unavailable() from None

    @staticmethod
    def _upload(db: DBSession, owner: Session, upload_id: UUID) -> VideoUpload:
        upload = db.scalar(select(VideoUpload).where(VideoUpload.id == upload_id,
                                                    VideoUpload.session_id == owner.id))
        if upload is None:
            raise not_found()
        return upload

    @staticmethod
    def _capturing(owner: Session) -> None:
        if owner.state != SessionState.CAPTURING:
            raise conflict()

    @staticmethod
    def _response(upload: VideoUpload) -> VideoUploadResponse:
        return VideoUploadResponse(upload_id=upload.id, state=upload.state,
            processing_state=upload.processing_state, reason=upload.reason,
            expected_bytes=upload.expected_bytes, content_type=upload.content_type,
            duration_ms=upload.duration_ms, segments=tuple(upload.segments))

    @staticmethod
    def _accepted(upload: VideoUpload) -> VideoAcceptedResponse:
        if upload.state == UploadState.ABORTED:
            raise conflict()
        return VideoAcceptedResponse(upload_id=upload.id, state=upload.state,
                                     processing_state=upload.processing_state)


class VideoUploadReconciler:
    """Bounded sweeps settle completion crashes, expired attempts and dead source jobs."""
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage):
        self.sessions, self.settings = sessions, settings
        self.storage = fenced_storage(sessions, storage)

    def sweep(self, *, limit: int = 25, budget_seconds: float = 30.0) -> dict[str, int]:
        deadline = time.monotonic() + budget_seconds
        outcomes: dict[str, int] = {}
        with self.sessions() as db:
            completing_cutoff = func.clock_timestamp() - timedelta(seconds=self.settings.upload_completing_grace_seconds)
            orphan_cutoff = func.clock_timestamp() - timedelta(seconds=self.settings.upload_orphan_expiry_seconds)
            has_job = exists().where(Job.session_id == VideoUpload.session_id,
                Job.subject_kind == "session", Job.subject_id == func.cast(VideoUpload.id, Job.subject_id.type),
                Job.stage == "source_ingest")
            terminal_job = exists().where(Job.session_id == VideoUpload.session_id,
                Job.subject_kind == "session", Job.subject_id == func.cast(VideoUpload.id, Job.subject_id.type),
                Job.stage == "source_ingest", Job.state.in_(("dead", "succeeded")))
            pending = list(db.scalars(select(VideoUpload.id).join(Session).where(
                Session.state == SessionState.CAPTURING,
                ((VideoUpload.state == UploadState.COMPLETING) & (VideoUpload.updated_at <= completing_cutoff)) |
                ((VideoUpload.state == UploadState.OPEN) & (VideoUpload.created_at <= orphan_cutoff)) |
                ((VideoUpload.state == UploadState.ACCEPTED) & (VideoUpload.processing_state.in_(("pending", "queued"))) &
                 (~has_job | terminal_job))
            ).order_by(VideoUpload.updated_at).limit(limit)))
        for upload_id in pending:
            if time.monotonic() >= deadline:
                break
            outcome = self.reconcile(upload_id)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return outcomes

    def reconcile(self, upload_id: UUID) -> str:
        with self.sessions() as db:
            upload = db.get(VideoUpload, upload_id)
            if upload is None:
                return "unchanged"
            attempt = SourceAttempt.of(upload)
        verdict = self._inspect(attempt) if attempt.state == UploadState.COMPLETING else None
        if verdict == "unavailable":
            return verdict
        with self.sessions.begin() as db:
            owner = db.scalar(select(Session).where(Session.id == attempt.session_id).with_for_update())
            if owner is None or owner.state != SessionState.CAPTURING:
                return "unchanged"
            upload = db.get(VideoUpload, upload_id)
            if upload.state != attempt.state:
                return "unchanged"
            if upload.state == UploadState.COMPLETING:
                if verdict == "accept":
                    accept_source(db, upload)
                    return "accepted"
                if verdict == "reopen":
                    upload.state = UploadState.OPEN
                    return "reopened"
                upload.state = UploadState.ABORTED
                upload.reason = "UPLOAD_SIZE_MISMATCH" if verdict == "mismatch" else "UPLOAD_LOST"
                release_reservations(db, upload)
                # Never accepted. Purge retries deletion if cleanup is temporarily unavailable.
                try:
                    self.storage.delete_object(upload.object_key)
                    self.storage.abort(upload.object_key, upload.provider_upload_id)
                except StorageError:
                    pass
                return "aborted"
            if upload.state == UploadState.OPEN:
                cutoff = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=self.settings.upload_orphan_expiry_seconds)
                if upload.created_at > cutoff:
                    return "unchanged"
                try:
                    self.storage.abort(upload.object_key, upload.provider_upload_id)
                except StorageError:
                    return "unavailable"
                upload.state = UploadState.ABORTED
                release_reservations(db, upload)
                return "expired"
            if upload.state == UploadState.ACCEPTED and upload.processing_state in ("pending", "queued"):
                job = db.scalar(select(Job).where(Job.session_id == owner.id, Job.subject_kind == "session",
                    Job.subject_id == str(upload.id), Job.stage == "source_ingest"))
                if job is not None and job.state in ("dead", "succeeded"):
                    # Succeeded without a source publication is corrupt/incomplete, never clean.
                    upload.processing_state, upload.reason = "failed", "VIDEO_PROCESSING_FAILED"
                    release_reservations(db, upload)
                    return "failed"
                if job is None:
                    enqueue(db, JobSpec(owner.id, "session", str(upload.id), "source_ingest",
                                        source_input_hash(upload), source_config_hash()))
                    upload.processing_state = "queued"
                    return "repaired"
            return "unchanged"

    def _inspect(self, attempt: SourceAttempt) -> str:
        try:
            size = self.storage.head_size(attempt.object_key)
            if size is not None:
                return "accept" if size == attempt.expected_bytes else "mismatch"
            try:
                pieces = self.storage.list_pieces(attempt.object_key, attempt.provider_upload_id)
            except NoSuchUpload:
                return self._recheck(attempt)
            try:
                verify_pieces(attempt.expected_bytes, pieces)
            except ProblemError:
                return "reopen"
            try:
                self.storage.complete(attempt.object_key, attempt.provider_upload_id, pieces)
            except NoSuchUpload:
                return self._recheck(attempt)
            except StorageError as error:
                return "reopen" if error.definitive else "unavailable"
            return self._recheck(attempt)
        except StorageError:
            return "unavailable"

    def _recheck(self, attempt: SourceAttempt) -> str:
        size = self.storage.head_size(attempt.object_key)
        return "lost" if size is None else ("accept" if size == attempt.expected_bytes else "mismatch")
