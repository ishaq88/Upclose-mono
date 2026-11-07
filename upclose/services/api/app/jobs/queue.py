"""PostgreSQL at-least-once leases with atomic, fenced database publication.

Publishers are trusted application code: local DB work only, no commits, network
or file I/O. External work runs after claim commits and before complete begins.
Session deletion must acquire its session row before locking any of its jobs.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.contracts.enums import SessionState
from app.models import Job, Session

Stage = Literal["ingest", "source_ingest", "vehicle_mask", "segment", "verify", "read_dash", "claims", "score", "narrate", "render", "billing_event", "purge"]
SubjectKind = Literal["part_run", "concept_run", "finding", "duplicate_group", "report_revision", "stripe_event", "session"]
Publisher = Callable[[DBSession], None]
SUBJECTS: dict[str, frozenset[str]] = {
    "source_ingest": frozenset({"session"}),
    "vehicle_mask": frozenset({"part_run"}),
    "ingest": frozenset({"part_run"}), "read_dash": frozenset({"part_run"}),
    "segment": frozenset({"concept_run"}), "verify": frozenset({"finding", "duplicate_group"}),
    "claims": frozenset({"report_revision"}), "score": frozenset({"report_revision"}),
    "narrate": frozenset({"report_revision"}), "render": frozenset({"report_revision"}),
    "billing_event": frozenset({"stripe_event"}), "purge": frozenset({"session"}),
}
FAILURE_CODES = frozenset({"INVALID_INPUT", "UNSUPPORTED", "UNAUTHORIZED", "BUDGET_EXHAUSTED",
                           "SCHEMA_INVALID", "PROVIDER_TIMEOUT", "TRANSIENT", "INTERNAL", "STAGE_UNREGISTERED"})
TERMINAL_CODES = frozenset({"INVALID_INPUT", "UNSUPPORTED", "UNAUTHORIZED", "BUDGET_EXHAUSTED", "STAGE_UNREGISTERED"})


@dataclass(frozen=True, slots=True)
class JobSpec:
    session_id: UUID | None
    subject_kind: SubjectKind
    subject_id: str
    stage: Stage
    input_hash: str
    config_hash: str

    def __post_init__(self) -> None:
        if self.subject_kind not in SUBJECTS.get(self.stage, ()):
            raise ValueError("stage and subject kind disagree")
        if not self.subject_id or len(self.subject_id) > 255:
            raise ValueError("invalid subject identity")
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (self.input_hash, self.config_hash)):
            raise ValueError("input and config hashes must be lowercase SHA256")
        if self.session_id is None and (self.subject_kind, self.stage) != ("stripe_event", "billing_event"):
            raise ValueError("only a Stripe billing event may be global")
        if self.stage == "purge" and self.subject_id != str(self.session_id):
            raise ValueError("purge subject must be its session")


def job_key(spec: JobSpec) -> str:
    values = [spec.subject_kind, spec.subject_id, spec.stage, spec.input_hash, spec.config_hash]
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _allowed(stage: str, state: SessionState, *, after_purge: bool = False) -> bool:
    if stage == "purge":
        return state == SessionState.DELETING or (after_purge and state == SessionState.DELETED)
    return state not in (SessionState.DELETING, SessionState.DELETED)


def enqueue(db: DBSession, spec: JobSpec) -> UUID:
    """Caller owns transaction; subject ownership must be validated upstream.

    Queue identity is polymorphic and does not replace each handler's ownership
    checks. Session lock is acquired here too, including for idempotent replay.
    """
    if spec.session_id is not None:
        owner = db.scalar(select(Session).where(Session.id == spec.session_id).with_for_update())
        if owner is None or not _allowed(spec.stage, owner.state):
            raise ValueError("session cannot accept this job")
    identity = job_key(spec)
    result = db.scalar(insert(Job).values(id=uuid4(), session_id=spec.session_id, subject_kind=spec.subject_kind,
                       subject_id=spec.subject_id, stage=spec.stage, input_hash=spec.input_hash,
                       config_hash=spec.config_hash, idempotency_key=identity).on_conflict_do_nothing(
                           index_elements=[Job.idempotency_key]).returning(Job.id))
    if result is not None:
        return result
    existing = db.execute(select(Job.id, Job.session_id).where(Job.idempotency_key == identity)).one()
    if existing.session_id != spec.session_id:
        raise ValueError("job identity belongs to another authority")
    return existing.id


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    deadline_seconds: int = 600

    def __post_init__(self) -> None:
        if not 0 < self.heartbeat_seconds < self.lease_seconds <= self.deadline_seconds <= 600:
            raise ValueError("invalid queue lease policy")


@dataclass(frozen=True, slots=True)
class Lease:
    job_id: UUID
    session_id: UUID | None
    token: UUID
    attempt: int
    stage: Stage
    subject_kind: SubjectKind
    subject_id: str
    lease_until: datetime
    deadline: datetime


@dataclass(frozen=True, slots=True)
class Failure:
    code: str
    retryable: bool = True
    retry_after_seconds: float = 0

    def __post_init__(self) -> None:
        if self.code not in FAILURE_CODES or not 0 <= self.retry_after_seconds <= 86400:
            raise ValueError("invalid sanitized failure")


class _LostFence(Exception):
    pass


class JobQueue:
    def __init__(self, factory: sessionmaker[DBSession], policy: QueuePolicy = QueuePolicy(),
                 jitter: Callable[[], float] | None = None) -> None:
        self.factory = factory
        self.policy = policy
        self.jitter = jitter or (lambda: secrets.randbelow(501) / 1000)

    def _eligible(self) -> ColumnElement[bool]:
        return or_(and_(Job.state == "queued", Job.available_at <= func.clock_timestamp()),
                   and_(Job.state == "running", or_(Job.lease_until <= func.clock_timestamp(),
                       Job.attempt_started_at + timedelta(seconds=self.policy.deadline_seconds) <= func.clock_timestamp())))

    def claim(self, worker_id: str, stages: Iterable[Stage] | None = None) -> Lease | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("invalid worker identity")
        # Discovery takes no row locks. Every candidate gets a separate short
        # transaction, preserving session -> job lock order on all paths.
        stage_filter = True if stages is None else Job.stage.in_(tuple(stages))
        permitted = or_(and_(Job.session_id.is_(None), Job.stage == "billing_event", Job.subject_kind == "stripe_event"),
                        exists(select(Session.id).where(Session.id == Job.session_id, or_(
                            and_(Job.stage == "purge", Session.state == SessionState.DELETING),
                            and_(Job.stage != "purge", Session.state.in_([SessionState.CAPTURING, SessionState.FINALIZED]))))))
        cursor = None
        while True:
            with self.factory() as db:
                page = True if cursor is None else or_(Job.available_at > cursor[0],
                    and_(Job.available_at == cursor[0], Job.id > cursor[1]))
                candidates = db.execute(select(Job.id, Job.session_id, Job.available_at).where(
                    self._eligible(), stage_filter, permitted, page).order_by(Job.available_at, Job.id).limit(100)).all()
            if not candidates:
                return None
            cursor = (candidates[-1].available_at, candidates[-1].id)
            for candidate in candidates:
                with self.factory.begin() as db:
                    owner = None
                    if candidate.session_id is not None:
                        owner = db.scalar(select(Session).where(Session.id == candidate.session_id).with_for_update(skip_locked=True))
                        if owner is None:
                            continue
                    job = db.scalar(select(Job).where(Job.id == candidate.id, Job.session_id == candidate.session_id,
                                                     self._eligible()).with_for_update(skip_locked=True))
                    if job is None or (owner is not None and not _allowed(job.stage, owner.state)):
                        continue
                    if job.attempts >= 3:
                        self._end(job, "dead", "ATTEMPTS_EXHAUSTED")
                        continue
                    now = db.scalar(select(func.clock_timestamp()))
                    job.state = "running"
                    job.attempts += 1
                    job.attempt_started_at = now
                    job.lease_until = now + timedelta(seconds=self.policy.lease_seconds)
                    job.lease_token = uuid4()
                    job.worker_id = worker_id
                    job.last_error = None
                    db.flush()
                    lease = Lease(job.id, job.session_id, job.lease_token, job.attempts, job.stage,
                                  job.subject_kind, job.subject_id, job.lease_until,
                                  now + timedelta(seconds=self.policy.deadline_seconds))
                return lease  # claim has committed before external work receives it

    def _fence(self, lease: Lease) -> ColumnElement[bool]:
        return and_(Job.id == lease.job_id, Job.session_id == lease.session_id,
                    Job.state == "running", Job.lease_token == lease.token,
                    Job.attempts == lease.attempt, Job.lease_until > func.clock_timestamp(),
                    Job.attempt_started_at + timedelta(seconds=self.policy.deadline_seconds) > func.clock_timestamp())

    def _locked(self, db: DBSession, lease: Lease) -> Job | None:
        owner = None
        if lease.session_id is not None:
            owner = db.scalar(select(Session).where(Session.id == lease.session_id).with_for_update())
            if owner is None:
                return None
        job = db.scalar(select(Job).where(self._fence(lease)).with_for_update())
        if job is None or (owner is not None and not _allowed(job.stage, owner.state)):
            return None
        # PostgreSQL may wait on FOR UPDATE after evaluating its WHERE clause.
        # Requery after acquiring the lock, especially for parentless billing.
        if db.scalar(select(Job.id).where(self._fence(lease))) is None:
            return None
        return job

    @staticmethod
    def _end(job: Job, state: str, code: str | None = None) -> None:
        job.state = state
        job.last_error = code
        job.lease_token = None
        job.lease_until = None
        job.worker_id = None

    def heartbeat(self, lease: Lease) -> bool:
        with self.factory.begin() as db:
            job = self._locked(db, lease)
            if job is None:
                return False
            result = db.scalar(update(Job).where(self._fence(lease)).values(lease_until=func.least(
                func.clock_timestamp() + timedelta(seconds=self.policy.lease_seconds),
                Job.attempt_started_at + timedelta(seconds=self.policy.deadline_seconds)
            )).returning(Job.id).execution_options(synchronize_session=False))
            return result is not None

    def complete(self, lease: Lease, publish: Publisher | None = None,
                 downstream: Iterable[JobSpec] = ()) -> bool:
        try:
            with self.factory.begin() as db:
                job = self._locked(db, lease)
                if job is None:
                    return False
                if publish is not None:
                    publish(db)
                # Downstream session changes are forbidden: all work in this
                # atomic publication shares the already locked parent authority.
                for spec in downstream:
                    if spec.session_id != lease.session_id:
                        raise ValueError("downstream job belongs to another authority")
                    enqueue(db, spec)
                db.flush()
                authority = True
                if lease.session_id is not None:
                    states = [SessionState.DELETING, SessionState.DELETED] if job.stage == "purge" else [SessionState.CAPTURING, SessionState.FINALIZED]
                    authority = exists(select(Session.id).where(Session.id == lease.session_id, Session.state.in_(states)))
                committed = db.scalar(update(Job).where(self._fence(lease), authority).values(
                    state="succeeded", lease_token=None, lease_until=None, worker_id=None, last_error=None
                ).returning(Job.id).execution_options(synchronize_session=False))
                if committed is None:
                    raise _LostFence
            return True
        except _LostFence:
            return False

    def fail(self, lease: Lease, failure: Failure) -> bool:
        with self.factory.begin() as db:
            job = self._locked(db, lease)
            if job is None:
                return False
            now = db.scalar(select(func.clock_timestamp()))
            jitter = self.jitter()
            if not 0 <= jitter <= 0.5:
                raise ValueError("retry jitter must be bounded to 0..0.5 seconds")
            delay = max((2 if job.attempts == 1 else 8) + jitter, failure.retry_after_seconds)
            deadline = job.attempt_started_at + timedelta(seconds=self.policy.deadline_seconds)
            retry = (failure.retryable and failure.code not in TERMINAL_CODES and job.attempts < 3
                     and now + timedelta(seconds=delay) < deadline)
            values = {"state": "queued" if retry else "dead", "last_error": failure.code,
                      "lease_token": None, "lease_until": None, "worker_id": None}
            if retry:
                values["available_at"] = now + timedelta(seconds=delay)
            result = db.scalar(update(Job).where(self._fence(lease)).values(**values).returning(
                Job.id).execution_options(synchronize_session=False))
            return result is not None
