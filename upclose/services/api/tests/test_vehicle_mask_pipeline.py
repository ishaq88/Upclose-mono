"""T13c real PostgreSQL/Floci/ffmpeg tests with synthetic mask and damage providers."""
from __future__ import annotations

from collections import Counter
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image
import pytest
from sqlalchemy import delete, func, select, update

from app.adapters.meta import ProviderFailure, ProviderUsage
from app.contracts.enums import PartState, SessionState
from app.jobs.queue import Failure
from app.jobs.worker import StageRegistry
from app.models import Artifact, ConceptRun, Finding, Job, Observation, PipelineRun, Session
from app.pipeline.orchestration import install_pipeline_handlers, masked_orchestration_context
from app.pipeline.segment import ScriptedSegmenter, ScriptedTrack
from app.pipeline.vehicle_mask import SyntheticVehicleSegmenter
from app.pipeline.vehicle_media import BACKGROUND
from tests.test_ingest import media_dir, scratch_root, stored  # noqa: F401
from tests.test_uploads import bucket, make, session_factory  # noqa: F401
from tests.test_orchestration import Pipe, concept_runs, coverage, findings, jobs, part, rows, setup, upload

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


@pytest.fixture(autouse=True)
def isolate(session_factory):
    with session_factory.begin() as db:
        db.execute(update(Job).where(Job.state.in_(("queued", "running"))).values(
            state="dead", lease_until=None, lease_token=None, worker_id=None))
    yield
    # The additive migration intentionally refuses downgrade while mask jobs
    # remain. Take owning sessions out of the reconciler's live states first,
    # otherwise a later sweep rebuilds the deleted mask job from durable state.
    with session_factory.begin() as db:
        db.execute(update(Session).where(Session.id.in_(
            select(Job.session_id).where(Job.stage == "vehicle_mask"))).values(state=SessionState.DELETING))
        db.execute(delete(Job).where(Job.stage == "vehicle_mask"))


class RecordingProvider:
    def __init__(self, h, *, failure=None, requires_budget=False):
        self.h, self.failure, self.requires_budget = h, failure, requires_budget
        self.calls = []
        self.damage_failures = 0
        self.mask_failures = 0
        self.reject = None  # "vehicle" or "damage": billed response our parser rejects
        self.rejected = []
        self.budget = None

    def segment(self, media, prompt, request_id):
        raw = stored(self.h, media.media_key)
        assert hashlib.sha256(raw).hexdigest() == media.content_sha256
        if self.requires_budget:
            assert self.budget.events[-1][0] == "dispatch"
        with self.h.db() as db:
            markers = list(db.scalars(select(Observation).where(
                Observation.session_id == self.h.session_id, Observation.kind == "vehicle_mask")))
        if prompt == "vehicle":
            assert "/vehicle/" not in media.media_key
            if self.reject == "vehicle":
                self.rejected.append(prompt)
                raise ProviderFailure("mask_alignment_mismatch")
            if self.mask_failures:
                self.mask_failures -= 1
                raise ProviderFailure("provider_timeout")
            result = SyntheticVehicleSegmenter().segment(media, prompt, request_id)
            if self.failure == "missing": result = result.model_copy(update={"observations": result.observations[:-1]})
            if self.failure == "too_few": result = result.model_copy(update={"observations": result.observations[:len(result.observations) // 3]})
            if self.failure == "empty":
                result = result.model_copy(update={"observations": tuple(item.model_copy(update={
                    "mask": item.mask.model_copy(update={"pixels": b"\x00" * len(item.mask.pixels)})}) for item in result.observations)})
            if self.failure == "ambiguous":
                result = result.model_copy(update={"observations": tuple(item.model_copy(update={
                    "track_id": "other" if i else item.track_id}) for i, item in enumerate(result.observations))})
        else:
            assert "/vehicle/" in media.media_key
            assert any(any(item["masked_key"] == media.media_key for item in row.value["media"]) for row in markers)
            with TemporaryDirectory() as scratch:
                path = Path(scratch) / "input"
                path.write_bytes(raw)
                if media.kind == "video":
                    output = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                                            capture_output=True, check=True, timeout=30).stdout
                else:
                    output = raw
                with Image.open(io.BytesIO(output)) as image:
                    assert image.size == media.display_size
                    assert image.convert("RGB").getpixel((0, 0)) == BACKGROUND
            if self.reject == "damage":
                self.rejected.append(prompt)
                raise ProviderFailure("invalid_segmentation")
            if self.damage_failures:
                self.damage_failures -= 1
                raise ProviderFailure("provider_timeout")
            script = {prompt: (ScriptedTrack("1", ((.3, .3, .3, .2, .2, .9),)),)}
            result = ScriptedSegmenter(script).segment(media, prompt, request_id)
        self.calls.append((prompt, media))
        return result.model_copy(update={"usage": ProviderUsage(
            video_frames_processed=max(50, len(media.frames)) if media.kind == "video" else None,
            images_processed=1 if media.kind == "image" else None)})


def pipe_for(h, provider):
    return Pipe(h, ctx=masked_orchestration_context(), factory=lambda _: provider)


@pytest.mark.parametrize("part_no", [1, 2, 3, 4, 6, 7])
def test_all_enabled_zones_wait_for_mask_and_damage_reads_only_masked_bytes(make, media_dir, scratch_root, part_no):
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, part_no)
    assert pipe.step()  # ingest creates mask work, no B work
    assert jobs(h, "vehicle_mask") and not jobs(h, "segment") and not provider.calls
    assert pipe.step()  # A publication commits marker and releases B
    marker, = rows(h, Observation, Observation.kind == "vehicle_mask")
    mask_calls = list(provider.calls)
    assert all(prompt == "vehicle" for prompt, _ in mask_calls)
    assert len(mask_calls) == len(marker.value["media"])
    assert jobs(h, "segment")
    pipe.drain()
    assert part(h, part_no).state == PartState.PROCESSED
    assert all(row.state == "succeeded" for row in rows(h, ConceptRun))
    assert any(prompt != "vehicle" for prompt, _ in provider.calls)
    assert sum(prompt == "vehicle" for prompt, _ in provider.calls) == len(mask_calls)
    assert findings(h)
    for finding in findings(h):
        assert "/vehicle/" not in finding.frame_key
        assert finding.frame_index >= 0
        artifact, = rows(h, Artifact, Artifact.key == finding.frame_key)
        assert artifact.sha256 == hashlib.sha256(stored(h, finding.frame_key)).hexdigest()
    assert not any(scratch_root.rglob("*"))


@pytest.mark.parametrize("part_no", [5, 8])
def test_unsupported_zones_have_no_mask_or_damage_calls(make, media_dir, scratch_root, part_no):
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, part_no)
    pipe.drain()
    assert not provider.calls and not jobs(h, "vehicle_mask") and not jobs(h, "segment")
    assert part(h, part_no).state == PartState.PROCESSED


@pytest.mark.parametrize("shape", ["missing", "ambiguous"])
def test_documented_sam_gaps_and_track_changes_still_mask_the_vehicle(make, media_dir, scratch_root, shape):
    # SAM emits no line for a frame without the car, and ids can change after occlusion.
    h = setup(make, scratch_root)
    provider = RecordingProvider(h, failure=shape)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    pipe.drain()
    assert len(rows(h, Observation, Observation.kind == "vehicle_mask")) == 1
    assert any(prompt != "vehicle" for prompt, _ in provider.calls)


@pytest.mark.parametrize("failure", ["empty", "too_few"])
def test_failed_mask_terminates_dependents_without_damage_or_clean_coverage(make, media_dir, scratch_root, failure):
    h = setup(make, scratch_root)
    provider = RecordingProvider(h, failure=failure)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    pipe.drain()
    assert provider.calls and all(prompt == "vehicle" for prompt, _ in provider.calls)
    assert not rows(h, Observation, Observation.kind == "vehicle_mask")
    assert not jobs(h, "segment") and not findings(h)
    assert all(row.state == "failed" for row in rows(h, ConceptRun))
    assert all(row.state.value != "assessed" for row in coverage(h).values())
    assert not any(scratch_root.rglob("*"))


@pytest.mark.parametrize("corruption", ["masked_bytes", "mask_bytes", "missing_marker", "frame_mapping", "artifact_hash"])
def test_corrupt_committed_binding_blocks_every_damage_call(make, media_dir, scratch_root, corruption):
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    assert pipe.step() and pipe.step()
    marker, = rows(h, Observation, Observation.kind == "vehicle_mask")
    item = marker.value["media"][0]
    if corruption in ("masked_bytes", "mask_bytes"):
        key = item["masked_key"] if corruption == "masked_bytes" else item["mask_key"]
        h.storage.internal.put_object(Bucket=h.storage.bucket, Key=key, Body=b"synthetic corruption")
    else:
        with h.db.begin() as db:
            if corruption == "missing_marker": db.execute(delete(Observation).where(Observation.id == marker.id))
            elif corruption == "frame_mapping":
                value = {**marker.value, "media": [{**item, "timestamps_ms": [123] * len(item["timestamps_ms"])}]}
                db.get(Observation, marker.id).value = value
            else: db.execute(update(Artifact).where(Artifact.key == item["masked_key"]).values(sha256="0" * 64))
    pipe.drain()
    assert all(prompt == "vehicle" for prompt, _ in provider.calls)
    assert not findings(h)
    assert all(row.state == "failed" for row in rows(h, ConceptRun))


def test_restart_and_damage_retry_reuse_committed_group_a(make, media_dir, scratch_root):
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    assert pipe.step() and pipe.step()
    marker, = rows(h, Observation, Observation.kind == "vehicle_mask")
    provider.damage_failures = 1
    restarted = pipe_for(h, provider)
    restarted.drain()
    assert sum(prompt == "vehicle" for prompt, _ in provider.calls) == 1
    same, = rows(h, Observation, Observation.kind == "vehicle_mask")
    assert same.id == marker.id and same.value == marker.value
    assert max(job.attempts for job in jobs(h, "segment")) == 2
    assert all(row.state == "succeeded" for row in rows(h, ConceptRun))



@pytest.mark.parametrize("stage", ["vehicle", "damage"])
def test_rejected_billed_response_is_not_paid_for_again(make, media_dir, scratch_root, stage):
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    provider.reject = stage
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    pipe.drain()
    concept_runs = rows(h, ConceptRun)
    if stage == "vehicle":
        assert provider.rejected == ["vehicle"]
    else:
        assert len(provider.rejected) == len(concept_runs)  # one call per concept, no retries
    assert concept_runs and all(row.state == "failed" for row in concept_runs)
    assert all(row.state.value != "assessed" for row in coverage(h).values())
    assert not findings(h)


class RecordingBudget:
    def __init__(self, *, deny=False):
        self.events = []
        self.requests = {}
        self.deny = deny
    def reserve(self, request):
        self.events.append(("reserve", request.attempt_id))
        self.requests[request.attempt_id] = request
    def mark_dispatched(self, attempt_id, lease_token):
        self.events.append(("dispatch", attempt_id))
        return not self.deny
    def settle(self, attempt_id, **kwargs): self.events.append(("settle", attempt_id))
    def mark_unknown(self, attempt_id, **kwargs): self.events.append(("unknown", attempt_id))


@pytest.mark.parametrize("deny", [False, True])
def test_both_groups_require_independent_dispatch_grants(make, media_dir, scratch_root, deny):
    h = setup(make, scratch_root)
    budget = RecordingBudget(deny=deny)
    provider = RecordingProvider(h, requires_budget=True)
    provider.budget = budget
    pipe = Pipe(h, ctx=masked_orchestration_context(), fanout=False)
    pipe.registry = StageRegistry()
    install_pipeline_handlers(pipe.registry, h.db, h.storage, h.settings, pipe.ctx,
                              segmenter_factory=lambda _: provider, budget_service=budget)
    upload(h, media_dir, 1)
    pipe.drain()
    if deny:
        assert not provider.calls
        assert [kind for kind, _ in budget.events] == ["reserve", "dispatch"]
    else:
        calls = len(provider.calls)
        assert calls > 1
        assert Counter(kind for kind, _ in budget.events) == {"reserve": calls, "dispatch": calls, "settle": calls}
        assert len(budget.requests) == calls
        assert all(request.units.frames > 0 for request in budget.requests.values())
        assert all(row.state == "succeeded" for row in rows(h, ConceptRun))


@pytest.mark.parametrize("fence", ["stale", "deleted"])
def test_mask_publication_loses_authority_after_lease_or_owner_revocation(make, media_dir, scratch_root, fence):
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    assert pipe.step()
    lease = pipe.queue.claim("mask-interleave", ["vehicle_mask"])
    assert lease is not None
    publication = pipe.registry.handler("vehicle_mask")(lease)
    with h.db.begin() as db:
        if fence == "stale":
            db.execute(update(Job).where(Job.id == lease.job_id).values(lease_until=func.clock_timestamp()))
        else:
            db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.DELETING))
    assert not pipe.queue.complete(lease, publication.publish, publication.downstream)
    assert not rows(h, Observation, Observation.kind == "vehicle_mask")
    assert not jobs(h, "segment")
