"""Recovers uploads stranded by crashes, and expires abandoned ones.

Every decision is made in two steps: storage is inspected without locks, then the session row
is locked first and the upload's state re-checked, so concurrent sweeps, API requests and
aborts converge on one terminal outcome. Nothing here accepts or enqueues for a session that
is deleting or deleted; those are left for purge.

Terminal choice for a completed object of the wrong size, or a multipart that vanished: the
upload becomes `aborted` and the part returns to `pending` with a reason code, so the buyer
can retake it (contract 3: retakes are allowed before acceptance; contract 4: aborted
uploads return to pending). `failed` is not used because it is final and no evidence exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
import time
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.storage import NoSuchUpload, ObjectStorage, StorageError
from app.adapters.session_storage import fenced_storage
from app.contracts.enums import PartState, SessionState, UploadState
from app.models import Part, PipelineRun, Session, Upload
from app.settings import Settings
from .problems import ProblemError
from .uploads import (_Attempt, accept_completed_upload, enqueue_ingest_for_accepted_part, verify_pieces)

SIZE_MISMATCH = "UPLOAD_SIZE_MISMATCH"
STORAGE_LOST = "UPLOAD_LOST"
_LIVE_SESSIONS = (SessionState.CAPTURING, SessionState.FINALIZED)


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    MISMATCH_ABORTED = "mismatch_aborted"
    LOST_ABORTED = "lost_aborted"
    REOPENED = "reopened"
    EXPIRED = "expired"
    REPAIRED = "repaired"
    UNAVAILABLE = "unavailable"  # storage could not be asked; retried by the next sweep
    UNCHANGED = "unchanged"  # another actor already settled it, or the session is being deleted


class _Verdict(StrEnum):
    ACCEPT = "accept"
    MISMATCH = "mismatch"
    LOST = "lost"
    REOPEN = "reopen"
    UNAVAILABLE = "unavailable"


@dataclass
class SweepResult:
    outcomes: dict[Outcome, int] = field(default_factory=dict)

    def add(self, outcome: Outcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class UploadReconciler:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage):
        self.sessions = sessions
        self.settings = settings
        self.storage = fenced_storage(sessions, storage)

    def sweep(self, *, limit: int = 25, budget_seconds: float = 30.0) -> SweepResult:
        """Bounded pass: at most `limit` uploads per category and a wall-clock budget."""
        result, deadline = SweepResult(), time.monotonic() + budget_seconds
        steps = ((self._stranded_ids, self.reconcile_completing), (self._expired_ids, self.expire_open),
                 (self._unqueued_ids, self.repair_ingest))
        for find, act in steps:
            for upload_id in find(limit):
                if time.monotonic() >= deadline:
                    return result
                result.add(act(upload_id))
        return result

    # --- candidate discovery (no locks; every action re-validates under the session lock)

    def _stranded_ids(self, limit: int) -> list[UUID]:
        cutoff = func.clock_timestamp() - timedelta(seconds=self.settings.upload_completing_grace_seconds)
        return self._ids(select(Upload.id).where(Upload.state == UploadState.COMPLETING,
                         Upload.updated_at <= cutoff).order_by(Upload.updated_at), limit)

    def _expired_ids(self, limit: int) -> list[UUID]:
        cutoff = func.clock_timestamp() - timedelta(seconds=self.settings.upload_orphan_expiry_seconds)
        return self._ids(select(Upload.id).where(Upload.state == UploadState.OPEN,
                         Upload.created_at <= cutoff).order_by(Upload.created_at), limit)

    def _unqueued_ids(self, limit: int) -> list[UUID]:
        """Accepted uploads whose part never got an ingest run (accepted before T03c)."""
        has_run = exists().where(PipelineRun.part_id == Part.id,
                                 PipelineRun.subject_key == func.concat("ingest:", Part.id))
        return self._ids(select(Upload.id).join(Part, Part.id == Upload.part_id).where(Part.raw_key == Upload.object_key,
                         Upload.state == UploadState.ACCEPTED,
                         Part.state == PartState.UPLOADED, ~has_run).order_by(Upload.accepted_at), limit)

    def _ids(self, query, limit: int) -> list[UUID]:  # type: ignore[no-untyped-def]
        live = exists().where(Session.id == Upload.session_id, Session.state.in_(_LIVE_SESSIONS))
        with self.sessions() as db:
            return list(db.scalars(query.where(live).limit(limit)))

    @staticmethod
    def _lock_owner(db: DBSession, upload_id: UUID) -> Session | None:
        """Session row first, always; the upload's session id is immutable so reading it is safe."""
        session_id = db.scalar(select(Upload.session_id).where(Upload.id == upload_id))
        if session_id is None:
            return None
        return db.scalar(select(Session).where(Session.id == session_id).with_for_update())

    # --- stranded completions

    def reconcile_completing(self, upload_id: UUID) -> Outcome:
        with self.sessions() as db:
            attempt = self._snapshot(db, upload_id, UploadState.COMPLETING)
        if attempt is None:
            return Outcome.UNCHANGED
        verdict = self._inspect(attempt)
        if verdict == _Verdict.UNAVAILABLE:
            return Outcome.UNAVAILABLE
        outcome = self._apply(upload_id, verdict)
        if outcome == Outcome.MISMATCH_ABORTED:
            self._discard_object(attempt.object_key)
        return outcome

    def _inspect(self, attempt: _Attempt) -> _Verdict:
        """Decide from storage alone: the object, else the still-open multipart, else nothing."""
        try:
            size = self.storage.head_size(attempt.object_key)
            if size is not None:
                return self._by_size(size, attempt.expected_bytes)
            try:
                pieces = self.storage.list_pieces(attempt.object_key, attempt.provider_upload_id)
            except NoSuchUpload:
                return self._recheck(attempt)  # it may have been completed between the two calls
            try:
                verify_pieces(attempt.expected_bytes, pieces)
            except ProblemError:
                return _Verdict.REOPEN  # pieces no longer match: let the buyer finish the upload
            try:
                self.storage.complete(attempt.object_key, attempt.provider_upload_id, pieces)
            except NoSuchUpload:
                return self._recheck(attempt)
            except StorageError as error:
                return _Verdict.REOPEN if error.definitive else _Verdict.UNAVAILABLE
            return self._recheck(attempt)
        except StorageError:
            return _Verdict.UNAVAILABLE

    def _recheck(self, attempt: _Attempt) -> _Verdict:
        size = self.storage.head_size(attempt.object_key)
        return _Verdict.LOST if size is None else self._by_size(size, attempt.expected_bytes)

    @staticmethod
    def _by_size(actual: int, expected: int) -> _Verdict:
        return _Verdict.ACCEPT if actual == expected else _Verdict.MISMATCH

    def _apply(self, upload_id: UUID, verdict: _Verdict) -> Outcome:
        with self.sessions.begin() as db:
            owner = self._lock_owner(db, upload_id)
            if owner is None or owner.state not in _LIVE_SESSIONS:
                return Outcome.UNCHANGED
            upload = db.scalar(select(Upload).where(Upload.id == upload_id).with_for_update())
            part = db.scalar(select(Part).where(Part.id == upload.part_id).with_for_update())
            if upload.state != UploadState.COMPLETING:
                return Outcome.UNCHANGED
            if verdict == _Verdict.ACCEPT:
                accept_completed_upload(db, part, upload)
                return Outcome.ACCEPTED
            if verdict == _Verdict.REOPEN:
                upload.state = UploadState.OPEN
                return Outcome.REOPENED
            reason, outcome = ((SIZE_MISMATCH, Outcome.MISMATCH_ABORTED) if verdict == _Verdict.MISMATCH
                               else (STORAGE_LOST, Outcome.LOST_ABORTED))
            self._abort_attempt(upload, part, reason)
            return outcome

    def _discard_object(self, key: str) -> None:
        try:
            self.storage.delete_object(key)  # never accepted or linked; failure leaves an orphan only
        except StorageError:
            pass

    # --- abandoned open uploads

    def expire_open(self, upload_id: UUID) -> Outcome:
        with self.sessions.begin() as db:
            owner = self._lock_owner(db, upload_id)
            if owner is None or owner.state not in _LIVE_SESSIONS:
                return Outcome.UNCHANGED
            upload = db.scalar(select(Upload).where(Upload.id == upload_id).with_for_update())
            cutoff = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=self.settings.upload_orphan_expiry_seconds)
            if upload.state != UploadState.OPEN or upload.created_at > cutoff:
                return Outcome.UNCHANGED
            part = db.scalar(select(Part).where(Part.id == upload.part_id).with_for_update())
            try:
                self.storage.abort(upload.object_key, upload.provider_upload_id)
            except StorageError:
                return Outcome.UNAVAILABLE  # leave it open; the next sweep retries
            self._abort_attempt(upload, part, None)
            return Outcome.EXPIRED

    @staticmethod
    def _abort_attempt(upload: Upload, part: Part, reason: str | None) -> None:
        """Terminal abort; the part is only touched while this is still its active attempt."""
        upload.state = UploadState.ABORTED
        if part.active_upload_id == upload.id:
            part.active_upload_id = None
            if part.state == PartState.UPLOADING:
                part.state = PartState.PENDING
                part.failure_reason = reason

    # --- downstream repair

    def repair_ingest(self, upload_id: UUID) -> Outcome:
        with self.sessions.begin() as db:
            owner = self._lock_owner(db, upload_id)
            if owner is None or owner.state not in _LIVE_SESSIONS:
                return Outcome.UNCHANGED
            upload = db.scalar(select(Upload).where(Upload.id == upload_id).with_for_update())
            part = db.scalar(select(Part).where(Part.id == upload.part_id).with_for_update())
            if upload.state != UploadState.ACCEPTED or part.state != PartState.UPLOADED:
                return Outcome.UNCHANGED  # already processing: ingest exists
            enqueue_ingest_for_accepted_part(db, part, upload)
            return Outcome.REPAIRED

    @staticmethod
    def _snapshot(db: DBSession, upload_id: UUID, state: UploadState) -> _Attempt | None:
        upload = db.scalar(select(Upload).where(Upload.id == upload_id))
        if upload is None or upload.state != state:
            return None
        owner_state = db.scalar(select(Session.state).where(Session.id == upload.session_id))
        if owner_state not in _LIVE_SESSIONS:
            return None
        return _Attempt(upload.id, upload.part_id, upload.state, upload.object_key,
                        upload.provider_upload_id, upload.expected_bytes)
