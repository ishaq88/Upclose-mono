"""Real PostgreSQL lease/concurrency checks using synthetic subjects only."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Thread
from time import monotonic
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.contracts.enums import SessionState
from app.jobs import Failure, JobQueue, JobSpec, QueuePolicy, enqueue, job_key
from app.jobs.worker import Publication, StageFailure, StageRegistry, WorkerMustRestart, run_once
from app.models import Job, Session

H = "a" * 64


@pytest.fixture(scope="module")
def factory():
    url = os.environ.get("JOB_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JOB_TEST_DATABASE_URL must name an isolated PostgreSQL test database")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing queue checks outside an explicitly named upclose_test_* PostgreSQL database")
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)
    engine = create_engine(url, pool_size=8, max_overflow=4)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def isolate(factory):
    # Only our module's explicit test database is eligible; avoid unrelated table
    # destruction. Jobs seeded by previous tests are drained from claim discovery.
    with factory.begin() as db:
        db.execute(update(Job).values(state="dead", lease_until=None, lease_token=None, worker_id=None))


def owner(factory, state: SessionState = SessionState.CAPTURING) -> UUID:
    with factory.begin() as db:
        row = Session(token_hash=uuid4().hex * 2, consent_version="synthetic-test", consent_at=datetime.now(timezone.utc), state=state)
        db.add(row)
        db.flush()
        return row.id


def spec(session_id: UUID | None, *, stage="ingest", kind="part_run", subject=None) -> JobSpec:
    return JobSpec(session_id, kind, subject or str(uuid4()), stage, H, H)


def put(factory, job_spec: JobSpec) -> UUID:
    with factory.begin() as db:
        return enqueue(db, job_spec)


def state(factory, job_id: UUID):
    with factory() as db:
        row = db.get(Job, job_id)
        return row.state, row.attempts, row.lease_token, row.last_error


def expire(factory, job_id: UUID) -> None:
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))


def available(factory, job_id: UUID) -> None:
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(available_at=func.clock_timestamp() - timedelta(seconds=1)))


def test_key_identity_and_two_finding_subjects(factory):
    session_id = owner(factory)
    a = spec(session_id, stage="verify", kind="finding")
    b = spec(session_id, stage="verify", kind="finding")
    assert job_key(a) != job_key(b)
    assert put(factory, a) == put(factory, a)
    assert put(factory, a) != put(factory, b)
    assert job_key(a) == job_key(JobSpec(**{name: getattr(a, name) for name in a.__dataclass_fields__}))


def test_competing_workers_only_one_current_owner(factory):
    job_id = put(factory, spec(owner(factory)))
    barrier = Barrier(2)

    def claim(worker):
        barrier.wait(timeout=5)
        return JobQueue(factory).claim(worker)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, ["worker-a", "worker-b"]))
    leases = [result for result in results if result]
    assert len(leases) == 1 and leases[0].job_id == job_id
    assert state(factory, job_id)[1] == 1


def test_workers_can_claim_distinct_sessions_and_skip_locked_parent(factory):
    blocked = owner(factory)
    first = put(factory, spec(blocked))
    second = put(factory, spec(owner(factory)))
    with factory.begin() as lock:
        lock.scalar(select(Session).where(Session.id == blocked).with_for_update())
        with ThreadPoolExecutor(1) as pool:
            lease = pool.submit(JobQueue(factory).claim, "other").result(timeout=5)
        assert lease.job_id == second
    assert JobQueue(factory).claim("first").job_id == first


def test_raced_enqueue_one_effect(factory):
    job_spec = spec(owner(factory))
    barrier = Barrier(2)

    def insert_one(_):
        barrier.wait(timeout=5)
        return put(factory, job_spec)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(insert_one, range(2)))
    assert results[0] == results[1]


def test_expired_lease_reclaimed_stale_token_cannot_publish(factory):
    queue = JobQueue(factory)
    job_id = put(factory, spec(owner(factory)))
    old = queue.claim("old")
    expire(factory, job_id)
    new = queue.claim("new")
    assert old.token != new.token and new.attempt == 2
    called = []
    assert not queue.complete(old, lambda db: called.append(True))
    assert not called and not queue.heartbeat(old)
    assert queue.complete(new)
    assert state(factory, job_id)[0] == "succeeded"


def test_expired_unreclaimed_token_cannot_complete_heartbeat_or_fail(factory):
    queue = JobQueue(factory)
    job_id = put(factory, spec(owner(factory)))
    lease = queue.claim("worker")
    expire(factory, job_id)
    assert not queue.complete(lease)
    assert not queue.heartbeat(lease)
    assert not queue.fail(lease, Failure("TRANSIENT"))


def test_three_total_attempts_including_expired_reclaims(factory):
    queue = JobQueue(factory)
    job_id = put(factory, spec(owner(factory)))
    for attempt in (1, 2, 3):
        lease = queue.claim("worker")
        assert lease.attempt == attempt
        expire(factory, job_id)
    assert queue.claim("fourth") is None
    assert state(factory, job_id)[:2] == ("dead", 3)


def test_retry_backoff_retry_after_and_cap(factory):
    queue = JobQueue(factory, jitter=lambda: .25)
    job_id = put(factory, spec(owner(factory)))
    for attempt, minimum in [(1, 5), (2, 8.25), (3, 0)]:
        lease = queue.claim("worker")
        assert lease.attempt == attempt
        assert queue.fail(lease, Failure("SCHEMA_INVALID", retry_after_seconds=5))
        if attempt < 3:
            with factory() as db:
                delay = db.scalar(select(Job.available_at - func.clock_timestamp()).where(Job.id == job_id)).total_seconds()
                assert minimum - .5 <= delay <= minimum
            assert queue.claim("early") is None
            available(factory, job_id)
    assert state(factory, job_id)[:2] == ("dead", 3)
    assert queue.claim("fourth") is None


@pytest.mark.parametrize("code", ["INVALID_INPUT", "UNSUPPORTED", "UNAUTHORIZED", "BUDGET_EXHAUSTED"])
def test_terminal_failure_does_not_retry(factory, code):
    queue = JobQueue(factory)
    job_id = put(factory, spec(owner(factory)))
    assert queue.fail(queue.claim("worker"), Failure(code))
    assert state(factory, job_id) == ("dead", 1, None, code)


def test_deadline_heartbeat_clamps_and_no_wall_clock_dependency(factory):
    queue = JobQueue(factory)
    job_id = put(factory, spec(owner(factory)))
    lease = queue.claim("worker")
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(attempt_started_at=func.clock_timestamp() - timedelta(seconds=590)))
    assert queue.heartbeat(lease)
    with factory() as db:
        row = db.get(Job, job_id)
        assert row.lease_until == row.attempt_started_at + timedelta(seconds=600)
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(attempt_started_at=func.clock_timestamp() - timedelta(seconds=601)))
    assert not queue.heartbeat(lease) and not queue.complete(lease)
    assert queue.claim("deadline-reclaimer").attempt == 2


def test_retry_after_beyond_deadline_is_terminal(factory):
    queue = JobQueue(factory)
    job_id = put(factory, spec(owner(factory)))
    assert queue.fail(queue.claim("worker"), Failure("PROVIDER_TIMEOUT", retry_after_seconds=601))
    assert state(factory, job_id)[0] == "dead"


def test_publisher_effects_completion_downstream_atomic(factory):
    session_id = owner(factory)
    queue = JobQueue(factory)
    job_id = put(factory, spec(session_id))
    downstream = spec(session_id, kind="concept_run", stage="segment")

    def publish(db):
        db.execute(update(Session).where(Session.id == session_id).values(listing_version=2))

    lease = queue.claim("worker")
    assert queue.complete(lease, publish, (downstream, downstream))
    with factory() as db:
        assert db.get(Session, session_id).listing_version == 2
        assert db.get(Job, job_id).state == "succeeded"
        assert db.scalar(select(func.count()).select_from(Job).where(Job.idempotency_key == job_key(downstream))) == 1
    assert not queue.complete(lease, publish, (downstream,))


def test_publisher_exception_rolls_back_all_effects(factory):
    session_id = owner(factory)
    queue = JobQueue(factory)
    job_id = put(factory, spec(session_id))
    downstream = spec(session_id, kind="concept_run", stage="segment")

    def publish(db):
        db.execute(update(Session).where(Session.id == session_id).values(listing_version=2))
        enqueue(db, downstream)
        raise RuntimeError("synthetic failure with private payload never logged")

    with pytest.raises(RuntimeError):
        queue.complete(queue.claim("worker"), publish)
    with factory() as db:
        assert db.get(Session, session_id).listing_version == 1
        assert db.get(Job, job_id).state == "running"
        assert db.scalar(select(Job.id).where(Job.idempotency_key == job_key(downstream))) is None


def test_post_publication_expiry_rolls_back_effects_and_downstream(factory):
    session_id = owner(factory)
    queue = JobQueue(factory)
    job_id = put(factory, spec(session_id))
    downstream = spec(session_id, kind="concept_run", stage="segment")

    def publish(db):
        db.execute(update(Session).where(Session.id == session_id).values(listing_version=2))
        db.execute(update(Job).where(Job.id == job_id).values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))

    assert not queue.complete(queue.claim("worker"), publish, (downstream,))
    with factory() as db:
        assert db.get(Session, session_id).listing_version == 1
        assert db.get(Job, job_id).state == "running"
        assert db.scalar(select(Job.id).where(Job.idempotency_key == job_key(downstream))) is None


def test_delete_wins_parent_lock_completion_fenced_without_deadlock(factory):
    session_id = owner(factory)
    queue = JobQueue(factory)
    put(factory, spec(session_id))
    lease = queue.claim("worker")
    started = Event()
    called = []

    def complete():
        started.set()
        return queue.complete(lease, lambda db: called.append(True))

    with ThreadPoolExecutor(1) as pool:
        with factory.begin() as deleting:
            deleting.scalar(select(Session).where(Session.id == session_id).with_for_update())
            future = pool.submit(complete)
            assert started.wait(5)
            deleting.execute(update(Session).where(Session.id == session_id).values(state=SessionState.DELETING))
            # Deletion can lock this job while holding its parent; completion
            # never locks the job first, so neither transaction deadlocks.
            deleting.scalar(select(Job).where(Job.id == lease.job_id).with_for_update())
        assert future.result(timeout=5) is False
    assert not called and not queue.heartbeat(lease)


def test_completion_wins_parent_then_deletion_serializes(factory):
    session_id = owner(factory)
    queue = JobQueue(factory)
    job_id = put(factory, spec(session_id))
    entered, release = Event(), Event()

    def publish(db):
        entered.set()
        assert release.wait(5)
        db.execute(update(Session).where(Session.id == session_id).values(listing_version=2))

    def delete():
        with factory.begin() as db:
            row = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
            row.state = SessionState.DELETING

    lease = queue.claim("worker")
    with ThreadPoolExecutor(2) as pool:
        complete_future = pool.submit(queue.complete, lease, publish)
        assert entered.wait(5)
        delete_future = pool.submit(delete)
        release.set()
        assert complete_future.result(timeout=5)
        delete_future.result(timeout=5)
    assert state(factory, job_id)[0] == "succeeded"


def test_deleting_normal_jobs_blocked_but_purge_runs_and_global_billing(factory):
    session_id = owner(factory)
    ordinary = put(factory, spec(session_id))
    with factory.begin() as db:
        db.execute(update(Session).where(Session.id == session_id).values(state=SessionState.DELETING))
    purge = put(factory, spec(session_id, stage="purge", kind="session", subject=str(session_id)))
    queue = JobQueue(factory)
    lease = queue.claim("worker")
    assert lease.job_id == purge

    def finish_purge(db):
        db.execute(update(Session).where(Session.id == session_id).values(state=SessionState.DELETED))

    assert queue.complete(lease, finish_purge)
    billing = put(factory, spec(None, stage="billing_event", kind="stripe_event", subject=f"evt_synthetic_{uuid4().hex}"))
    lease = queue.claim("billing")
    assert lease.job_id == billing and lease.session_id is None
    assert queue.complete(lease)
    assert state(factory, ordinary)[0] == "queued"
    assert queue.claim("none") is None


def test_cross_authority_downstream_rolls_back(factory):
    queue = JobQueue(factory)
    parent = owner(factory)
    put(factory, spec(parent))
    with pytest.raises(ValueError):
        queue.complete(queue.claim("worker"), downstream=(spec(owner(factory)),))


def test_fixture_handler_runs_outside_claim_transaction(factory):
    session_id = owner(factory)
    job_id = put(factory, spec(session_id))
    registry = StageRegistry()

    def synthetic_handler(lease):
        # A second connection can take both locks while the handler performs
        # external work. It also sees the committed running lease.
        with factory.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            db.scalar(select(Session).where(Session.id == session_id).with_for_update(nowait=True))
            row = db.scalar(select(Job).where(Job.id == job_id).with_for_update(nowait=True))
            assert row.state == "running" and row.lease_token == lease.token
        return Publication()

    registry.register("ingest", synthetic_handler)
    assert run_once(JobQueue(factory), registry, "synthetic")
    assert state(factory, job_id)[0] == "succeeded"


def test_worker_unregistered_stages_not_claimed_and_errors_sanitized(factory):
    job_id = put(factory, spec(owner(factory)))
    queue = JobQueue(factory, jitter=lambda: 0)
    registry = StageRegistry()
    assert not run_once(queue, registry, "worker")
    assert state(factory, job_id)[:2] == ("queued", 0)

    def fail(lease):
        raise RuntimeError("provider private payload")

    registry.register("ingest", fail)
    assert run_once(queue, registry, "worker")
    assert state(factory, job_id)[3] == "INTERNAL"


def test_input_policy_failure_and_registry_validation(factory):
    with pytest.raises(ValueError):
        JobSpec(None, "part_run", "synthetic", "ingest", H, H)
    with pytest.raises(ValueError):
        JobSpec(owner(factory), "part_run", "synthetic", "verify", H, H)
    with pytest.raises(ValueError):
        QueuePolicy(lease_seconds=30, heartbeat_seconds=30)
    with pytest.raises(ValueError):
        Failure("PRIVATE PROVIDER ERROR")
    registry = StageRegistry()
    registry.register("ingest", lambda lease: Publication())
    with pytest.raises(ValueError):
        registry.register("ingest", lambda lease: Publication())


def test_discovery_pages_past_100_locked_and_filters_deleted_jobs(factory):
    deleted = owner(factory)
    locked = owner(factory)
    with factory.begin() as db:
        for _ in range(101):
            enqueue(db, spec(deleted))
            enqueue(db, spec(locked))
        db.execute(update(Session).where(Session.id == deleted).values(state=SessionState.DELETED))
    eligible = put(factory, spec(owner(factory)))
    with factory.begin() as db:
        db.scalar(select(Session).where(Session.id == locked).with_for_update())
        with ThreadPoolExecutor(1) as pool:
            lease = pool.submit(JobQueue(factory).claim, "not-starved").result(timeout=10)
        assert lease.job_id == eligible


@pytest.mark.parametrize("operation", ["heartbeat", "fail", "complete"])
def test_global_billing_lock_wait_expiry_is_rechecked(factory, operation):
    job_id = put(factory, spec(None, stage="billing_event", kind="stripe_event"))
    queue = JobQueue(factory)
    lease = queue.claim("billing")
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(lease_until=func.clock_timestamp() + timedelta(seconds=1)))
    called = []

    def operation_call():
        if operation == "heartbeat":
            return queue.heartbeat(lease)
        if operation == "fail":
            return queue.fail(lease, Failure("TRANSIENT"))
        return queue.complete(lease, lambda db: called.append(True))

    with ThreadPoolExecutor(1) as pool:
        with factory.begin() as locking:
            locking.scalar(select(Job).where(Job.id == job_id).with_for_update())
            future = pool.submit(operation_call)
            until = monotonic() + 3
            blocked = False
            while monotonic() < until:
                blocked = locking.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock')"))
                if blocked:
                    break
                Event().wait(.01)
            assert blocked, "queue operation must actually wait on the held job lock"
            locking.execute(text("SELECT pg_sleep(1.1)"))
        assert future.result(timeout=5) is False
    assert not called and state(factory, job_id)[0] == "running"


def test_actual_lease_defaults_use_database_clock(factory):
    queue = JobQueue(factory)
    put(factory, spec(owner(factory)))
    lease = queue.claim("worker")
    with factory() as db:
        row = db.get(Job, lease.job_id)
        assert row.lease_until - row.attempt_started_at == timedelta(seconds=120)
        assert lease.deadline - row.attempt_started_at == timedelta(seconds=600)
        assert queue.policy.heartbeat_seconds == 30


def test_worker_requires_restart_while_timed_out_handler_still_active(factory):
    put(factory, spec(owner(factory)))
    # Short synthetic runner policy: production default remains 120/30/600.
    queue = JobQueue(factory, QueuePolicy(lease_seconds=2, heartbeat_seconds=1, deadline_seconds=2))
    registry = StageRegistry()
    release = Event()

    def stuck_handler(lease):
        assert release.wait(5)
        return Publication()

    registry.register("ingest", stuck_handler)
    try:
        with pytest.raises(WorkerMustRestart):
            run_once(queue, registry, "worker")
    finally:
        release.set()


def test_worker_validated_terminal_failure_and_invalid_publication(factory):
    queue = JobQueue(factory)
    job_id = put(factory, spec(owner(factory)))
    registry = StageRegistry()

    def unsupported(lease):
        raise StageFailure(Failure("UNSUPPORTED"))

    registry.register("ingest", unsupported)
    assert run_once(queue, registry, "worker")
    assert state(factory, job_id) == ("dead", 1, None, "UNSUPPORTED")

    other = put(factory, spec(owner(factory)))
    invalid = StageRegistry()
    invalid.register("ingest", lambda lease: {"invalid": "synthetic response"})
    assert run_once(queue, invalid, "worker")
    assert state(factory, other) == ("dead", 1, None, "INVALID_INPUT")


def test_worker_main_process_starts_fixture_only_and_stops_cleanly(factory):
    # The subprocess must use the same disposable database as this module,
    # including when this file runs alone without model_engine's environment.
    env = {**os.environ, "MODEL_MODE": "fixture",
           "DATABASE_URL": factory.kw["bind"].url.render_as_string(hide_password=False)}
    env.pop("MODEL_API_KEY", None)
    process = subprocess.Popen([sys.executable, "-m", "app.jobs.worker"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = Event()
    messages = []

    def read_startup():
        for line in process.stderr:
            messages.append(line)
            if "Queue runner started (mode=fixture)" in line:
                ready.set()

    reader = Thread(target=read_startup, daemon=True)
    reader.start()
    try:
        # Startup must reach the running loop, rather than failing construction
        # (notably SecretStr -> SQLAlchemy URL conversion).
        assert ready.wait(10), "fixture worker did not reach startup loop: " + "".join(messages)
        assert process.poll() is None, "fixture worker exited during startup"
        process.terminate()
        process.wait(timeout=5)
        reader.join(timeout=5)
        assert process.returncode == 0
        assert any("Queue runner started (mode=fixture)" in line for line in messages)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_claim_stage_filter_leaves_other_stages_queued(factory):
    heavy = put(factory, spec(owner(factory), stage="segment", kind="concept_run"))
    light = put(factory, spec(owner(factory), stage="ingest"))
    queue = JobQueue(factory)
    lease = queue.claim("light-worker", ["ingest", "audio_ingest"])
    assert lease is not None and lease.job_id == light and lease.stage == "ingest"
    assert queue.claim("light-worker", ["ingest", "audio_ingest"]) is None
    assert state(factory, heavy)[:2] == ("queued", 0)
    heavy_lease = queue.claim("heavy-worker")
    assert heavy_lease is not None and heavy_lease.job_id == heavy and heavy_lease.attempt == 1


def test_claimable_stages_allow_exclude_and_validation():
    from app.jobs.worker import claimable_stages
    registry = StageRegistry()
    for stage in ("ingest", "vehicle_mask", "segment", "render"):
        registry.register(stage, lambda lease: Publication())
    assert claimable_stages(registry) == ("ingest", "vehicle_mask", "segment", "render")
    assert claimable_stages(registry, exclude="vehicle_mask, segment") == ("ingest", "render")
    assert claimable_stages(registry, allow="render,ingest,audio_ingest") == ("ingest", "render")
    for bad in ({"allow": "ingest", "exclude": "render"}, {"allow": "nonsense"}, {"exclude": "nonsense"}):
        with pytest.raises(ValueError):
            claimable_stages(registry, **bad)
    assert run_once(JobQueue(None), registry, "w", stages=[]) is False  # an empty lane claims nothing, no DB touched
