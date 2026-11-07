"""Source lifecycle against disposable PostgreSQL and the storage emulator."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.contracts.enums import PartState, SessionState, UploadState
from app.adapters.storage import StorageError
from app.jobs.queue import Lease
from app.models import Job, Part, Session, VideoUpload
from app.pipeline.purge import PurgeHandler
from app.services.video_uploads import VideoUploadReconciler
from tests.test_uploads import Harness, bucket, session_factory


class VideoHarness(Harness):
    def new_owner(self, http):
        created = http.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
            "consent_version": "upclose-prototype-v1", "capture_mode": "single_video"})
        assert created.status_code == 201, created.text
        body = created.json()
        return UUID(body["session_id"]), {"Authorization": f"Bearer {body['session_token']}"}

    def source(self, size=100, segments=None, headers=None):
        return self.http.post(f"/api/v1/sessions/{self.session_id}/video", headers=headers or self.headers,
            json={"content_type": "video/mp4", "content_length": size,
                  "segments": segments if segments is not None else []})

    def source_send(self, upload_id, payload=b"x" * 100):
        response = self.http.post(f"/api/v1/video-uploads/{upload_id}/urls", headers=self.headers,
                                  json={"part_numbers": [1]})
        assert response.status_code == 200, response.text
        assert httpx.put(response.json()["items"][0]["url"], content=payload).status_code == 200

    def source_complete(self, upload_id, headers=None):
        return self.http.post(f"/api/v1/video-uploads/{upload_id}/complete", headers=headers or self.headers, json={})

    def finalize(self):
        return self.http.post(f"/api/v1/sessions/{self.session_id}/finalize", headers=self.headers, json={})

    def reconciler(self):
        return VideoUploadReconciler(self.db, self.settings, self.storage)


@pytest.fixture
def make_video(session_factory, bucket):
    return lambda **settings: VideoHarness(session_factory, bucket, **settings)


RANGE = [{"part_no": 1, "start_ms": 100, "end_ms": 1000}]


def test_mode_default_and_explicit_single_video(session_factory, bucket, make_video):
    guided = Harness(session_factory, bucket)
    response = guided.http.get(f"/api/v1/sessions/{guided.session_id}", headers=guided.headers)
    assert response.json()["capture_mode"] == "guided"
    blocked = guided.http.post(f"/api/v1/sessions/{guided.session_id}/video", headers=guided.headers,
        json={"content_type": "video/mp4", "content_length": 100})
    assert blocked.status_code == 409
    h = make_video()
    assert h.http.get(f"/api/v1/sessions/{h.session_id}", headers=h.headers).json()["capture_mode"] == "single_video"
    assert h.http.get(f"/api/v1/sessions/{h.session_id}/video", headers=h.headers).json() == {"source": None}


def test_source_resume_acceptance_job_and_safe_summary(make_video):
    h = make_video()
    created = h.source(segments=RANGE)
    assert created.status_code == 201, created.text
    source_id = UUID(created.json()["upload_id"])
    assert h.part().source_video_id == source_id
    assert h.part().state == PartState.PENDING and h.part().raw_key is None
    h.source_send(source_id)
    resumed = h.http.get(f"/api/v1/video-uploads/{source_id}", headers=h.headers)
    assert resumed.json()["pieces"][0]["size_bytes"] == 100
    response = h.source_complete(source_id)
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "accepted" and response.json()["processing_state"] == "queued"
    assert h.source_complete(source_id).json() == response.json()
    with h.db() as db:
        jobs = list(db.scalars(select(Job).where(Job.session_id == h.session_id)))
        assert len(jobs) == 1 and jobs[0].stage == "source_ingest"
        assert jobs[0].subject_kind == "session" and jobs[0].subject_id == str(source_id)
    source = h.http.get(f"/api/v1/sessions/{h.session_id}/video", headers=h.headers).json()["source"]
    assert source["segments"] == RANGE
    assert not {"object_key", "provider_upload_id", "raw_sha256", "url"} & source.keys()
    assert h.finalize().status_code == 409
    assert h.source().status_code == 409


def test_reservations_abort_and_followup(make_video):
    h = make_video()
    source_id = h.source(segments=RANGE).json()["upload_id"]
    assert h.start(100).status_code == 409
    assert h.http.post(f"/api/v1/parts/{h.part().id}/skip", headers=h.headers, json={}).status_code == 409
    assert h.finalize().status_code == 409
    assert h.start(100, part_no=2).status_code == 201
    assert h.http.delete(f"/api/v1/video-uploads/{source_id}", headers=h.headers).status_code == 204
    assert h.http.delete(f"/api/v1/video-uploads/{source_id}", headers=h.headers).status_code == 204
    assert h.part().source_video_id is None
    assert h.start(100).status_code == 201
    assert h.source(segments=RANGE).status_code == 409
    assert h.source().status_code == 201


def test_idempotent_create_and_source_attempt_conflicts(make_video):
    h = make_video()
    headers = h.headers | {"Idempotency-Key": str(uuid4())}
    first = h.source(segments=RANGE, headers=headers)
    assert first.status_code == 201
    assert h.source(segments=RANGE, headers=headers).json() == first.json()
    assert h.source(size=101, headers=headers).status_code == 409
    assert h.source().status_code == 409


@pytest.mark.parametrize("operation", ["get", "urls", "complete", "abort", "current"])
def test_cross_owner_cannot_access_source(make_video, operation):
    h = make_video()
    source_id = h.source().json()["upload_id"]
    headers = h.other_owner()
    path = f"/api/v1/video-uploads/{source_id}"
    if operation == "get":
        response = h.http.get(path, headers=headers)
    elif operation == "urls":
        response = h.http.post(path + "/urls", headers=headers, json={"part_numbers": [1]})
    elif operation == "complete":
        response = h.source_complete(source_id, headers)
    elif operation == "abort":
        response = h.http.delete(path, headers=headers)
    else:
        response = h.http.get(f"/api/v1/sessions/{h.session_id}/video", headers=headers)
    assert response.status_code == 404


@pytest.mark.parametrize("segments", [
    [{"part_no": 1, "start_ms": 0, "end_ms": 60001}],
    [{"part_no": 1, "start_ms": 0, "end_ms": 100}, {"part_no": 1, "start_ms": 100, "end_ms": 200}],
    [{"part_no": 1, "start_ms": 0, "end_ms": 100}, {"part_no": 2, "start_ms": 50, "end_ms": 200}],
    [{"part_no": 1, "start_ms": 100, "end_ms": 100}],
    [{"part_no": 1, "start_ms": 170000, "end_ms": 180001}],
])
def test_bad_ranges_reject_without_storage_or_reservation(make_video, segments):
    h = make_video()
    assert h.source(segments=segments).status_code == 422
    assert h.part().source_video_id is None


def test_incomplete_then_concurrent_completion(make_video):
    h = make_video()
    source_id = h.source().json()["upload_id"]
    assert h.source_complete(source_id).status_code == 409
    h.source_send(source_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: h.source_complete(source_id), range(4)))
    assert all(response.status_code == 202 for response in results)
    with h.db() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.session_id == h.session_id)) == 1


def test_reconcile_completed_storage_after_db_crash(make_video):
    h = make_video()
    source_id = UUID(h.source(segments=RANGE).json()["upload_id"])
    h.source_send(source_id)
    with h.db.begin() as db:
        upload = db.get(VideoUpload, source_id)
        upload.state = UploadState.COMPLETING
        key, provider_id = upload.object_key, upload.provider_upload_id
    h.storage.complete(key, provider_id, h.storage.list_pieces(key, provider_id))
    assert h.reconciler().reconcile(source_id) == "accepted"
    assert h.reconciler().reconcile(source_id) == "unchanged"
    assert h.row(VideoUpload, source_id).processing_state == "queued"
    with h.db() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.session_id == h.session_id)) == 1


@pytest.mark.parametrize("case", ["mismatch", "lost", "incomplete", "expiry"])
def test_reconcile_failures_and_expiry(make_video, case):
    h = make_video()
    source_id = UUID(h.source(segments=RANGE).json()["upload_id"])
    if case == "mismatch":
        h.source_send(source_id, b"wrong")
    with h.db.begin() as db:
        upload = db.get(VideoUpload, source_id)
        key, provider_id = upload.object_key, upload.provider_upload_id
        if case == "expiry":
            upload.created_at = db.scalar(select(func.clock_timestamp())) - timedelta(days=2)
        else:
            upload.state = UploadState.COMPLETING
    if case == "mismatch":
        h.storage.complete(key, provider_id, h.storage.list_pieces(key, provider_id))
    elif case == "lost":
        h.storage.abort(key, provider_id)
    result = h.reconciler().reconcile(source_id)
    assert result == {"mismatch": "aborted", "lost": "aborted", "incomplete": "reopened", "expiry": "expired"}[case]
    if case != "incomplete":
        assert h.part().source_video_id is None
        assert h.source().status_code == 201


def test_dead_job_releases_reservation_allows_followup_and_finish(make_video):
    h = make_video()
    source_id = UUID(h.source(segments=RANGE).json()["upload_id"])
    h.source_send(source_id)
    assert h.source_complete(source_id).status_code == 202
    with h.db.begin() as db:
        job = db.scalar(select(Job).where(Job.session_id == h.session_id))
        job.state = "dead"
        # The sweep is global. Make this test's candidate first without depending
        # on how many other sessions previous integration tests left behind.
        db.get(VideoUpload, source_id).updated_at = datetime(1900, 1, 1, tzinfo=timezone.utc)
    h.reconciler().sweep(limit=1)
    settled = h.row(VideoUpload, source_id)
    assert settled.processing_state == "failed" and settled.reason == "VIDEO_PROCESSING_FAILED"
    assert h.part().source_video_id is None
    assert h.finalize().status_code == 202
    assert h.part().failure_reason == "VIDEO_ZONE_UNCONFIRMED"
    assert h.source().status_code == 409
    assert h.source_complete(source_id).status_code == 409


def test_source_and_guided_share_session_byte_cap(make_video):
    h = make_video(session_upload_limit_bytes=1000)
    assert h.source(size=700).status_code == 201
    assert h.start(301, part_no=2).status_code == 413
    assert h.start(300, part_no=2).status_code == 201
    other = make_video(session_upload_limit_bytes=1000)
    assert other.start(700, part_no=2).status_code == 201
    assert other.source(size=301).status_code == 413


def test_database_rejects_cross_session_binding_and_second_live_source(make_video):
    h, other = make_video(), make_video()
    source_id = UUID(h.source().json()["upload_id"])
    with pytest.raises(IntegrityError), h.db.begin() as db:
        part = db.get(Part, other.part().id)
        part.source_video_id = source_id
        db.flush()
    with pytest.raises(IntegrityError), h.db.begin() as db:
        db.add(VideoUpload(session_id=h.session_id, object_key=f"raw/{h.session_id}/{uuid4()}",
            provider_upload_id=str(uuid4()), expected_bytes=100, content_type="video/mp4"))
        db.flush()


def test_reconciliation_never_accepts_after_deletion(make_video):
    h = make_video()
    source_id = UUID(h.source(segments=RANGE).json()["upload_id"])
    h.source_send(source_id)
    with h.db.begin() as db:
        upload = db.get(VideoUpload, source_id)
        upload.state = UploadState.COMPLETING
        h.storage.complete(upload.object_key, upload.provider_upload_id,
            h.storage.list_pieces(upload.object_key, upload.provider_upload_id))
        db.get(Session, h.session_id).state = SessionState.DELETING
    assert h.reconciler().reconcile(source_id) == "unchanged"
    assert h.row(VideoUpload, source_id).state == UploadState.COMPLETING
    with h.db() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.session_id == h.session_id)) == 0


def test_failed_head_after_completion_never_reopens(make_video, monkeypatch):
    h = make_video()
    source_id = UUID(h.source().json()["upload_id"])
    h.source_send(source_id)
    original = h.storage.head_size
    def unavailable(_):
        raise StorageError("Denied", definitive=True)
    monkeypatch.setattr(h.storage, "head_size", unavailable)
    assert h.source_complete(source_id).status_code == 503
    assert h.row(VideoUpload, source_id).state == UploadState.COMPLETING
    monkeypatch.setattr(h.storage, "head_size", original)
    assert h.reconciler().reconcile(source_id) == "accepted"


def test_concurrent_source_and_guided_reservation_serialize(make_video):
    h = make_video()
    with ThreadPoolExecutor(max_workers=2) as pool:
        source = pool.submit(h.source, segments=RANGE)
        guided = pool.submit(h.start, 100)
        results = [source.result(), guided.result()]
    assert sorted(response.status_code for response in results) == [201, 409]
    part = h.part()
    assert (part.source_video_id is not None) != (part.active_upload_id is not None)


@pytest.mark.parametrize("accepted", [False, True])
def test_purge_removes_source_media_multipart_and_metadata(make_video, accepted):
    h = make_video()
    source_id = UUID(h.source(segments=RANGE).json()["upload_id"])
    h.source_send(source_id)
    if accepted:
        assert h.source_complete(source_id).status_code == 202
    source = h.row(VideoUpload, source_id)
    h.storage.internal.put_object(Bucket=h.settings.s3_bucket,
        Key=f"media/{h.session_id}/source/{source_id}/manifest.json", Body=b"{}")
    with h.db.begin() as db:
        db.get(Session, h.session_id).state = SessionState.DELETING
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    lease = Lease(uuid4(), h.session_id, uuid4(), 1, "purge", "session", str(h.session_id), deadline, deadline)
    publication = PurgeHandler(h.db, h.storage)(lease)
    with h.db.begin() as db:
        db.scalar(select(Session).where(Session.id == h.session_id).with_for_update())
        publication.publish(db)
    assert h.storage.head_size(source.object_key) is None
    assert h.storage.list_multipart(f"raw/{h.session_id}/") == ()
    assert h.storage.list_keys(f"media/{h.session_id}/") == ()
    assert h.row(VideoUpload, source_id) is None
    assert h.row(Session, h.session_id).state == SessionState.DELETED


def test_terminal_source_sweep_not_starved_by_active_source_job(make_video):
    active, terminal = make_video(), make_video()
    source_ids = []
    for h in (active, terminal):
        source_id = UUID(h.source().json()["upload_id"])
        h.source_send(source_id)
        assert h.source_complete(source_id).status_code == 202
        source_ids.append(source_id)
    with terminal.db.begin() as db:
        job = db.scalar(select(Job).where(Job.session_id == terminal.session_id))
        job.state = "dead"
        # Establish a deterministic oldest-active / next-terminal pair while
        # leaving unrelated shared-database sessions untouched.
        db.get(VideoUpload, source_ids[0]).updated_at = datetime(1900, 1, 1, tzinfo=timezone.utc)
        db.get(VideoUpload, source_ids[1]).updated_at = datetime(1900, 1, 2, tzinfo=timezone.utc)
    terminal.reconciler().sweep(limit=1)
    assert terminal.row(VideoUpload, source_ids[1]).processing_state == "failed"
    assert active.row(VideoUpload, source_ids[0]).processing_state == "queued"
