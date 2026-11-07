"""Owner deletion commands and durable recovery; no storage I/O in transactions."""
from datetime import timedelta
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.api import DeleteMediaResponse
from app.contracts.enums import SessionState
from app.jobs.queue import JobSpec, enqueue
from app.models import Base, Entitlement, Job, Purchase, Report, Session
from app.services.auth import authenticate
from app.services.problems import not_found
from app.adapters.session_storage import media_guard
from app.adapters.storage import ObjectStorage, StorageError

PURGE_HASH = sha256(b"upclose-purge-v1").hexdigest()


@dataclass(frozen=True, slots=True)
class DeletedMediaAudit:
    cursor: UUID | None
    inspected: int
    requeued: int


def sweep_deleted_media(factory: sessionmaker[DBSession], storage: ObjectStorage, *,
                        after: UUID | None = None, limit: int = 100) -> DeletedMediaAudit:
    """Recover inaccessible late orphans after a lost database guard connection.

    Caller retains the keyset cursor between sweeps; None starts a new pass.
    Failed storage checks advance too and are retried on the next pass. Work
    stops between sessions after 30s (individual transports retain their bounds).
    This is eventual cleanup, not distributed DB/object-store atomicity.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("tombstone audit batch must be bounded")
    with factory() as db:
        ids = tuple(db.scalars(select(Session.id).where(Session.state == SessionState.DELETED,
            True if after is None else Session.id > after).order_by(Session.id).limit(limit)))
    cursor, inspected, requeued = after, 0, 0
    started = monotonic()
    for session_id in ids:
        if inspected and monotonic() - started >= 30:
            break
        cursor, inspected = session_id, inspected + 1
        try:
            # No row transaction remains open while waiting or inspecting storage.
            with media_guard(factory, session_id, exclusive=True):
                leftovers = any(storage.list_keys(prefix) or storage.list_multipart(prefix)
                    for prefix in (f"{kind}/{session_id}/" for kind in ("raw", "media", "demo")))
        except StorageError:
            continue
        if not leftovers:
            continue
        with factory.begin() as db:
            owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
            if owner is None or owner.state != SessionState.DELETED:
                continue
            owner.state = SessionState.DELETING
            fresh_hash = sha256(f"tombstone-audit:{uuid4()}".encode()).hexdigest()
            enqueue(db, JobSpec(session_id, "session", str(session_id), "purge", fresh_hash, PURGE_HASH))
            requeued += 1
    return DeletedMediaAudit(cursor if inspected else None, inspected, requeued)


def request_deletion(factory: sessionmaker[DBSession], token: str, session_id: UUID) -> DeleteMediaResponse:
    with factory.begin() as db:
        if authenticate(db, token) != session_id:
            raise not_found()
        owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
        if owner.state == SessionState.DELETED:
            return DeleteMediaResponse(state="deleted")
        _begin_deletion(db, owner)
    return DeleteMediaResponse(state="deleting")


def _begin_deletion(db: DBSession, owner: Session) -> None:
    """Caller holds the session lock; share revocation and job creation are atomic."""
    session_id = owner.id
    owner.state = SessionState.DELETING
    db.execute(update(Report).where(Report.session_id == session_id).values(share_enabled=False))
    db.execute(update(Entitlement).where(Entitlement.session_id == session_id,
        Entitlement.revoked_at.is_(None)).values(revoked_at=func.clock_timestamp()))
    jobs = db.scalars(select(Job).where(Job.session_id == session_id,
        Job.stage != "purge", Job.state.in_(["queued", "running"])).with_for_update()).all()
    for job in jobs:
        job.state, job.last_error = "dead", "SESSION_DELETED"
        job.lease_token = job.lease_until = job.worker_id = None
    enqueue(db, JobSpec(session_id, "session", str(session_id), "purge", PURGE_HASH, PURGE_HASH))


def sweep_retention(factory: sessionmaker[DBSession], demo_session_id: UUID | None = None,
                    limit: int = 100) -> int:
    """Queue deletion of 30-day unpaid/demo media; paid-user policy remains open.

    Age is measured from session creation. Checkout creation serializes on the
    session; fulfillment serializes on Purchase. Acquire both in that order and
    recheck payment state after waiting. Later provider reconciliation may still
    record a payment against a tombstone, as with explicit owner deletion.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("retention batch must be bounded")
    historical_purchase = exists(select(Purchase.id).where(Purchase.session_id == Session.id,
                                                           Purchase.state.in_(["paid", "refunded"])))
    with factory() as db:
        candidates = tuple(db.scalars(select(Session.id).where(
            Session.state.in_([SessionState.CAPTURING, SessionState.FINALIZED]),
            Session.created_at <= func.clock_timestamp() - timedelta(days=30),
            or_(Session.id == demo_session_id, ~historical_purchase))
            .order_by(Session.created_at, Session.id).limit(limit)))
    count = 0
    for session_id in candidates:
        with factory.begin() as db:
            owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update(skip_locked=True))
            if owner is None or owner.state not in (SessionState.CAPTURING, SessionState.FINALIZED):
                continue
            now = db.scalar(select(func.clock_timestamp()))
            if owner.created_at > now - timedelta(days=30):
                continue
            purchases = db.scalars(select(Purchase).where(Purchase.session_id == session_id)
                .order_by(Purchase.id).with_for_update()).all()
            if session_id != demo_session_id and any(p.state in ("paid", "refunded") for p in purchases):
                continue
            _begin_deletion(db, owner)
            count += 1
    return count


def sweep_purges(factory: sessionmaker[DBSession], limit: int = 100) -> int:
    """Bounded durable retry reconciliation after the queue's three-attempt budget.

    Preserve exhausted attempts; each recovery cycle gets a distinct durable job
    and a one minute cooldown. Only one active cycle per session is permitted.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("purge recovery batch must be bounded")
    count = 0
    with factory() as db:
        ids = tuple(db.scalars(select(Session.id).where(Session.state == SessionState.DELETING,
            ~exists(select(Job.id).where(Job.session_id == Session.id, Job.stage == "purge",
                                         Job.state.in_(["queued", "running"]))))
            .order_by(Session.id).limit(limit)))
    for session_id in ids:
        with factory.begin() as db:
            owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update(skip_locked=True))
            if owner is None or owner.state != SessionState.DELETING:
                continue
            jobs = db.scalars(select(Job).where(Job.session_id == session_id, Job.stage == "purge")
                .order_by(Job.created_at.desc(), Job.id.desc()).with_for_update()).all()
            if any(job.state in ("queued", "running") for job in jobs):
                continue
            job = jobs[0] if jobs else None
            if job is None:
                enqueue(db, JobSpec(session_id, "session", str(session_id), "purge", PURGE_HASH, PURGE_HASH))
                count += 1
            elif job.state == "dead":
                recovery_hash = sha256(f"purge-recovery:{job.id}".encode()).hexdigest()
                recovery_id = enqueue(db, JobSpec(session_id, "session", str(session_id), "purge", recovery_hash, PURGE_HASH))
                recovery = db.get(Job, recovery_id)
                recovery.available_at = db.scalar(select(func.clock_timestamp())) + timedelta(seconds=60)
                count += 1
    return count


def scrub_content(db: DBSession, session_id: UUID) -> None:
    """Keep only the owner tombstone, payment relations and numeric usage accounting."""
    db.execute(update(Report).where(Report.session_id == session_id).values(
        current_revision_id=None, share_enabled=False))
    # Break nullable upload references before removing upload records.
    parts = Base.metadata.tables["parts"]
    db.execute(parts.update().where(parts.c.session_id == session_id).values(active_upload_id=None, raw_key=None))
    keep = {"sessions", "jobs", "reports", "purchases", "entitlements", "model_calls"}
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in keep:
            continue
        authority = table.c.get("session_id")
        if authority is None and table.name == "request_keys":
            authority = table.c.authority_id
        if authority is not None:
            db.execute(delete(table).where(authority == session_id))
    db.execute(update(Job).where(Job.session_id == session_id, Job.stage != "purge").values(subject_id="deleted"))
    owner = db.get(Session, session_id)
    owner.listing_text = None
    owner.state = SessionState.DELETED
    owner.deleted_at = owner.deleted_at or db.scalar(select(func.clock_timestamp()))



