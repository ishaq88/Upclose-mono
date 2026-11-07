"""Deletion against disposable PostgreSQL and private synthetic storage."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.adapters.session_storage import media_guard
from app.adapters.storage import StorageError
from app.contracts.enums import SessionState
from app.jobs.queue import JobQueue
from app.jobs.worker import StageFailure
from app.models import Artifact, Job, Part, Report, Session, Upload
from app.pipeline.purge import PurgeHandler
from app.services.deletion import sweep_purges
from tests.test_uploads import bucket, make, session_factory  # noqa: F401


def delete(h):
    return h.http.post(f"/api/v1/sessions/{h.session_id}/delete-media", json={}, headers=h.headers)


def test_delete_auth_immediate_revocation_and_idempotence(make):
    h = make()
    path = f"/api/v1/sessions/{h.session_id}/delete-media"
    assert h.http.post(path, json={}).status_code == 401
    assert h.http.post(path, json={}, headers=h.other_owner()).status_code == 404
    assert delete(h).json() == {"state": "deleting"}
    assert delete(h).status_code == 202
    assert h.http.get(f"/api/v1/sessions/{h.session_id}", headers=h.headers).status_code == 404
    with h.db() as db:
        assert db.get(Session, h.session_id).state == SessionState.DELETING
        assert len(db.scalars(select(Job).where(Job.session_id == h.session_id, Job.stage == "purge")).all()) == 1


def test_purge_prefixes_recorded_keys_multipart_and_content(make, tmp_path):
    h = make()
    h.begin(10)
    h.http.put(f"/api/v1/sessions/{h.session_id}/listing", headers=h.headers | {"Idempotency-Key": uuid4().hex},
               json={"listing_text": "synthetic listing private"})
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic")
    keys = [f"{kind}/{h.session_id}/orphan" for kind in ("raw", "media", "demo")]
    keys.append(f"recorded/{uuid4()}")
    for key in keys:
        h.storage.put_file(key, source, "application/octet-stream")
    with h.db.begin() as db:
        db.add(Artifact(session_id=h.session_id, kind="proxy", key=keys[-1], sha256="a" * 64))
    h.storage.create_multipart(f"media/{h.session_id}/unfinished", "video/mp4")
    assert delete(h).status_code == 202
    queue = JobQueue(h.db)
    lease = queue.claim("purge", ["purge"])
    assert lease is not None
    assert queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)
    for key in keys:
        assert h.storage.head_size(key) is None
    assert not h.storage.list_multipart(f"raw/{h.session_id}/")
    assert not h.storage.list_multipart(f"media/{h.session_id}/")
    with h.db() as db:
        owner = db.get(Session, h.session_id)
        assert owner.state == SessionState.DELETED and owner.listing_text is None
        for model in (Upload, Part, Artifact):
            assert not db.scalars(select(model).where(model.session_id == h.session_id)).all()
    assert delete(h).json() == {"state": "deleted"}


def test_partial_failure_retries_and_exhausted_recovery(make, tmp_path):
    h = make()
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic")
    key = f"media/{h.session_id}/orphan"
    h.storage.put_file(key, source, "application/octet-stream")
    partial_key = f"media/{h.session_id}/partial"
    h.storage.put_file(partial_key, source, "application/octet-stream")
    delete(h)
    queue = JobQueue(h.db, jitter=lambda: 0)
    original = h.storage.delete_object
    def failed(key):
        if key == partial_key:
            return original(key)
        original(partial_key)
        raise StorageError("Unavailable")
    h.storage.delete_object = failed
    for attempt in range(3):
        lease = queue.claim("purge", ["purge"])
        with pytest.raises(StageFailure) as failure:
            PurgeHandler(h.db, h.storage)(lease)
        assert queue.fail(lease, failure.value.failure)
        with h.db.begin() as db:
            db.execute(update(Job).where(Job.id == lease.job_id).values(available_at=func.clock_timestamp()))
    assert h.storage.head_size(partial_key) is None
    with h.db() as db:
        assert db.get(Job, lease.job_id).state == "dead"
        assert db.get(Session, h.session_id).state == SessionState.DELETING
    assert sweep_purges(h.db) >= 1
    with h.db.begin() as db:
        assert db.get(Job, lease.job_id).attempts == 3
        assert db.get(Job, lease.job_id).state == "dead"
        db.execute(update(Job).where(Job.session_id != h.session_id, Job.stage == "purge", Job.state == "queued")
                   .values(available_at=func.clock_timestamp() + timedelta(seconds=60)))
        db.execute(update(Job).where(Job.session_id == h.session_id, Job.stage == "purge", Job.state == "queued")
                   .values(available_at=func.clock_timestamp()))
    h.storage.delete_object = original
    lease = queue.claim("recovery", ["purge"])
    assert queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)
    assert h.storage.head_size(key) is None


def test_ready_snapshot_and_findings_are_scrubbed(ready):
    from app.models import Claim, ClaimEvidence, Finding, Observation, PrivacyDerivative, ReportRevision, RevisionFinding
    h = ready.h
    report = h.row(Report, ready.revision().report_id)
    assert delete(h).status_code == 202
    queue = JobQueue(h.db)
    lease = queue.claim("purge", ["purge"])
    assert queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)
    with h.db() as db:
        for model in (Claim, ClaimEvidence, Finding, Observation, PrivacyDerivative, ReportRevision, RevisionFinding):
            assert not db.scalars(select(model).where(model.session_id == h.session_id)).all()
        tombstone = db.get(Report, report.id)
        assert tombstone.share_slug == report.share_slug and tombstone.current_revision_id is None


# Reuse the existing synthetic full pipeline fixture; this exercises frozen FK graphs.
from tests.test_report_routes import ready  # noqa: E402,F401
from tests.test_ingest import media_dir, scratch_root  # noqa: E402,F401
from tests.test_orchestration import isolate  # noqa: E402,F401

def test_deletion_cancels_running_lease_and_preserves_accounting(ready):
    from app.models import Entitlement, Purchase
    from app.jobs.queue import JobSpec, enqueue
    h = ready.h
    report_id = ready.revision().report_id
    with h.db.begin() as db:
        purchase = Purchase(session_id=h.session_id, report_id=report_id, state="paid",
                            checkout_id=uuid4().hex, payment_intent_id=uuid4().hex)
        db.add(purchase)
        db.flush()
        entitlement = Entitlement(session_id=h.session_id, report_id=report_id, source_purchase_id=purchase.id)
        db.add(entitlement)
        db.execute(update(Report).where(Report.id == report_id).values(share_enabled=True))
        enqueue(db, JobSpec(h.session_id, "report_revision", str(ready.revision().id), "render", "d"*64, "e"*64))
    queue = JobQueue(h.db)
    stale = queue.claim("stale", ["render"])
    assert stale is not None
    assert delete(h).status_code == 202
    assert not queue.complete(stale)
    with h.db() as db:
        assert not db.get(Report, report_id).share_enabled
        assert db.scalar(select(Entitlement).where(Entitlement.session_id == h.session_id)).revoked_at is not None
        assert db.get(Job, stale.job_id).state == "dead"
    lease = queue.claim("purge", ["purge"])
    assert queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)
    with h.db() as db:
        assert db.scalar(select(Purchase).where(Purchase.session_id == h.session_id)).state == "paid"

def test_purge_waits_for_inflight_writer_and_rejects_stale_put(make, tmp_path):
    from app.adapters.session_storage import FencedStorage
    h = make()
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic")
    entered, release, purged = Event(), Event(), Event()
    inner_put = h.storage.put_file
    def slow_put(key, path, content_type):
        entered.set()
        assert release.wait(4)
        inner_put(key, path, content_type)
    h.storage.put_file = slow_put
    fenced = FencedStorage(h.db, h.storage)
    key = f"media/{h.session_id}/stale-orphan"
    queue = JobQueue(h.db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(fenced.put_file, key, source, "application/octet-stream")
        assert entered.wait(2)
        assert delete(h).status_code == 202
        lease = queue.claim("purge", ["purge"])
        def purge():
            result = PurgeHandler(h.db, h.storage)(lease)
            purged.set()
            return result
        pending = pool.submit(purge)
        assert not purged.wait(0.15)
        release.set()
        with pytest.raises(StorageError) as failure:
            writer.result(timeout=4)
        assert failure.value.code == "SessionDeleted"
        assert queue.complete(lease, pending.result(timeout=4).publish)
    assert h.storage.head_size(key) is None
    with pytest.raises(StorageError) as failure:
        fenced.put_file(key, source, "application/octet-stream")
    assert failure.value.code == "SessionDeleted"
    assert h.storage.head_size(key) is None


def test_retention_old_unpaid_fresh_paid_refunded_demo_and_deleting(make):
    from app.models import Purchase
    from app.services.deletion import sweep_retention
    rigs = {name: make() for name in ("unpaid", "fresh", "paid", "refunded", "demo", "deleting")}
    with rigs["unpaid"].db.begin() as db:
        for name, h in rigs.items():
            if name == "unpaid":
                db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.FINALIZED))
            if name != "fresh":
                db.execute(update(Session).where(Session.id == h.session_id).values(
                    created_at=func.clock_timestamp() - timedelta(days=31)))
            if name in ("paid", "refunded", "demo"):
                report = Report(session_id=h.session_id, share_slug=uuid4().hex)
                db.add(report)
                db.flush()
                db.add(Purchase(session_id=h.session_id, report_id=report.id,
                                state="refunded" if name == "refunded" else "paid",
                                checkout_id=uuid4().hex, payment_intent_id=uuid4().hex))
    assert delete(rigs["deleting"]).status_code == 202
    sweep_retention(rigs["unpaid"].db, demo_session_id=rigs["demo"].session_id)
    with rigs["unpaid"].db() as db:
        for name, h in rigs.items():
            expected = SessionState.DELETING if name in ("unpaid", "demo", "deleting") else SessionState.CAPTURING
            assert db.get(Session, h.session_id).state == expected
        assert len(db.scalars(select(Job).where(Job.session_id == rigs["deleting"].session_id,
                                                Job.stage == "purge")).all()) == 1
    assert sweep_retention(rigs["unpaid"].db, demo_session_id=rigs["demo"].session_id) == 0


def test_retention_rechecks_purchase_after_fulfillment_lock_wait(make):
    from app.models import Purchase
    from app.services.deletion import sweep_retention
    h = make()
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(
            created_at=func.clock_timestamp() - timedelta(days=31)))
        report = Report(session_id=h.session_id, share_slug=uuid4().hex)
        db.add(report)
        db.flush()
        purchase = Purchase(session_id=h.session_id, report_id=report.id)
        db.add(purchase)
        db.flush()
        purchase_id = purchase.id
    with ThreadPoolExecutor(max_workers=1) as pool:
        with h.db.begin() as db:
            purchase = db.scalar(select(Purchase).where(Purchase.id == purchase_id).with_for_update())
            started = Event()
            def sweep():
                started.set()
                return sweep_retention(h.db)
            future = pool.submit(sweep)
            assert started.wait(2)
            # Wait until the sweep owns the session lock and is waiting on payment.
            from sqlalchemy import text
            import time
            for _ in range(100):
                with h.db() as probe:
                    waiting = probe.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%purchases%')"))
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting
            purchase.state, purchase.checkout_id, purchase.payment_intent_id = "paid", uuid4().hex, uuid4().hex
        future.result(timeout=4)
    assert h.row(Session, h.session_id).state == SessionState.CAPTURING


@pytest.mark.parametrize("state", [SessionState.DELETING, SessionState.DELETED])
def test_stale_multipart_create_and_complete_refused(make, state):
    from app.adapters.session_storage import FencedStorage
    h = make()
    key = f"raw/{h.session_id}/stale"
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(state=state))
    def unexpected(*args):
        pytest.fail("Storage mutation reached provider after deletion")
    h.storage.create_multipart = unexpected
    h.storage.complete = unexpected
    h.storage.delete_object = unexpected
    h.storage.abort = unexpected
    fenced = FencedStorage(h.db, h.storage)
    for operation in (lambda: fenced.create_multipart(key, "video/mp4"),
                      lambda: fenced.complete(key, "synthetic-upload-id", ())):
        with pytest.raises(StorageError) as failure:
            operation()
        assert failure.value.code == "SessionDeleted"

def test_lost_guard_late_write_fails_and_tombstone_audit_recovers(make, tmp_path):
    from app.adapters.session_storage import FencedStorage, media_lock_id
    from app.services.deletion import sweep_deleted_media
    from sqlalchemy import text
    h = make()
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic")
    entered, release = Event(), Event()
    original_put, original_delete = h.storage.put_file, h.storage.delete_object
    def slow_put(key, path, content_type):
        entered.set()
        assert release.wait(5)
        original_put(key, path, content_type)
    h.storage.put_file = slow_put
    key = f"media/{h.session_id}/lost-guard-orphan"
    queue = JobQueue(h.db)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(FencedStorage(h.db, h.storage).put_file, key, source, "application/octet-stream")
        assert entered.wait(2)
        assert delete(h).status_code == 202
        lock_id = media_lock_id(h.session_id) & ((1 << 64) - 1)
        with h.db.begin() as db:
            pid = db.scalar(text("SELECT pid FROM pg_locks WHERE locktype='advisory' AND mode='ShareLock' AND granted AND classid=:hi AND objid=:lo"),
                            {"hi": lock_id >> 32, "lo": lock_id & 0xffffffff})
            assert pid is not None
            assert db.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        lease = queue.claim("purge-before-late-write", ["purge"])
        assert queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)
        original_deleted_at = h.row(Session, h.session_id).deleted_at
        def unavailable(key):
            raise StorageError("Unavailable")
        h.storage.delete_object = unavailable  # best-effort late cleanup cannot finish
        release.set()
        with pytest.raises(StorageError) as failure:
            writer.result(timeout=4)
        assert failure.value.code == "MediaGuardLost"
    assert h.storage.head_size(key) is not None
    assert h.http.get(f"/api/v1/sessions/{h.session_id}", headers=h.headers).status_code == 404
    h.storage.delete_object = original_delete
    audit = sweep_deleted_media(h.db, h.storage)
    assert audit.requeued >= 1
    lease = queue.claim("late-orphan-recovery", ["purge"])
    assert lease.session_id == h.session_id
    assert queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)
    assert h.storage.head_size(key) is None
    assert h.row(Session, h.session_id).deleted_at == original_deleted_at


def test_tombstone_audit_cursor_advances_past_storage_errors(make):
    from app.services.deletion import sweep_deleted_media
    h = make()
    ids = []
    with h.db.begin() as db:
        for _ in range(105):
            owner = Session(token_hash=uuid4().hex * 2, consent_version="synthetic-test",
                            consent_at=func.now(), state=SessionState.DELETED)
            db.add(owner)
            db.flush()
            ids.append(owner.id)
    ids.sort()
    original = h.storage.list_keys
    checked = []
    def unavailable(prefix):
        checked.append(prefix)
        raise StorageError("Unavailable")
    h.storage.list_keys = unavailable
    cursor = None
    seen = set()
    for _ in range(100):
        audit = sweep_deleted_media(h.db, h.storage, after=cursor, limit=25)
        if audit.cursor is None:
            break
        # Every failed prefix was still inspected; the cursor must walk past
        # the first 100 tombstones instead of repeatedly selecting that page.
        seen.update(UUID(prefix.split('/')[1]) for prefix in checked)
        cursor = audit.cursor
    assert set(ids) <= seen
    assert audit.cursor is None
    assert checked
    h.storage.list_keys = original

def test_live_transient_storage_errors_never_trigger_destructive_cleanup(make, tmp_path):
    from app.adapters.session_storage import FencedStorage
    h = make()
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"synthetic")
    key = f"raw/{h.session_id}/accepted"
    h.storage.put_file(key, path, "application/octet-stream")
    def failed(*args):
        raise StorageError("Unavailable")
    def unexpected(*args):
        pytest.fail("Transient live failure must not destroy accepted storage")
    h.storage.put_file = h.storage.complete = h.storage.create_multipart = failed
    h.storage.delete_object = h.storage.abort = unexpected
    fenced = FencedStorage(h.db, h.storage)
    for operation in (lambda: fenced.put_file(key, path, "application/octet-stream"),
                      lambda: fenced.complete(key, "synthetic", ()),
                      lambda: fenced.create_multipart(key, "video/mp4")):
        with pytest.raises(StorageError) as failure:
            operation()
        assert failure.value.code == "Unavailable"
    assert h.storage.head_size(key) == len(b"synthetic")
