"""Multipart upload lifecycle. The session row is locked before any part or upload row.

Storage I/O for completion happens outside the lock; each DB transition re-checks state
under it. Acceptance, the part's ingest run and its one ingest job commit atomically;
uploads stranded in `completing` are recovered by `upload_reconciler`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
from math import ceil
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.storage import NoSuchUpload, ObjectStorage, StorageError, StoredPiece
from app.contracts.api import (CreateUploadRequest, CreateUploadResponse, PartAcceptedResponse,
    UploadedPiece, UploadResponse, UploadUrl, UploadUrlsResponse)
from app.contracts.enums import PartState, SessionState, UploadState
from app.jobs.queue import JobSpec, enqueue
from app.models import Part, PipelineRun, Session, Upload, VideoUpload
from app.models.rate_limits import RateLimitBucket
from app.settings import Settings
from .auth import owner_session
from .idempotency import execute_idempotent
from .problems import ProblemError, conflict, not_found, validation

CHUNK_SIZE_BYTES = 8 * 1024 * 1024
_HELD_STATES = (UploadState.OPEN, UploadState.COMPLETING, UploadState.ACCEPTED)


def chunk_count(total_bytes: int) -> int:
    return ceil(total_bytes / CHUNK_SIZE_BYTES)


def piece_size(total_bytes: int, part_number: int) -> int:
    """Every piece is a full chunk except the last."""
    return min(CHUNK_SIZE_BYTES, total_bytes - (part_number - 1) * CHUNK_SIZE_BYTES)


def too_large(detail: str) -> ProblemError:
    return ProblemError(413, "UPLOAD_TOO_LARGE", "Upload too large", detail)


def incomplete(detail: str = "The uploaded pieces do not match the declared file.") -> ProblemError:
    return ProblemError(409, "UPLOAD_INCOMPLETE", "Upload incomplete", detail)


def storage_unavailable() -> ProblemError:
    # Provider detail stays out of responses; credentials and signed URLs never appear here.
    return ProblemError(503, "INTERNAL", "Service unavailable", "Object storage is temporarily unavailable.")


def verify_pieces(expected_bytes: int, pieces: tuple[StoredPiece, ...]) -> None:
    """Contiguous 1..N, exact sizes (all but the last are full chunks) and exact total."""
    count = chunk_count(expected_bytes)
    if tuple(piece.part_number for piece in pieces) != tuple(range(1, count + 1)):
        raise incomplete("Some pieces are missing or unexpected.")
    if any(piece.size_bytes != piece_size(expected_bytes, piece.part_number) for piece in pieces):
        raise incomplete("A piece has an unexpected size.")
    if sum(piece.size_bytes for piece in pieces) != expected_bytes:
        raise incomplete("The uploaded total does not match the declared size.")


def consume_url_refresh(db: DBSession, session_id: UUID, limit: int) -> None:
    """Per-session, per-minute limit in the shared limiter table under a hashed key."""
    now = db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
    window = now.replace(second=0, microsecond=0)
    source = hashlib.sha256(f"upload-url-refresh:{session_id}".encode()).hexdigest()
    statement = insert(RateLimitBucket).values(source_hash=source, window_start=window, count=1)
    statement = statement.on_conflict_do_update(
        index_elements=[RateLimitBucket.source_hash, RateLimitBucket.window_start],
        set_={"count": RateLimitBucket.count + 1}, where=RateLimitBucket.count < limit,
    ).returning(RateLimitBucket.count)
    if db.scalar(statement) is None:
        raise ProblemError(429, "RATE_LIMITED", "Rate limit exceeded", "Too many upload URL requests.",
                           max(1, ceil((window + timedelta(minutes=1) - now).total_seconds())))


MEDIA_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "media.v1.json"


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def ingest_config_hashes() -> dict[str, str]:
    """`MediaConfig.content_hash` of the validated media config, the same value the ingest handler
    computes (invariant 8: a run replays only under the config it was created with).

    Fails closed (the acceptance transaction rolls back and reconciliation retries) if the file is
    unreadable or invalid. The import is local so the API imports the media module only on use.
    """
    from app.pipeline.media import load_media_config
    return {"media": load_media_config(MEDIA_CONFIG_PATH).content_hash}


def ingest_input_hash(upload: Upload) -> str:
    """Derived only from durable accepted state, so repeats and the reconciler agree."""
    return canonical_hash({"upload_id": str(upload.id), "object_key": upload.object_key,
                           "expected_bytes": upload.expected_bytes, "content_type": upload.content_type})


def enqueue_ingest_for_accepted_part(db: DBSession, part: Part, upload: Upload) -> None:
    """Create the part's ingest run and exactly one `ingest` job; idempotent under replay.

    Runs inside the acceptance transaction (session row already locked). No handler exists
    yet: the job stays queued until the ingest card registers one.
    """
    if part.state != PartState.UPLOADED or upload.state != UploadState.ACCEPTED:
        raise conflict()
    input_hash, config_hashes = ingest_input_hash(upload), ingest_config_hashes()
    config_hash, subject_key = canonical_hash(config_hashes), f"ingest:{part.id}"
    run_id = db.scalar(insert(PipelineRun).values(id=uuid4(), session_id=part.session_id, part_id=part.id,
        subject_key=subject_key, input_hash=input_hash, config_hashes=config_hashes,
        config_hash=config_hash).on_conflict_do_nothing().returning(PipelineRun.id))
    if run_id is None:
        run_id = db.scalar(select(PipelineRun.id).where(PipelineRun.subject_key == subject_key,
            PipelineRun.input_hash == input_hash, PipelineRun.config_hash == config_hash))
    try:
        enqueue(db, JobSpec(part.session_id, "part_run", str(run_id), "ingest", input_hash, config_hash))
    except ValueError:
        raise conflict() from None  # session is deleting/deleted: no acceptance, no job


def accept_completed_upload(db: DBSession, part: Part, upload: Upload) -> None:
    """Commit acceptance. Caller holds the session lock; the object's size is already verified."""
    if upload.state != UploadState.COMPLETING or part.state != PartState.UPLOADING:
        raise conflict()
    accepted_at = db.scalar(select(func.clock_timestamp()))  # before mutating: queries autoflush
    upload.accepted_at = accepted_at
    upload.state = UploadState.ACCEPTED
    part.state = PartState.UPLOADED
    part.raw_key = upload.object_key
    part.active_upload_id = None
    db.flush()
    enqueue_ingest_for_accepted_part(db, part, upload)


@dataclass(frozen=True)
class _Attempt:
    """Immutable copy of the DB-owned values needed for storage I/O outside a transaction."""
    upload_id: UUID
    part_id: UUID
    state: UploadState
    object_key: str
    provider_upload_id: str
    expected_bytes: int


class UploadService:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage):
        self.sessions = sessions
        self.settings = settings
        self.storage = storage

    def create(self, token: str, session_id: UUID, request: CreateUploadRequest,
               idempotency_key: str | None) -> CreateUploadResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            self._capturing(owner)
            def start() -> dict[str, object]:
                return self._start(db, owner, request).model_dump(mode="json")
            if idempotency_key is None:
                result = start()
            else:
                result = execute_idempotent(db, authority_id=owner.id, route=f"/sessions/{owner.id}/parts",
                    key=idempotency_key, body=request.model_dump(mode="json"), operation=start)
            return CreateUploadResponse.model_validate(result)

    def _start(self, db: DBSession, owner: Session, request: CreateUploadRequest) -> CreateUploadResponse:
        part = db.scalar(select(Part).where(Part.session_id == owner.id,
                                            Part.part_no == request.part_no).with_for_update())
        if part is None:
            raise not_found()
        if part.state != PartState.PENDING or part.source_video_id is not None:
            raise conflict()  # includes an existing open attempt (part is `uploading`)
        held = db.scalar(select(func.coalesce(func.sum(Upload.expected_bytes), 0)).where(
            Upload.session_id == owner.id, Upload.state.in_(_HELD_STATES)))
        source_held = db.scalar(select(func.coalesce(func.sum(VideoUpload.expected_bytes), 0)).where(
            VideoUpload.session_id == owner.id, VideoUpload.state.in_(_HELD_STATES)))
        if int(held) + int(source_held) + request.content_length > self.settings.session_upload_limit_bytes:
            raise too_large("The session upload limit would be exceeded.")
        upload_id = uuid4()
        key = f"raw/{owner.id}/{part.id}/{upload_id}"
        try:
            provider_id = self.storage.create_multipart(key, request.content_type)
        except StorageError:
            raise storage_unavailable() from None
        try:
            db.add(Upload(id=upload_id, session_id=owner.id, part_id=part.id, object_key=key,
                          provider_upload_id=provider_id, expected_bytes=request.content_length,
                          content_type=request.content_type))
            db.flush()
            part.state = PartState.UPLOADING
            part.active_upload_id = upload_id
            part.failure_reason = None  # a reconciler reason from an earlier attempt is stale now
            db.flush()
        except Exception:
            self._abort_quietly(key, provider_id)
            raise
        return CreateUploadResponse(part_id=part.id, upload_id=upload_id,
                                    chunk_count=chunk_count(request.content_length))

    def urls(self, token: str, upload_id: UUID, part_numbers: tuple[int, ...]) -> UploadUrlsResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            upload = self._upload(db, owner, upload_id, lock=True)
            self._capturing(owner)
            if upload.state != UploadState.OPEN:
                raise conflict()
            if any(number > chunk_count(upload.expected_bytes) for number in part_numbers):
                raise validation()
            consume_url_refresh(db, owner.id, self.settings.upload_url_refresh_limit_per_minute)
            ttl = self.settings.upload_url_ttl_seconds
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
            try:
                signed = [(number, self.storage.presign_piece(upload.object_key, upload.provider_upload_id,
                           number, piece_size(upload.expected_bytes, number), ttl)) for number in part_numbers]
            except StorageError:
                raise storage_unavailable() from None
            return UploadUrlsResponse(items=tuple(UploadUrl(part_number=number, url=piece.url,
                expires_at=expires_at, headers=piece.headers) for number, piece in signed))

    def get(self, token: str, upload_id: UUID) -> UploadResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token)
            attempt = self._attempt(self._upload(db, owner, upload_id))
        pieces: tuple[StoredPiece, ...] = ()
        if attempt.state in (UploadState.OPEN, UploadState.COMPLETING):
            pieces = self._list(attempt)
        limit = chunk_count(attempt.expected_bytes)
        # Anything outside the declared layout is not an accepted piece and must be re-sent.
        usable = [piece for piece in pieces if 1 <= piece.part_number <= limit
                  and 0 < piece.size_bytes <= CHUNK_SIZE_BYTES]
        return UploadResponse(upload_id=attempt.upload_id, state=attempt.state, pieces=tuple(
            UploadedPiece(part_number=piece.part_number, etag=piece.etag, size_bytes=piece.size_bytes)
            for piece in usable))

    def complete(self, token: str, upload_id: UUID) -> PartAcceptedResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token)
            upload = self._upload(db, owner, upload_id)
            settled = self._settled(db, upload)
            if settled is not None:
                return settled
            self._capturing(owner)
            attempt = self._attempt(upload)
        pieces = self._list(attempt)
        verify_pieces(attempt.expected_bytes, pieces)
        settled = self._begin_completing(token, upload_id)
        if settled is not None:
            return settled
        self._finish_object(token, attempt, pieces)
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            upload = self._upload(db, owner, upload_id, lock=True)
            part = self._part(db, upload, lock=True)
            if upload.state == UploadState.COMPLETING:
                accept_completed_upload(db, part, upload)
            return PartAcceptedResponse(part_id=part.id, state=part.state)

    def _begin_completing(self, token: str, upload_id: UUID) -> PartAcceptedResponse | None:
        """Move open -> completing before CompleteMultipartUpload; losers of a race get status."""
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            upload = self._upload(db, owner, upload_id, lock=True)
            settled = self._settled(db, upload)
            if settled is not None:
                return settled
            self._capturing(owner)
            upload.state = UploadState.COMPLETING
            return None

    def _finish_object(self, token: str, attempt: _Attempt, pieces: tuple[StoredPiece, ...]) -> None:
        """Complete using server-listed ETags, then verify the real object size.

        Transient failures and a post-completion size mismatch leave the upload `completing`
        on purpose: the object may exist, so T03c reconciliation decides, never this request.
        """
        try:
            self.storage.complete(attempt.object_key, attempt.provider_upload_id, pieces)
        except NoSuchUpload:
            raise storage_unavailable() from None  # outcome unknown; stays completing
        except StorageError as error:
            if error.definitive:
                self._reopen(token, attempt.upload_id)
                raise incomplete("Object storage rejected the uploaded pieces.") from None
            raise storage_unavailable() from None
        try:
            actual = self.storage.head_size(attempt.object_key)
        except StorageError:
            raise storage_unavailable() from None
        if actual != attempt.expected_bytes:
            raise incomplete("The completed object does not match the declared size.")

    def _reopen(self, token: str, upload_id: UUID) -> None:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            upload = self._upload(db, owner, upload_id, lock=True)
            if upload.state == UploadState.COMPLETING:
                upload.state = UploadState.OPEN

    def abort(self, token: str, upload_id: UUID) -> None:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            upload = self._upload(db, owner, upload_id, lock=True)
            if upload.state == UploadState.ABORTED:
                return
            if upload.state != UploadState.OPEN:
                raise conflict()  # completing or accepted attempts are not abortable
            part = self._part(db, upload, lock=True)
            try:
                self.storage.abort(upload.object_key, upload.provider_upload_id)
            except StorageError:
                raise storage_unavailable() from None
            upload.state = UploadState.ABORTED
            if part.active_upload_id == upload.id:
                part.active_upload_id = None
            if part.state == PartState.UPLOADING:
                part.state = PartState.PENDING
            db.flush()

    def _list(self, attempt: _Attempt) -> tuple[StoredPiece, ...]:
        try:
            return self.storage.list_pieces(attempt.object_key, attempt.provider_upload_id)
        except NoSuchUpload:
            if attempt.state == UploadState.COMPLETING:
                return ()  # the multipart upload is consumed once storage completed it
            # DB says unfinished but storage has no such upload: completed or aborted out of band.
            raise conflict() from None
        except StorageError:
            raise storage_unavailable() from None

    def _settled(self, db: DBSession, upload: Upload) -> PartAcceptedResponse | None:
        if upload.state == UploadState.ABORTED:
            raise conflict()
        if upload.state == UploadState.OPEN:
            return None
        part = self._part(db, upload)
        return PartAcceptedResponse(part_id=part.id, state=part.state)

    @staticmethod
    def _upload(db: DBSession, owner: Session, upload_id: UUID, *, lock: bool = False) -> Upload:
        query = select(Upload).where(Upload.id == upload_id, Upload.session_id == owner.id)
        upload = db.scalar(query.with_for_update() if lock else query)
        if upload is None:
            raise not_found()
        return upload

    @staticmethod
    def _part(db: DBSession, upload: Upload, *, lock: bool = False) -> Part:
        query = select(Part).where(Part.id == upload.part_id, Part.session_id == upload.session_id)
        part = db.scalar(query.with_for_update() if lock else query)
        if part is None:
            raise not_found()
        return part

    @staticmethod
    def _attempt(upload: Upload) -> _Attempt:
        return _Attempt(upload.id, upload.part_id, upload.state, upload.object_key,
                        upload.provider_upload_id, upload.expected_bytes)

    @staticmethod
    def _capturing(owner: Session) -> None:
        if owner.state != SessionState.CAPTURING:
            raise conflict()

    def _abort_quietly(self, key: str, provider_id: str) -> None:
        try:
            self.storage.abort(key, provider_id)
        except StorageError:
            pass  # Orphans are expired by T03c; the original failure is the one to surface.
