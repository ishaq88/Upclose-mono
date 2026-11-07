"""Completion crash recovery on real PostgreSQL and Floci. Assertions are on persisted state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import threading
import time
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.adapters.storage import S3Storage, StorageError
from app.contracts.enums import PartState, SessionState, UploadState
from app.jobs.queue import JobSpec, job_key
from app.jobs.worker import upload_sweeper
from app.models import Job, Part, PipelineRun, Session, Upload
from app.services.upload_reconciler import Outcome, UploadReconciler
from app.services.uploads import (CHUNK_SIZE_BYTES, MEDIA_CONFIG_PATH, canonical_hash, ingest_config_hashes,
                                  ingest_input_hash)
# Fixtures are shared with the T03b module; importing registers them here.
from tests.test_uploads import Harness, bucket, make, problem, session_factory  # noqa: F401


class SimulatedCrash(Exception):
    """Stands in for the API process dying: nothing after the raise runs."""


class FaultStorage(S3Storage):
    crash_before_complete = False
    crash_after_complete = False
    complete_delay = 0.0
    list_delay = 0.0
    head_hook = None
    head_error: StorageError | None = None

    def list_pieces(self, key, provider_upload_id):
        time.sleep(self.list_delay)
        return super().list_pieces(key, provider_upload_id)

    def complete(self, key, provider_upload_id, pieces) -> None:
        if self.crash_before_complete:
            raise SimulatedCrash()
        time.sleep(self.complete_delay)
        super().complete(key, provider_upload_id, pieces)
        if self.crash_after_complete:
            raise SimulatedCrash()

    def head_size(self, key):
        if self.head_error:
            raise self.head_error
        if self.head_hook:
            self.head_hook()
        return super().head_size(key)


@pytest.fixture
def fault(make):
    def build(*, grace: int = 0, **faults: object) -> Harness:
        class Local(FaultStorage):
            pass
        for name, value in faults.items():
            setattr(Local, name, staticmethod(value) if callable(value) else value)
        return make(storage_cls=Local, upload_completing_grace_seconds=grace)
    return build


def db_now(h: Harness):
    with h.db() as db:
        return db.scalar(select(func.clock_timestamp()))


def reconciler(h: Harness) -> UploadReconciler:
    return UploadReconciler(h.db, h.settings, h.storage)


def ingest_rows(h: Harness) -> tuple[list[PipelineRun], list[Job]]:
    with h.db() as db:
        runs = list(db.scalars(select(PipelineRun).where(PipelineRun.session_id == h.session_id)))
        jobs = list(db.scalars(select(Job).where(Job.session_id == h.session_id)))
    return runs, jobs


def upload_and_part(h: Harness, upload_id: UUID, part_no: int = 1) -> tuple[Upload, Part]:
    return h.row(Upload, upload_id), h.part(part_no)


def force_state(h: Harness, upload_id: UUID, state: UploadState) -> None:
    with h.db.begin() as db:
        db.execute(update(Upload).where(Upload.id == upload_id).values(state=state))


def stranded(h: Harness, data: bytes = b"x" * 1000, *, complete_in_storage: bool = False) -> UUID:
    """An upload the API left in `completing`, as after a crash; optionally with the object stored."""
    upload_id = h.begin(len(data))
    assert h.send(upload_id, 1, data).status_code == 200
    force_state(h, upload_id, UploadState.COMPLETING)
    if complete_in_storage:
        upload = h.row(Upload, upload_id)
        h.storage.complete(upload.object_key, upload.provider_upload_id,
                           h.storage.list_pieces(upload.object_key, upload.provider_upload_id))
    return upload_id


def assert_accepted_once(h: Harness, upload_id: UUID) -> None:
    upload, part = upload_and_part(h, upload_id)
    assert (upload.state, part.state) == (UploadState.ACCEPTED, PartState.UPLOADED)
    assert part.raw_key == upload.object_key and part.active_upload_id is None
    runs, jobs = ingest_rows(h)
    assert len(runs) == 1 and len(jobs) == 1


def assert_nothing_enqueued(h: Harness) -> None:
    runs, jobs = ingest_rows(h)
    assert runs == [] and jobs == []


def test_acceptance_creates_one_run_and_one_queued_ingest_job_atomically(make) -> None:
    h = make()
    upload_id = h.begin(1000)
    assert h.send(upload_id, 1, b"x" * 1000).status_code == 200
    assert h.complete(upload_id).status_code == 202
    assert h.complete(upload_id).status_code == 202  # repeat creates nothing more
    assert_accepted_once(h, upload_id)
    upload, part = upload_and_part(h, upload_id)
    (run,), (job,) = ingest_rows(h)
    config_hashes = {"media": ingest_config_hashes()["media"]}
    assert (run.part_id, run.subject_key, run.state) == (part.id, f"ingest:{part.id}", "pending")
    assert run.input_hash == ingest_input_hash(upload) and run.config_hashes == config_hashes
    assert run.config_hash == canonical_hash(config_hashes)
    assert (job.stage, job.subject_kind, job.subject_id, job.state, job.attempts) == ("ingest", "part_run", str(run.id), "queued", 0)
    assert (job.input_hash, job.config_hash) == (run.input_hash, run.config_hash)
    assert job.idempotency_key == job_key(JobSpec(h.session_id, "part_run", str(run.id), "ingest",
                                                   run.input_hash, run.config_hash))
    assert MEDIA_CONFIG_PATH.exists() and len(config_hashes["media"]) == 64


def test_failure_to_enqueue_rolls_back_acceptance(make, monkeypatch) -> None:
    from app.services import uploads
    from app.services.problems import conflict
    h = make()
    upload_id = h.begin(100)
    assert h.send(upload_id, 1, b"x" * 100).status_code == 200

    def broken(*_: object) -> None:
        raise conflict()
    monkeypatch.setattr(uploads, "enqueue_ingest_for_accepted_part", broken)
    problem(h.complete(upload_id), 409, "STATE_CONFLICT")
    upload, part = upload_and_part(h, upload_id)
    assert (upload.state, upload.accepted_at) == (UploadState.COMPLETING, None)
    assert (part.state, part.raw_key, part.active_upload_id) == (PartState.UPLOADING, None, upload_id)
    assert_nothing_enqueued(h)
    monkeypatch.undo()
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.ACCEPTED  # recovery path converges
    assert_accepted_once(h, upload_id)


def test_crash_after_object_completion_before_db_commit_converges_to_one_job(fault) -> None:
    h = fault(crash_after_complete=True)
    upload_id = h.begin(3000)
    assert h.send(upload_id, 1, b"y" * 3000).status_code == 200
    with pytest.raises(SimulatedCrash):
        h.complete(upload_id)
    upload, part = upload_and_part(h, upload_id)
    assert (upload.state, part.state, part.raw_key) == (UploadState.COMPLETING, PartState.UPLOADING, None)
    assert h.storage.head_size(upload.object_key) == 3000  # the object exists; the DB does not know
    assert_nothing_enqueued(h)
    assert h.complete(upload_id).json()["state"] == "uploading"  # a retry only reports status
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.ACCEPTED
    assert_accepted_once(h, upload_id)
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.UNCHANGED
    assert h.complete(upload_id).json() == {"part_id": str(upload.part_id), "state": "uploaded"}
    assert_accepted_once(h, upload_id)


def test_crash_before_object_completion_is_finished_from_listed_pieces(fault) -> None:
    h = fault(crash_before_complete=True)
    upload_id = h.begin(3000)
    assert h.send(upload_id, 1, b"z" * 3000).status_code == 200
    with pytest.raises(SimulatedCrash):
        h.complete(upload_id)
    key = h.row(Upload, upload_id).object_key
    assert h.storage.head_size(key) is None  # storage never completed
    type(h.storage).crash_before_complete = False  # the "new process" has no fault
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.ACCEPTED
    assert h.storage.head_size(key) == 3000
    assert_accepted_once(h, upload_id)


def test_sweep_respects_grace_and_converges_when_repeated(fault) -> None:
    h = fault(grace=3600, crash_after_complete=True)
    upload_id = h.begin(500)
    assert h.send(upload_id, 1, b"a" * 500).status_code == 200
    with pytest.raises(SimulatedCrash):
        h.complete(upload_id)
    assert reconciler(h).sweep(limit=1000).outcomes.get(Outcome.ACCEPTED, 0) == 0  # in-flight window
    assert h.row(Upload, upload_id).state == UploadState.COMPLETING
    patient = reconciler(h)
    patient.settings = h.settings.model_copy(update={"upload_completing_grace_seconds": 0})
    assert patient.sweep(limit=1000).outcomes[Outcome.ACCEPTED] >= 1
    patient.sweep(limit=1000)
    assert_accepted_once(h, upload_id)


def test_concurrent_reconcilers_produce_exactly_one_job(make) -> None:
    h = make(upload_completing_grace_seconds=0)
    upload_id = stranded(h, complete_in_storage=True)
    barrier = threading.Barrier(4)

    def run() -> Outcome:
        barrier.wait()
        return reconciler(h).reconcile_completing(upload_id)
    with ThreadPoolExecutor(4) as pool:
        outcomes = [f.result() for f in [pool.submit(run) for _ in range(4)]]
    assert sorted(outcomes) == sorted([Outcome.ACCEPTED] + [Outcome.UNCHANGED] * 3)
    assert_accepted_once(h, upload_id)


def test_missing_pieces_stay_incomplete_and_the_upload_is_reopened(make) -> None:
    h = make(upload_completing_grace_seconds=0)
    first, tail = os.urandom(CHUNK_SIZE_BYTES), os.urandom(500)
    upload_id = h.begin(len(first) + len(tail))
    assert h.send(upload_id, 1, first).status_code == 200  # piece 2 never arrived
    force_state(h, upload_id, UploadState.COMPLETING)
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.REOPENED
    upload, part = upload_and_part(h, upload_id)
    assert (upload.state, part.state, part.raw_key) == (UploadState.OPEN, PartState.UPLOADING, None)
    assert h.storage.head_size(upload.object_key) is None
    assert_nothing_enqueued(h)
    assert h.send(upload_id, 2, tail).status_code == 200  # the buyer finishes; piece 1 is not resent
    assert h.complete(upload_id).status_code == 202
    assert_accepted_once(h, upload_id)


def test_wrong_actual_size_is_never_enqueued_and_the_part_is_retakeable(make) -> None:
    h = make(upload_completing_grace_seconds=0)
    upload_id = h.begin(1000)
    sent = h.send(upload_id, 1, b"x" * 999)  # Floci does not enforce the signed length
    assert sent.status_code == 200
    force_state(h, upload_id, UploadState.COMPLETING)
    upload = h.row(Upload, upload_id)
    h.storage.complete(upload.object_key, upload.provider_upload_id,
                       h.storage.list_pieces(upload.object_key, upload.provider_upload_id))
    assert h.storage.head_size(upload.object_key) == 999
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.MISMATCH_ABORTED
    upload, part = upload_and_part(h, upload_id)
    assert upload.state == UploadState.ABORTED
    assert (part.state, part.active_upload_id, part.raw_key, part.failure_reason) == (
        PartState.PENDING, None, None, "UPLOAD_SIZE_MISMATCH")
    assert h.storage.head_size(upload.object_key) is None  # unaccepted object discarded
    assert_nothing_enqueued(h)
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.UNCHANGED
    retake = h.begin(10)  # the buyer can retake; the stale reason is cleared
    assert h.part(1).failure_reason is None and h.row(Upload, retake).state == UploadState.OPEN


def test_vanished_multipart_without_object_ends_aborted_with_reason(make) -> None:
    h = make(upload_completing_grace_seconds=0)
    upload_id = stranded(h)
    upload = h.row(Upload, upload_id)
    h.storage.abort(upload.object_key, upload.provider_upload_id)
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.LOST_ABORTED
    upload, part = upload_and_part(h, upload_id)
    assert (upload.state, part.state, part.failure_reason) == (UploadState.ABORTED, PartState.PENDING, "UPLOAD_LOST")
    assert_nothing_enqueued(h)


def test_storage_outage_leaves_the_upload_completing_for_the_next_sweep(fault) -> None:
    h = fault(head_error=StorageError("InternalError"))
    upload_id = stranded(h, complete_in_storage=True)
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.UNAVAILABLE
    assert h.row(Upload, upload_id).state == UploadState.COMPLETING
    assert_nothing_enqueued(h)
    type(h.storage).head_error = None
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.ACCEPTED
    assert_accepted_once(h, upload_id)


# Timeline: list pieces 0.2 s, open->completing, storage complete 0.2 s, accept. The first three
# aborts land before `completing` (abort wins); the last two land during/after it (complete wins).
@pytest.mark.parametrize("delays,winner", [((0.0, 0.0), UploadState.ABORTED), ((0.0, 0.1), UploadState.ABORTED),
    ((0.15, 0.0), UploadState.ABORTED), ((0.0, 0.3), UploadState.ACCEPTED), ((0.0, 0.9), UploadState.ACCEPTED)])
def test_abort_and_complete_race_ends_in_one_terminal_outcome(fault, delays, winner) -> None:
    complete_wait, abort_wait = delays
    h = fault(list_delay=0.2, complete_delay=0.2)
    upload_id = h.begin(800)
    assert h.send(upload_id, 1, b"r" * 800).status_code == 200

    def complete():
        time.sleep(complete_wait)
        return h.complete(upload_id)

    def abort():
        time.sleep(abort_wait)
        return h.http.delete(f"/api/v1/uploads/{upload_id}", headers=h.headers)
    with ThreadPoolExecutor(2) as pool:
        finishing, aborting = pool.submit(complete), pool.submit(abort)
        finishing, aborting = finishing.result(), aborting.result()
    upload, part = upload_and_part(h, upload_id)
    runs, jobs = ingest_rows(h)
    assert upload.state == winner
    if upload.state == UploadState.ACCEPTED:
        assert (finishing.status_code, aborting.status_code) == (202, 409)
        assert (part.state, part.raw_key) == (PartState.UPLOADED, upload.object_key) and (len(runs), len(jobs)) == (1, 1)
    else:
        assert upload.state == UploadState.ABORTED
        assert (finishing.status_code, aborting.status_code) == (409, 204)
        assert (part.state, part.raw_key, part.active_upload_id) == (PartState.PENDING, None, None)
        assert (len(runs), len(jobs)) == (0, 0)


def test_orphan_open_uploads_expire_and_only_release_their_own_active_part(make) -> None:
    h = make()
    old_id, fresh_id = h.begin(100, part_no=1), h.begin(100, part_no=2)
    stale = h.row(Upload, old_id)
    aged = db_now(h) - timedelta(hours=25)
    with h.db.begin() as db:
        db.execute(update(Upload).where(Upload.id == old_id).values(created_at=aged))
    sweeper = reconciler(h)
    assert sweeper.sweep(limit=1000).outcomes[Outcome.EXPIRED] >= 1
    upload, part = upload_and_part(h, old_id)
    assert (upload.state, part.state, part.active_upload_id) == (UploadState.ABORTED, PartState.PENDING, None)
    with pytest.raises(StorageError):
        h.storage.list_pieces(stale.object_key, stale.provider_upload_id)  # multipart aborted in storage
    fresh, fresh_part = upload_and_part(h, fresh_id, 2)
    assert (fresh.state, fresh_part.state) == (UploadState.OPEN, PartState.UPLOADING)
    # An expired attempt that is no longer the part's active one must not touch the part.
    other = h.begin(100, part_no=3)
    with h.db.begin() as db:
        db.execute(update(Upload).where(Upload.id == other).values(created_at=aged))
        db.execute(update(Part).where(Part.id == h.part(3).id).values(
            active_upload_id=None, state=PartState.PENDING, failure_reason="KEEP"))
    assert sweeper.expire_open(other) == Outcome.EXPIRED
    assert (h.part(3).state, h.part(3).failure_reason) == (PartState.PENDING, "KEEP")
    assert sweeper.expire_open(other) == Outcome.UNCHANGED


def test_deleting_sessions_are_never_accepted_or_expired(fault, session_factory) -> None:
    h = fault(head_hook=None)
    upload_id = stranded(h, complete_in_storage=True)
    old = h.begin(10, part_no=2)
    aged = db_now(h) - timedelta(hours=30)
    with h.db.begin() as db:
        db.execute(update(Upload).where(Upload.id == old).values(created_at=aged))
        db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.DELETING))
    sweeper = reconciler(h)
    assert sweeper.reconcile_completing(upload_id) == Outcome.UNCHANGED
    assert sweeper.expire_open(old) == Outcome.UNCHANGED
    sweeper.sweep(limit=1000)
    assert h.row(Upload, upload_id).state == UploadState.COMPLETING and h.row(Upload, old).state == UploadState.OPEN
    assert h.part(1).state == PartState.UPLOADING and h.part(1).raw_key is None
    assert_nothing_enqueued(h)


def test_session_deleted_between_inspection_and_commit_blocks_acceptance(fault) -> None:
    def delete_session() -> None:
        with h.db.begin() as db:
            db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.DELETING))
    h = fault(head_hook=delete_session)
    upload_id = stranded(h, complete_in_storage=True)
    assert reconciler(h).reconcile_completing(upload_id) == Outcome.UNCHANGED
    assert h.row(Upload, upload_id).state == UploadState.COMPLETING
    assert_nothing_enqueued(h)


def test_sweep_repairs_accepted_parts_that_lack_an_ingest_job(make) -> None:
    h = make(upload_completing_grace_seconds=0)
    upload_id = h.begin(100)
    assert h.send(upload_id, 1, b"x" * 100).status_code == 200
    assert h.complete(upload_id).status_code == 202
    with h.db.begin() as db:  # simulate acceptance recorded before ingest enqueueing existed
        db.execute(Job.__table__.delete().where(Job.session_id == h.session_id))
        db.execute(PipelineRun.__table__.delete().where(PipelineRun.session_id == h.session_id))
    assert_nothing_enqueued(h)
    assert reconciler(h).sweep(limit=1000).outcomes[Outcome.REPAIRED] >= 1
    assert_accepted_once(h, upload_id)
    reconciler(h).sweep(limit=1000)
    assert_accepted_once(h, upload_id)


def test_worker_sweeper_is_bounded_and_survives_an_idle_database(make) -> None:
    h = make()
    sweep = upload_sweeper(h.settings, h.db)
    sweep()
    sweep()  # second call inside the interval is a no-op
