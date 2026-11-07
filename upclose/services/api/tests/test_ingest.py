"""T05a ingest handler on real PostgreSQL, the Floci emulator and ffmpeg (synthetic media only).

Uploads go through the real API routes, so each test starts from an accepted part with its queued
ingest job. The handler is driven through the real queue claim path (`run_once`) or, where a test must
interleave lease events, by claiming and completing explicitly.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import shutil
from uuid import UUID, uuid4

from PIL import Image
import pytest
from sqlalchemy import func, select, update

from app.adapters.storage import S3Storage, StorageError
from app.contracts.enums import PartState, SessionState
from app.jobs.queue import Failure, JobQueue, JobSpec
from app.jobs.worker import Publication, StageFailure, StageRegistry, run_once
from app.models import Artifact, Job, Part, PipelineRun, Session
from app.pipeline import ingest
from app.pipeline.ingest import (IngestOutcome, attempt_prefix, extract_and_store_frames, register_ingest_handlers,
                                 sweep_ingest_scratch)
from app.pipeline.media import FrameManifest, MediaError, MediaReason, load_media_config, plan_frame_indices
from app.services.uploads import ingest_config_hashes
from tests import media_fixtures as fx
# Shared PostgreSQL/Floci fixtures; importing registers them in this module.
from tests.test_storage import storage_settings
from tests.test_uploads import Harness, bucket, make, session_factory  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")
H = "b" * 64


class FlakyStorage(S3Storage):
    """Injects storage faults on the server-side (worker) paths only."""
    download_failures = 0
    put_fail_on_call = 0  # 1-based put_file call number that raises once reached; 0 disables
    puts = 0

    def download_to(self, key, path, max_bytes):
        if self.download_failures > 0:
            type(self).download_failures -= 1
            raise StorageError("InternalError")
        return super().download_to(key, path, max_bytes)

    def put_file(self, key, path, content_type):
        type(self).puts += 1
        if self.put_fail_on_call and self.puts >= self.put_fail_on_call:
            raise StorageError("InternalError")
        super().put_file(key, path, content_type)


@pytest.fixture(autouse=True)
def isolate(session_factory):
    """Other modules leave queued ingest jobs in the shared test database; only ours may be claimed."""
    FlakyStorage.download_failures, FlakyStorage.put_fail_on_call, FlakyStorage.puts = 0, 0, 0
    with session_factory.begin() as db:
        db.execute(update(Job).where(Job.state.in_(("queued", "running"))).values(
            state="dead", lease_until=None, lease_token=None, worker_id=None))


@pytest.fixture
def scratch_root(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    path = tmp_path / "media"
    path.mkdir()
    return path


class Rig:
    def __init__(self, h: Harness, **kwargs: object) -> None:
        self.h = h
        self.queue = JobQueue(h.db)
        self.registry = StageRegistry()
        self.handler = register_ingest_handlers(self.registry, h.db, h.storage, h.settings, **kwargs)

    def run(self) -> bool:
        return run_once(self.queue, self.registry, "ingest-test")

    def claim(self):
        return self.queue.claim("ingest-test", ["ingest"])

    def finish(self, lease, publication: Publication) -> bool:
        return self.queue.complete(lease, publication.publish, publication.downstream)


def accept(h: Harness, path: Path, part_no: int = 1, content_type: str = "video/mp4") -> bytes:
    data = path.read_bytes()
    started = h.start(len(data), part_no, content_type)
    assert started.status_code == 201, started.text
    upload_id = UUID(started.json()["upload_id"])
    assert h.send(upload_id, 1, data).status_code == 200
    assert h.complete(upload_id).status_code == 202
    return data


def part_of(h: Harness, part_no: int = 1) -> Part:
    return h.part(part_no)


def run_of(h: Harness, part_no: int = 1) -> PipelineRun:
    with h.db() as db:
        return db.scalar(select(PipelineRun).where(PipelineRun.part_id == part_of(h, part_no).id))


def job_of(h: Harness, stage: str = "ingest") -> Job:
    with h.db() as db:
        return db.scalar(select(Job).where(Job.session_id == h.session_id, Job.stage == stage))


def artifacts_of(h: Harness) -> list[Artifact]:
    with h.db() as db:
        return list(db.scalars(select(Artifact).where(Artifact.session_id == h.session_id).order_by(Artifact.key)))


def stored(h: Harness, key: str) -> bytes:
    return h.storage.internal.get_object(Bucket=h.storage.bucket, Key=key)["Body"].read()


def keys_under(h: Harness, prefix: str) -> list[str]:
    page = h.storage.internal.list_objects_v2(Bucket=h.storage.bucket, Prefix=prefix)
    return sorted(item["Key"] for item in page.get("Contents", []))


def run_prefix(h: Harness, part_no: int = 1) -> str:
    return f"media/{h.session_id}/{part_of(h, part_no).id}/"


def make_available(h: Harness, job_id: UUID) -> None:
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(available_at=func.clock_timestamp() - timedelta(seconds=1)))


def expire(h: Harness, job_id: UUID) -> None:
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))


def no_scratch_left(root: Path) -> bool:
    return not list(root.glob("upclose-media-*"))


def clip(media_dir: Path, name: str = "clip.mp4", **kwargs: object) -> Path:
    return fx.make_pattern_clip(media_dir / name, size=kwargs.pop("size", (320, 180)), **kwargs).path


def assert_published_set(h: Harness, data: bytes, part_no: int = 1, frames: int = 0) -> tuple[Part, list[Artifact]]:
    """Video parts store proxy + manifest only (frames=0); stills parts also store their planned stills."""
    part, run = part_of(h, part_no), run_of(h, part_no)
    assert part.state == PartState.PROCESSING and part.failure_reason is None
    assert part.raw_sha256 == hashlib.sha256(data).hexdigest()
    assert run.state == "running" and job_of(h).state == "succeeded"
    rows = artifacts_of(h)
    kinds = sorted(row.kind for row in rows)
    assert kinds.count("proxy") == 1 and kinds.count("derivative") == 1 and kinds.count("frame") == frames
    assert len(kinds) == len(rows) and all(row.privacy_state.value == "pending" and row.purge_state == "retained" for row in rows)
    prefix = f"media/{h.session_id}/{part.id}/{run.id}/a1/"
    assert all(row.key.startswith(prefix) for row in rows)
    for row in rows:  # every row points at a stored private object whose bytes hash to the recorded value
        assert hashlib.sha256(stored(h, row.key)).hexdigest() == row.sha256
    assert keys_under(h, run_prefix(h, part_no)) == sorted(row.key for row in rows)
    return part, rows


# --------------------------------------------------------------------------- acceptance


@pytest.mark.parametrize("container,content_type", [("mp4", "video/mp4"), ("webm", "video/webm")])
def test_accepted_clip_ingests_to_processing_through_the_real_claim_path(make, media_dir, scratch_root, container, content_type) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    data = accept(h, clip(media_dir, f"clip.{container}", container=container), content_type=content_type)
    assert part_of(h).state == PartState.UPLOADED and job_of(h).state == "queued"
    assert rig.run() is True
    part, rows = assert_published_set(h, data)
    proxy = next(row for row in rows if row.kind == "proxy")
    manifest_row = next(row for row in rows if row.kind == "derivative")
    manifest = FrameManifest.from_json(stored(h, manifest_row.key).decode())
    assert manifest.proxy_sha256 == proxy.sha256 and manifest.content_hash == manifest_row.sha256
    assert run_of(h).config_hashes["media"] == load_media_config().content_hash == ingest_config_hashes()["media"]
    assert not [row for row in rows if row.kind == "frame"]  # video-mode part: no frames at ingest
    quality = part.quality
    assert quality["thresholds_provisional"] is True and "usable_for_scoring" in quality
    assert quality["config_hash"] == load_media_config().content_hash
    assert no_scratch_left(scratch_root)
    assert rig.run() is False  # nothing left to claim


@pytest.mark.parametrize("source", ["portrait", "rotated"])
def test_portrait_frames_stay_portrait_and_stills_part_gets_its_configured_count(make, media_dir, scratch_root, source) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    if source == "portrait":
        path = clip(media_dir, size=(180, 320), fps=10, seconds=2.0)
    else:
        path = fx.make_rotated_clip(media_dir / "rot.mp4", 90, size=(320, 180)).path
    data = accept(h, path, part_no=4)  # part 4 is a stills plan: 8 frames
    assert Rig(h).run() is True
    _, rows = assert_published_set(h, data, part_no=4, frames=8)
    manifest = FrameManifest.from_json(stored(h, next(r for r in rows if r.kind == "derivative").key).decode())
    assert manifest.height > manifest.width
    frames = [row for row in rows if row.kind == "frame"]
    plan = load_media_config().plan_for(4)
    assert len(frames) == plan.count == 8
    planned = plan_frame_indices(manifest, plan)  # exactly the planned stills, nothing else
    assert [int(row.key.rsplit("_", 1)[1].split(".")[0]) for row in frames] == list(planned)
    for row in frames:
        width, height = Image.open(io.BytesIO(stored(h, row.key))).size
        assert (width, height) == (manifest.width, manifest.height)


def test_blurry_clip_is_processed_with_a_scoring_warning_never_a_clean_result(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    path = clip(media_dir, "blurry.mp4", size=(640, 360), video_filter="boxblur=luma_radius=18:luma_power=3")
    data = accept(h, path)
    assert Rig(h).run() is True
    part, _ = assert_published_set(h, data)
    quality = part.quality
    assert quality["usable_for_scoring"] is False and quality["thresholds_provisional"] is True
    assert any(w["code"].startswith("blur") and w["blocks_scoring"] for w in quality["warnings"])


def test_corrupt_upload_fails_the_part_with_a_reason_and_no_artifacts(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    data = accept(h, fx.make_garbage(media_dir / "bad.mp4").path)
    assert Rig(h).run() is True
    part = part_of(h)
    assert part.state == PartState.FAILED and part.failure_reason.startswith("MEDIA_")
    assert str(scratch_root) not in part.failure_reason and "/" not in part.failure_reason
    assert part.raw_sha256 == hashlib.sha256(data).hexdigest() and part.quality is None
    assert run_of(h).state == "failed" and job_of(h).state == "succeeded"
    assert artifacts_of(h) == [] and keys_under(h, run_prefix(h)) == []
    assert no_scratch_left(scratch_root)


def test_over_duration_clip_fails_the_part(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    accept(h, fx.make_long_tiny_clip(media_dir / "long.mp4", 62).path)
    assert Rig(h).run() is True
    part = part_of(h)
    assert (part.state, part.failure_reason) == (PartState.FAILED, "MEDIA_DURATION_EXCEEDED")
    assert artifacts_of(h) == [] and no_scratch_left(scratch_root)


# --------------------------------------------------------------------------- leases and convergence


def test_stale_attempt_cannot_publish_and_its_objects_are_never_linked(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    data = accept(h, clip(media_dir))
    stale = rig.claim()
    stale_publication = rig.handler(stale)  # I/O done, objects uploaded under attempt 1
    stale_keys = keys_under(h, run_prefix(h))
    assert stale_keys and all("/a1/" in key for key in stale_keys)
    expire(h, stale.job_id)
    current = rig.claim()
    assert current.attempt == 2 and current.token != stale.token
    assert rig.finish(current, rig.handler(current)) is True
    assert rig.finish(stale, stale_publication) is False  # lost the fence
    part, rows = part_of(h), artifacts_of(h)
    assert part.state == PartState.PROCESSING and part.raw_sha256 == hashlib.sha256(data).hexdigest()
    assert rows and all("/a2/" in row.key for row in rows)
    orphans = set(keys_under(h, run_prefix(h))) - {row.key for row in rows}
    assert orphans == set(stale_keys)  # known limitation: unlinked objects await garbage collection
    assert no_scratch_left(scratch_root)


def test_rerun_after_a_crash_before_publish_converges_to_one_artifact_set(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    accept(h, clip(media_dir))
    crashed = rig.claim()
    rig.handler(crashed)  # the worker dies here: nothing published
    assert part_of(h).state == PartState.UPLOADED and artifacts_of(h) == []
    expire(h, crashed.job_id)
    assert rig.run() is True
    rows = artifacts_of(h)
    assert rows and {row.key.split("/")[4] for row in rows} == {"a2"}
    assert sum(row.kind == "proxy" for row in rows) == 1 and sum(row.kind == "derivative" for row in rows) == 1
    assert job_of(h).state == "succeeded" and part_of(h).state == PartState.PROCESSING


def test_an_already_ingested_part_is_not_processed_twice(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    accept(h, clip(media_dir))
    first = rig.claim()
    assert rig.finish(first, rig.handler(first)) is True
    before = [row.key for row in artifacts_of(h)]
    # Simulate a duplicate delivery of the same subject: the handler converges without new objects.
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == first.job_id).values(state="queued", attempts=1,
                   available_at=func.clock_timestamp() - timedelta(seconds=1)))
    again = rig.claim()
    publication = rig.handler(again)
    assert publication.publish is None and rig.finish(again, publication) is True
    assert [row.key for row in artifacts_of(h)] == before and keys_under(h, run_prefix(h)) == sorted(before)


def test_deleting_session_publishes_nothing(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    accept(h, clip(media_dir))
    lease = rig.claim()
    publication = rig.handler(lease)
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.DELETING))
    assert rig.finish(lease, publication) is False
    assert part_of(h).state == PartState.UPLOADED and artifacts_of(h) == []
    assert no_scratch_left(scratch_root)


def test_handler_refuses_a_session_that_began_deleting_before_it_ran(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    accept(h, clip(media_dir))
    lease = rig.claim()
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.DELETING))
    with pytest.raises(StageFailure) as refused:
        rig.handler(lease)
    assert refused.value.failure.retryable is False
    assert keys_under(h, run_prefix(h)) == [] and no_scratch_left(scratch_root)


def test_expired_deadline_is_retryable_not_a_clean_result(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    accept(h, clip(media_dir))
    lease = rig.claim()
    late = replace(lease, deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(StageFailure) as refused:
        rig.handler(late)
    assert refused.value.failure == Failure("TRANSIENT") and part_of(h).state == PartState.UPLOADED


# --------------------------------------------------------------------------- retry budget


def exhaust(h: Harness, rig: Rig, expected_reason: str) -> None:
    """Attempts 1 and 2 are retried with the part still `uploaded`; attempt 3 fails the part."""
    for attempt in (1, 2):
        assert rig.run() is True
        job = job_of(h)
        assert (job.state, job.attempts, part_of(h).state) == ("queued", attempt, PartState.UPLOADED)
        make_available(h, job.id)
    assert rig.run() is True
    job, part = job_of(h), part_of(h)
    assert (job.state, job.attempts) == ("succeeded", 3)
    assert (part.state, part.failure_reason) == (PartState.FAILED, expected_reason)
    assert run_of(h).state == "failed" and artifacts_of(h) == []


def test_transient_storage_error_is_retried_then_succeeds(make, media_dir, scratch_root) -> None:
    h = make(storage_cls=FlakyStorage, ingest_scratch_root=str(scratch_root))
    FlakyStorage.download_failures = 1
    rig = Rig(h)
    data = accept(h, clip(media_dir))
    assert rig.run() is True
    job = job_of(h)
    assert (job.state, job.attempts, job.last_error) == ("queued", 1, "TRANSIENT")
    assert part_of(h).state == PartState.UPLOADED and no_scratch_left(scratch_root)
    make_available(h, job.id)
    assert rig.run() is True
    assert_published_set_attempt(h, data, attempt=2)


def assert_published_set_attempt(h: Harness, data: bytes, attempt: int) -> None:
    part, rows = part_of(h), artifacts_of(h)
    assert part.state == PartState.PROCESSING and part.raw_sha256 == hashlib.sha256(data).hexdigest()
    assert rows and all(f"/a{attempt}/" in row.key for row in rows)


def test_persistent_storage_error_fails_the_part_on_the_last_attempt(make, media_dir, scratch_root) -> None:
    h = make(storage_cls=FlakyStorage, ingest_scratch_root=str(scratch_root))
    FlakyStorage.download_failures = 99
    rig = Rig(h)
    accept(h, clip(media_dir))
    exhaust(h, rig, ingest.STORAGE_UNAVAILABLE)
    assert no_scratch_left(scratch_root)


def test_failed_upload_midway_deletes_the_objects_it_wrote(make, media_dir, scratch_root) -> None:
    h = make(storage_cls=FlakyStorage, ingest_scratch_root=str(scratch_root))
    FlakyStorage.puts, FlakyStorage.put_fail_on_call = 0, 4  # proxy, manifest, first still ok; second still fails
    rig = Rig(h)
    accept(h, clip(media_dir), part_no=4)
    assert rig.run() is True
    assert job_of(h).state == "queued" and part_of(h, 4).state == PartState.UPLOADED
    assert keys_under(h, run_prefix(h, 4)) == [] and artifacts_of(h) == []


@pytest.mark.parametrize("reason,expected", [(MediaReason.TIMEOUT, "MEDIA_TIMEOUT"),
                                              (MediaReason.RESOURCE_LIMIT, "MEDIA_RESOURCE_LIMIT")])
def test_media_timeout_and_resource_limit_use_the_retry_budget(make, media_dir, scratch_root, monkeypatch, reason, expected) -> None:
    def refuse(*_: object, **__: object):
        raise MediaError(reason, "limit")
    monkeypatch.setattr(ingest, "normalize_and_assess", refuse)
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    accept(h, clip(media_dir))
    exhaust(h, rig, expected)
    assert no_scratch_left(scratch_root)


def test_unexpected_error_is_retried_then_fails_without_leaking_detail(make, media_dir, scratch_root, monkeypatch) -> None:
    def explode(*_: object, **__: object):
        raise RuntimeError("secret detail /private/path")
    monkeypatch.setattr(ingest, "normalize_and_assess", explode)
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    accept(h, clip(media_dir))
    exhaust(h, rig, ingest.INTERNAL_ERROR)
    assert "secret" not in part_of(h).failure_reason and job_of(h).last_error is None  # succeeded job clears it


# --------------------------------------------------------------------------- durable-state guards


def test_media_config_that_differs_from_the_runs_frozen_hash_fails_terminally(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    accept(h, clip(media_dir))
    with h.db.begin() as db:
        db.execute(update(PipelineRun).where(PipelineRun.session_id == h.session_id).values(config_hashes={"media": "0" * 64}))
    assert Rig(h).run() is True
    part = part_of(h)
    assert (part.state, part.failure_reason) == (PartState.FAILED, ingest.CONFIG_MISMATCH)
    assert artifacts_of(h) == [] and keys_under(h, run_prefix(h)) == []


def test_raw_object_missing_or_changed_after_acceptance_fails_terminally(make, media_dir, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    accept(h, clip(media_dir, "a.mp4"))
    key = part_of(h).raw_key
    h.storage.delete_object(key)
    assert Rig(h).run() is True
    assert part_of(h).failure_reason == ingest.OBJECT_MISSING

    for part_no, payload in ((2, b"shorter"), (3, os.urandom(200_000))):
        accept(h, clip(media_dir, f"b{part_no}.mp4"), part_no=part_no)
        h.storage.internal.put_object(Bucket=h.storage.bucket, Key=part_of(h, part_no).raw_key, Body=payload)
        assert Rig(h).run() is True
        part = part_of(h, part_no)
        assert (part.state, part.failure_reason) == (PartState.FAILED, ingest.OBJECT_CHANGED)
    assert artifacts_of(h) == [] and no_scratch_left(scratch_root)


# --------------------------------------------------------------------------- T05b seam


def test_fanout_seam_rows_and_jobs_commit_atomically_with_processing(make, media_dir, scratch_root) -> None:
    seen: list[IngestOutcome] = []
    subject = str(uuid4())

    def planner(outcome: IngestOutcome) -> Publication:
        seen.append(outcome)
        spec = JobSpec(outcome.session_id, "concept_run", subject, "segment", H, H)
        return Publication(downstream=(spec,))

    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h, fanout=planner)
    data = accept(h, clip(media_dir))
    stale = rig.claim()
    stale_publication = rig.handler(stale)
    expire(h, stale.job_id)
    assert rig.run() is True
    assert rig.finish(stale, stale_publication) is False
    assert len(seen) == 2  # the stale attempt planned too, but its publication was fenced out
    outcome = seen[-1]
    assert outcome.part_no == 1 and outcome.raw_sha256 == hashlib.sha256(data).hexdigest()
    assert outcome.usable_for_scoring is True and outcome.frames == () and outcome.proxy_key.endswith("/proxy.mp4")
    with h.db() as db:
        segment_jobs = list(db.scalars(select(Job).where(Job.session_id == h.session_id, Job.stage == "segment")))
    assert len(segment_jobs) == 1 and segment_jobs[0].subject_id == subject and segment_jobs[0].state == "queued"
    assert part_of(h).state == PartState.PROCESSING


def test_failing_fanout_rolls_back_the_whole_publication(make, media_dir, scratch_root) -> None:
    def broken(db) -> None:
        raise RuntimeError("fan-out failed")

    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h, fanout=lambda outcome: Publication(publish=broken))
    accept(h, clip(media_dir))
    assert rig.run() is True
    assert part_of(h).state == PartState.UPLOADED and run_of(h).state == "pending"
    assert artifacts_of(h) == [] and job_of(h).state == "queued"


# --------------------------------------------------------------------------- scratch and storage helpers


def test_startup_sweep_removes_only_stale_scratch(make, scratch_root) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    old, fresh = scratch_root / "upclose-media-old", scratch_root / "upclose-media-fresh"
    for directory in (old, fresh):
        directory.mkdir(parents=True)
        (directory / ".upclose-scratch").touch()
    unrelated = scratch_root / "keep-me"
    unrelated.mkdir()
    aged = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(old / ".upclose-scratch", (aged, aged))
    assert sweep_ingest_scratch(h.settings) == 1
    assert not old.exists() and fresh.exists() and unrelated.exists()


def test_config_hash_is_the_media_config_content_hash() -> None:
    assert ingest_config_hashes() == {"media": load_media_config().content_hash}


def test_download_to_is_bounded_hashed_and_private(bucket, tmp_path) -> None:
    storage = S3Storage(storage_settings(bucket))
    key, payload = f"ingest-test/{uuid4().hex}", os.urandom(3 * 1024 * 1024 + 17)
    source = tmp_path / "up.bin"
    source.write_bytes(payload)
    storage.put_file(key, source, "application/octet-stream")
    target = tmp_path / "down.bin"
    result = storage.download_to(key, target, len(payload))
    assert (result.size_bytes, result.sha256) == (len(payload), hashlib.sha256(payload).hexdigest())
    assert target.read_bytes() == payload
    smaller = tmp_path / "small.bin"
    with pytest.raises(StorageError) as too_large:
        storage.download_to(key, smaller, len(payload) - 1)
    assert too_large.value.code == "ObjectTooLarge" and too_large.value.definitive and not smaller.exists()
    with pytest.raises(StorageError) as missing:
        storage.download_to(f"ingest-test/{uuid4().hex}", tmp_path / "none.bin", 10)
    assert missing.value.code in {"NoSuchKey", "404"} and not (tmp_path / "none.bin").exists()
    storage.delete_object(key)


def test_extract_and_store_frames_serves_a_video_part_for_cited_indices_only(make, media_dir, scratch_root, tmp_path) -> None:
    h = make(ingest_scratch_root=str(scratch_root))
    rig = Rig(h)
    data = accept(h, fx.make_color_steps(media_dir / "steps.mp4", size=(160, 90), fps=10).path)  # 1 s each: R, G, B, Y
    assert rig.run() is True
    _, rows = assert_published_set(h, data)  # ingest stored proxy + manifest, no frames
    proxy = next(row for row in rows if row.kind == "proxy")
    manifest = FrameManifest.from_json(stored(h, next(row for row in rows if row.kind == "derivative").key).decode())
    local = tmp_path / "proxy.mp4"
    assert h.storage.download_to(proxy.key, local, 10_000_000).sha256 == proxy.sha256 == manifest.proxy_sha256
    cited = (20, 8)  # unordered on purpose; proxy is 6 fps so these are ~3.3 s and ~1.3 s
    part = part_of(h)
    later = attempt_prefix(part.session_id, part.id, run_of(h).id, 7)  # a later stage's own attempt-scoped prefix
    work = tmp_path / "work"
    work.mkdir()
    records = extract_and_store_frames(h.storage, local, manifest, cited, later, work, load_media_config())
    assert [record.frame_index for record in records] == [8, 20]
    expected = {8: (0, 255, 0), 20: (255, 255, 0)}  # green at 1.33 s, yellow at 3.33 s
    for record in records:
        assert record.pts == manifest.pts[record.frame_index] and record.key.startswith(later + "frames/")
        assert record.key == f"{later}frames/frame_{record.frame_index:06d}.jpg"
        assert (record.width, record.height) == (manifest.width, manifest.height)
        body = stored(h, record.key)
        assert hashlib.sha256(body).hexdigest() == record.sha256
        pixel = Image.open(io.BytesIO(body)).convert("RGB").getpixel((record.width // 2, record.height // 2))
        assert all(abs(got - want) <= 40 for got, want in zip(pixel, expected[record.frame_index]))
    assert keys_under(h, later) == sorted(record.key for record in records)  # only the cited frames exist
    assert len(artifacts_of(h)) == len(rows)  # storing is not linking: the caller publishes artifact rows
    with pytest.raises(ValueError):
        extract_and_store_frames(h.storage, local, manifest, (manifest.frame_count,), later, work, load_media_config())
