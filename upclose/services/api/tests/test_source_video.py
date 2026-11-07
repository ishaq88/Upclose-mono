"""Source ingest against real PostgreSQL, Floci and synthetic ffmpeg footage."""
from datetime import timedelta
import hashlib
from pathlib import Path
import shutil
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update

from app.contracts.enums import PartState, SessionState
from app.jobs.queue import JobQueue
from app.jobs.worker import StageRegistry, run_once
from app.models import Artifact, Job, Part, PipelineRun, Session, VideoUpload
from app.pipeline.media import FrameManifest
from app.pipeline.source_video import SourceVideoHandler, register_source_video_handlers
from app.services.video_uploads import VideoUploadReconciler
from tests import media_fixtures as fx
from tests.test_uploads import Harness, bucket, session_factory  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


class VideoHarness(Harness):
    def new_owner(self, http):
        response = http.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
            "consent_version": "upclose-prototype-v1", "capture_mode": "single_video"})
        assert response.status_code == 201, response.text
        body = response.json()
        return UUID(body["session_id"]), {"Authorization": f"Bearer {body['session_token']}"}


@pytest.fixture
def make_video(session_factory, bucket):
    def build(**settings):
        return VideoHarness(session_factory, bucket, **settings)
    return build


@pytest.fixture(autouse=True)
def isolate_source_jobs(session_factory):
    with session_factory.begin() as db:
        db.execute(update(Job).where(Job.stage == "source_ingest", Job.state.in_(("queued", "running")))
                   .values(state="dead", lease_until=None, lease_token=None, worker_id=None))


def accept_source(h: VideoHarness, path: Path, segments: list[dict] | None = None) -> UUID:
    """Shared helper for report/purge integration: actual owner multipart acceptance."""
    data = path.read_bytes()
    response = h.http.post(f"/api/v1/sessions/{h.session_id}/video", headers=h.headers,
                          json={"content_type": "video/webm" if path.suffix == ".webm" else "video/mp4",
                                "content_length": len(data), "segments": segments or []})
    assert response.status_code == 201, response.text
    source_id = UUID(response.json()["upload_id"])
    urls = h.http.post(f"/api/v1/video-uploads/{source_id}/urls", headers=h.headers,
                       json={"part_numbers": [1]})
    assert urls.status_code == 200, urls.text
    assert httpx.put(urls.json()["items"][0]["url"], content=data, timeout=30).status_code == 200
    completed = h.http.post(f"/api/v1/video-uploads/{source_id}/complete", headers=h.headers, json={})
    assert completed.status_code == 202, completed.text
    return source_id


def source_rig(h: VideoHarness):
    registry = StageRegistry()
    handler = register_source_video_handlers(registry, h.db, h.storage, h.settings)
    return JobQueue(h.db), registry, handler


def clip(tmp_path: Path, name: str = "source.mp4", **kwargs) -> Path:
    return fx.make_pattern_clip(tmp_path / name, seconds=kwargs.pop("seconds", 4), **kwargs).path


def source_job(h):
    with h.db() as db:
        return db.scalar(select(Job).where(Job.session_id == h.session_id, Job.stage == "source_ingest"))


def artifacts(h):
    with h.db() as db:
        return list(db.scalars(select(Artifact).where(Artifact.session_id == h.session_id)))


def test_unclassified_source_is_ready_without_any_zone_or_findings(make_video, tmp_path):
    h = make_video()
    path = clip(tmp_path)
    source_id = accept_source(h, path)
    queue, registry, _ = source_rig(h)
    assert run_once(queue, registry, "source-test")
    source = h.row(VideoUpload, source_id)
    assert source.processing_state == "ready" and source.reason == "VIDEO_MAPPING_UNAVAILABLE"
    assert source.raw_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert source.duration_ms == 4000
    assert artifacts(h) == []
    with h.db() as db:
        parts = list(db.scalars(select(Part).where(Part.session_id == h.session_id)))
        assert all(part.state == PartState.PENDING and part.source_video_id is None for part in parts)
        assert list(db.scalars(select(PipelineRun).where(PipelineRun.session_id == h.session_id))) == []
    assert source_job(h).state == "succeeded"


@pytest.mark.parametrize("part_no", [1, 4])
def test_declared_range_publishes_exact_provenance_and_unverified_quality(make_video, tmp_path, part_no):
    h = make_video()
    path = clip(tmp_path)
    source_id = accept_source(h, path, [{"part_no": part_no, "start_ms": 1000, "end_ms": 3000}])
    queue, registry, _ = source_rig(h)
    assert run_once(queue, registry, "source-test")
    source, part = h.row(VideoUpload, source_id), h.part(part_no)
    assert source.processing_state == "ready", source.reason
    assert source.reason == "VIDEO_MAPPING_UNVERIFIED"
    assert part.state == PartState.PROCESSING and part.raw_key is None and part.active_upload_id is None
    assert part.source_video_id == source_id
    assert part.raw_sha256 == source.raw_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert part.quality["usable_for_scoring"] is False
    assert "source_mapping_unverified" in [warning["code"] for warning in part.quality["warnings"]]
    provenance = part.quality["source_video"]
    assert provenance["source_upload_id"] == str(source_id)
    assert provenance["source_sha256"] == source.raw_sha256
    assert provenance["mapping_basis"] == "user_declared"
    times = provenance["frame_timestamps_ms"]
    assert times[0] == 1000 and times[-1] < 3000 and times == sorted(set(times))
    rows = artifacts(h)
    manifest_row = next(row for row in rows if row.kind == "derivative")
    raw = h.storage.internal.get_object(Bucket=h.storage.bucket, Key=manifest_row.key)["Body"].read()
    manifest = FrameManifest.from_json(raw.decode())
    assert manifest.frame_count == len(times)
    assert sum(row.kind == "frame" for row in rows) == (8 if part_no == 4 else 0)
    with h.db() as db:
        runs = list(db.scalars(select(PipelineRun).where(PipelineRun.part_id == part.id)))
        assert len(runs) == 2  # source ingest and assessment committed in the same transaction
        assert db.scalar(select(Job).where(Job.session_id == h.session_id, Job.stage == "segment")) is not None


def test_genuine_blur_warning_is_retained_beside_mapping_warning(make_video, tmp_path):
    h = make_video()
    path = clip(tmp_path, video_filter="boxblur=20")
    source_id = accept_source(h, path, [{"part_no": 1, "start_ms": 0, "end_ms": 2000}])
    queue, registry, _ = source_rig(h)
    assert run_once(queue, registry, "source-test")
    assert h.row(VideoUpload, source_id).processing_state == "ready"
    quality = h.part().quality
    codes = [warning["code"] for warning in quality["warnings"]]
    assert "blur_strong" in codes and "source_mapping_unverified" in codes
    assert quality["media_usable_for_scoring"] is False and quality["usable_for_scoring"] is False


@pytest.mark.parametrize("failure", ["corrupt", "bounds", "missing", "config"])
def test_terminal_failure_releases_followup_reservations(make_video, tmp_path, failure):
    h = make_video()
    path = fx.make_garbage(tmp_path / "bad.mp4").path if failure == "corrupt" else clip(tmp_path)
    source_id = accept_source(h, path, [{"part_no": 2, "start_ms": 0,
                                       "end_ms": 5000 if failure == "bounds" else 2000}])
    if failure == "missing":
        h.storage.delete_object(h.row(VideoUpload, source_id).object_key)
    if failure == "config":
        with h.db.begin() as db:
            db.execute(update(Job).where(Job.id == source_job(h).id).values(config_hash="e" * 64))
    queue, registry, _ = source_rig(h)
    assert run_once(queue, registry, "source-test")
    source = h.row(VideoUpload, source_id)
    assert source.processing_state == "failed" and source.reason
    assert h.part(2).state == PartState.PENDING and h.part(2).source_video_id is None
    assert artifacts(h) == []
    assert h.start(100, 2).status_code == 201


def test_lost_lease_cannot_publish_and_retry_converges(make_video, tmp_path):
    h = make_video()
    source_id = accept_source(h, clip(tmp_path), [{"part_no": 1, "start_ms": 0, "end_ms": 2000}])
    queue, _, handler = source_rig(h)
    first = queue.claim("first", ["source_ingest"])
    publication = handler(first)
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == first.job_id)
                   .values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))
    second = queue.claim("second", ["source_ingest"])
    assert second is not None and second.attempt == 2
    assert not queue.complete(first, publication.publish, publication.downstream)
    assert artifacts(h) == [] and h.row(VideoUpload, source_id).processing_state == "queued"
    later = handler(second)
    assert queue.complete(second, later.publish, later.downstream)
    assert h.row(VideoUpload, source_id).processing_state == "ready"
    assert all("/a2/" in row.key for row in artifacts(h))
    converged = handler(second)
    assert converged.publish is None and converged.downstream == ()


def test_deletion_prevents_atomic_source_publication(make_video, tmp_path):
    h = make_video()
    source_id = accept_source(h, clip(tmp_path), [{"part_no": 1, "start_ms": 0, "end_ms": 2000}])
    queue, _, handler = source_rig(h)
    lease = queue.claim("source-test", ["source_ingest"])
    publication = handler(lease)
    with h.db.begin() as db:
        db.get(Session, h.session_id).state = SessionState.DELETING
    assert not queue.complete(lease, publication.publish, publication.downstream)
    assert artifacts(h) == [] and h.row(VideoUpload, source_id).processing_state == "queued"


def test_unexpected_failures_retry_then_settle_source(make_video, tmp_path, monkeypatch):
    h = make_video()
    source_id = accept_source(h, clip(tmp_path), [{"part_no": 1, "start_ms": 0, "end_ms": 2000}])
    queue, registry, handler = source_rig(h)
    def fail(*args):
        raise RuntimeError("synthetic fault")
    monkeypatch.setattr(handler, "_process", fail)
    for attempt in range(1, 4):
        assert run_once(queue, registry, "source-test")
        job = source_job(h)
        assert job.attempts == attempt
        if attempt < 3:
            assert h.row(VideoUpload, source_id).processing_state == "queued"
            with h.db.begin() as db:
                db.execute(update(Job).where(Job.id == job.id).values(available_at=func.clock_timestamp()))
    assert h.row(VideoUpload, source_id).processing_state == "failed"
    assert h.row(VideoUpload, source_id).reason == "VIDEO_PROCESSING_FAILED"
    assert h.part().source_video_id is None


def test_dead_source_job_reconciliation_settles_worker_crash(make_video, tmp_path):
    h = make_video()
    source_id = accept_source(h, clip(tmp_path), [{"part_no": 1, "start_ms": 0, "end_ms": 2000}])
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == source_job(h).id).values(state="dead", last_error="INTERNAL"))
    reconciler = VideoUploadReconciler(h.db, h.settings, h.storage)
    reconciler.reconcile(source_id)
    assert h.row(VideoUpload, source_id).processing_state == "failed"
    assert h.part().source_video_id is None and h.part().state == PartState.PENDING


def test_fanout_publication_failure_rolls_back_source_parts_runs_and_artifacts(make_video, tmp_path):
    from app.jobs.worker import Publication
    h = make_video()
    source_id = accept_source(h, clip(tmp_path), [{"part_no": 1, "start_ms": 0, "end_ms": 2000}])
    def fanout(outcome):
        def fail(db):
            raise RuntimeError("synthetic publication failure")
        return Publication(fail)
    registry = StageRegistry()
    register_source_video_handlers(registry, h.db, h.storage, h.settings, fanout=fanout)
    assert run_once(JobQueue(h.db), registry, "source-test")
    assert source_job(h).state == "queued"
    assert h.row(VideoUpload, source_id).processing_state == "queued"
    assert h.part().state == PartState.PENDING and h.part().quality is None
    assert artifacts(h) == []
    with h.db() as db:
        assert list(db.scalars(select(PipelineRun).where(PipelineRun.session_id == h.session_id))) == []
