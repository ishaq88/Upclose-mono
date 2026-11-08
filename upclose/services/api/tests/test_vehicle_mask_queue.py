"""Durable T13 mask predecessor checks on disposable PostgreSQL; synthetic rows only."""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update

from app.contracts.enums import PART_ZONES, PartState, SessionState
from app.jobs.queue import Failure, JobQueue
from app.models import Artifact, ConceptRun, Coverage, Job, Observation, Part, PipelineRun, Session
from app.pipeline.orchestration import (VEHICLE_MASK, create_assessment, masked_orchestration_context,
                                        release_masked_segments)
from app.pipeline.media import FrameManifest
from app.pipeline.reconcile import PipelineReconciler
from app.settings import Settings
from tests.test_jobs import factory, isolate, owner  # noqa: F401

H = "a" * 64
CTX = masked_orchestration_context()
MANIFEST = FrameManifest(H, 16, 16, 1, 10, tuple(range(8)))
_OWNED_SESSIONS = set()


@pytest.fixture(autouse=True)
def cleanup_owned_jobs(factory):
    # The additive migration intentionally refuses downgrade while mask jobs remain.
    # Remove only jobs seeded by this module so migration checks can run after it.
    # Sessions are moved out of live states first so a later reconciler sweep
    # cannot rebuild the deleted mask jobs from durable state.
    _OWNED_SESSIONS.clear()
    yield
    with factory.begin() as db:
        if _OWNED_SESSIONS:
            db.execute(update(Session).where(Session.id.in_(_OWNED_SESSIONS)).values(state=SessionState.DELETING))
            db.execute(delete(Job).where(Job.session_id.in_(_OWNED_SESSIONS)))
    _OWNED_SESSIONS.clear()


def assessment(factory, part_no: int = 1):
    session_id = owner(factory)
    _OWNED_SESSIONS.add(session_id)
    with factory.begin() as db:
        part = Part(session_id=session_id, part_no=part_no, zone=PART_ZONES[part_no - 1],
                    state=PartState.PROCESSING, quality={"usable_for_scoring": True})
        db.add(part)
        db.flush()
        ingest = PipelineRun(session_id=session_id, part_id=part.id, subject_key=f"ingest:{part.id}",
                             input_hash=H, config_hash=H, config_hashes={"media": H}, state="running")
        db.add(ingest)
        db.flush()
        prefix = f"media/{session_id}/{part.id}/{ingest.id}/a1"
        db.add_all([Artifact(session_id=session_id, kind="proxy", key=prefix + "/proxy.mp4", sha256=H),
                    Artifact(session_id=session_id, kind="derivative", key=prefix + "/manifest.json", sha256=MANIFEST.content_hash)])
        db.add_all(Artifact(session_id=session_id, kind="frame", key=f"{prefix}/frame_{i:06d}.jpg", sha256=H)
                   for i in range(MANIFEST.frame_count))
        db.flush()
        run_id = create_assessment(db, CTX, part.id, ingest.id)
        return session_id, part.id, run_id


def rows(factory, model, session_id, *where):
    with factory() as db:
        return list(db.scalars(select(model).where(model.session_id == session_id, *where)))


def reconcile(factory, session_id):
    return PipelineReconciler(factory, Settings(), CTX).reconcile_session(session_id)


@pytest.mark.parametrize("part_no", [1, 2, 3, 4, 6, 7])
def test_all_enabled_zones_wait_for_mask_publication(factory, part_no):
    session_id, _, run_id = assessment(factory, part_no)
    jobs = rows(factory, Job, session_id)
    assert [(job.stage, job.subject_kind, job.subject_id) for job in jobs] == [("vehicle_mask", "part_run", str(run_id))]
    assert {row.state for row in rows(factory, ConceptRun, session_id)} == {"pending"}
    assert JobQueue(factory).claim("damage-only", stages=("segment",)) is None
    reconcile(factory, session_id)
    assert not rows(factory, Job, session_id, Job.stage == "segment")


@pytest.mark.parametrize("part_no", [5, 8])
def test_no_concept_parts_drain_without_mask_request(factory, part_no):
    session_id, part_id, _ = assessment(factory, part_no)
    # No mask request either way; the dashboard (part 5) alone enqueues its read_dash job.
    assert [job.stage for job in rows(factory, Job, session_id)] == (["read_dash"] if part_no == 5 else [])
    # Part 8 drains immediately; part 5 stays processing until its read_dash job publishes.
    assert rows(factory, Part, session_id, Part.id == part_id)[0].state == (PartState.PROCESSING if part_no == 5 else PartState.PROCESSED)


def test_missing_mask_job_rebuilt_once_without_damage_bypass(factory):
    session_id, _, _ = assessment(factory)
    with factory.begin() as db:
        db.execute(delete(Job).where(Job.session_id == session_id))
    assert reconcile(factory, session_id)["vehicle_mask_jobs_rebuilt"] == 1
    assert reconcile(factory, session_id)["vehicle_mask_jobs_rebuilt"] == 0
    assert [job.stage for job in rows(factory, Job, session_id)] == ["vehicle_mask"]


@pytest.mark.parametrize("state,reason", [("dead", "VEHICLE_MASK_JOB_DEAD"),
                                          ("succeeded", "VEHICLE_MASK_OUTPUT_MISSING")])
def test_terminal_mask_without_valid_output_ends_dependents_unavailable(factory, state, reason):
    session_id, part_id, _ = assessment(factory)
    with factory.begin() as db:
        db.execute(update(Job).where(Job.session_id == session_id).values(state=state))
    assert reconcile(factory, session_id)["vehicle_masks_failed"] == 1
    assert {row.state for row in rows(factory, ConceptRun, session_id)} == {"failed"}
    assert {row.reason for row in rows(factory, ConceptRun, session_id)} == {reason}
    assert rows(factory, Part, session_id, Part.id == part_id)[0].state == PartState.PROCESSED
    assert all(row.state.value != "assessed" for row in rows(factory, Coverage, session_id))
    assert not rows(factory, Job, session_id, Job.stage == "segment")
    assert reconcile(factory, session_id)["vehicle_masks_failed"] == 0


def test_identity_only_marker_cannot_release_damage(factory):
    session_id, _, run_id = assessment(factory)
    with factory.begin() as db:
        run = db.get(PipelineRun, run_id)
        db.add(Observation(session_id=session_id, run_id=run_id, kind=VEHICLE_MASK,
                           value={"state": "ready", "version": "vehicle-mask-v1",
                                  "input_hash": run.input_hash, "config_hash": run.config_hash}))
        db.flush()
        assert not release_masked_segments(db, CTX, run)
    reconcile(factory, session_id)
    assert not rows(factory, Job, session_id, Job.stage == "segment")


def test_mask_retry_and_lease_recovery_keep_damage_withheld(factory):
    session_id, _, _ = assessment(factory)
    queue = JobQueue(factory, jitter=lambda: 0)
    first = queue.claim("mask-first", stages=("vehicle_mask",))
    assert first and queue.fail(first, Failure("PROVIDER_TIMEOUT"))
    reconcile(factory, session_id)
    assert not rows(factory, Job, session_id, Job.stage == "segment")
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == first.job_id).values(available_at=func.clock_timestamp() - timedelta(seconds=1)))
    second = queue.claim("mask-second", stages=("vehicle_mask",))
    assert second and second.attempt == 2
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == second.job_id).values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))
    third = queue.claim("mask-restart", stages=("vehicle_mask",))
    assert third and third.attempt == 3 and third.token != second.token
    assert not queue.complete(second, lambda db: pytest.fail("stale publisher must not execute"))
    assert not rows(factory, Job, session_id, Job.stage == "segment")
    assert queue.fail(third, Failure("UNSUPPORTED", retryable=False))
    reconcile(factory, session_id)
    assert {row.state for row in rows(factory, ConceptRun, session_id)} == {"failed"}


def test_mask_publication_after_session_deletion_is_fenced(factory):
    session_id, _, _ = assessment(factory)
    queue = JobQueue(factory)
    lease = queue.claim("mask-delete", stages=("vehicle_mask",))
    assert lease
    with factory.begin() as db:
        db.execute(update(Session).where(Session.id == session_id).values(state=SessionState.DELETING))
    assert not queue.complete(lease, lambda db: pytest.fail("deleted publisher must not execute"))
    assert not rows(factory, Job, session_id, Job.stage == "segment")



def put_marker(db, run):
    """Persist synthetic artifact bindings exactly as a fenced stage publisher does."""
    facts = db.scalar(select(Observation).where(Observation.run_id == run.id,
                                                Observation.kind == "assess_input")).value
    part = db.get(Part, run.part_id)
    is_video = CTX.config.prompts.part(part.part_no).media == "video"
    sources = ([{"key": facts["proxy_key"], "sha256": facts["proxy_sha256"],
                 "indices": list(range(MANIFEST.frame_count))}] if is_video else
               [{**row, "indices": [row["frame_index"]]} for row in facts["frames"]])
    media = []
    for index, source in enumerate(sources):
        prefix = f"media/{run.session_id}/{run.part_id}/{run.id}/vehicle/a1/{index}"
        masked_key, mask_key = prefix + ".mp4", prefix + ".json"
        db.add_all([Artifact(session_id=run.session_id, kind="proxy" if is_video else "frame",
                             key=masked_key, sha256="b" * 64),
                    Artifact(session_id=run.session_id, kind="mask", key=mask_key, sha256="c" * 64)])
        media.append({"kind": "video" if is_video else "image", "source_key": source["key"],
                      "source_sha256": source["sha256"], "masked_key": masked_key,
                      "masked_sha256": "b" * 64, "mask_key": mask_key, "mask_sha256": "c" * 64,
                      "request_id": str(uuid4()), "provenance": "synthetic",
                      "frame_indices": source["indices"],
                      "timestamps_ms": [round(MANIFEST.time_seconds(i) * 1000) for i in source["indices"]]})
    marker = Observation(session_id=run.session_id, run_id=run.id, kind=VEHICLE_MASK,
                         value={"version": "vehicle-mask-v1", "state": "ready", "input_hash": run.input_hash,
                                "config_hash": run.config_hash, "source_manifest_sha256": MANIFEST.content_hash,
                                "source_manifest": json.loads(MANIFEST.to_canonical_json()), "media": media})
    db.add(marker)
    db.flush()
    return marker


@pytest.mark.parametrize("part_no", [1, 2, 3, 4, 6, 7])
def test_atomic_mask_commit_releases_all_and_only_pending_damage(factory, part_no):
    session_id, _, run_id = assessment(factory, part_no)
    queue = JobQueue(factory)
    lease = queue.claim("mask-publisher", stages=("vehicle_mask",))
    assert lease

    def publish(db):
        run = db.get(PipelineRun, run_id)
        put_marker(db, run)
        assert release_masked_segments(db, CTX, run, lease.job_id)
        # Other DB connections cannot observe either the marker or fan-out until commit.
        assert not rows(factory, Job, session_id, Job.stage == "segment")
        assert not rows(factory, Observation, session_id, Observation.kind == VEHICLE_MASK)

    assert queue.complete(lease, publish)
    segment = rows(factory, Job, session_id, Job.stage == "segment")
    assert {job.subject_id for job in segment} == {str(row.id) for row in rows(factory, ConceptRun, session_id)}
    assert {job.state for job in segment} == {"queued"}
    reconcile(factory, session_id)
    assert len(rows(factory, Job, session_id, Job.stage == "segment")) == len(segment)
    with factory.begin() as db:
        db.execute(delete(Job).where(Job.id == segment[0].id))
    reconcile(factory, session_id)
    assert len(rows(factory, Job, session_id, Job.stage == "segment")) == len(segment)


@pytest.mark.parametrize("corruption", ["source_hash", "masked_hash", "mask_hash", "timestamp", "frame_index",
                                         "missing_artifact", "foreign_prefix", "duplicate_marker", "manifest",
                                         "purged_artifact", "assessment_source_hash"])
def test_corrupt_complete_marker_never_releases_damage(factory, corruption):
    session_id, _, run_id = assessment(factory)
    with factory.begin() as db:
        run = db.get(PipelineRun, run_id)
        marker = put_marker(db, run)
        value = json.loads(json.dumps(marker.value))
        item = value["media"][0]
        if corruption == "source_hash":
            item["source_sha256"] = "d" * 64
        elif corruption == "masked_hash":
            item["masked_sha256"] = "d" * 64
        elif corruption == "mask_hash":
            item["mask_sha256"] = "d" * 64
        elif corruption == "timestamp":
            item["timestamps_ms"][0] += 1
        elif corruption == "frame_index":
            item["frame_indices"][0] = 1
        elif corruption == "missing_artifact":
            db.execute(delete(Artifact).where(Artifact.key == item["masked_key"]))
        elif corruption == "purged_artifact":
            db.execute(update(Artifact).where(Artifact.key == item["masked_key"]).values(purge_state="purged"))
        elif corruption == "assessment_source_hash":
            facts_row = db.scalar(select(Observation).where(Observation.run_id == run_id,
                                                            Observation.kind == "assess_input"))
            changed = FrameManifest("d" * 64, 16, 16, 1, 10, tuple(range(8)))
            facts_row.value = {**facts_row.value, "proxy_sha256": changed.proxy_sha256,
                               "manifest_sha256": changed.content_hash}
            value["source_manifest"] = json.loads(changed.to_canonical_json())
            value["source_manifest_sha256"] = changed.content_hash
            item["source_sha256"] = changed.proxy_sha256
            db.execute(update(Artifact).where(Artifact.key == facts_row.value["proxy_key"]).values(
                sha256=changed.proxy_sha256))
            db.execute(update(Artifact).where(Artifact.key == facts_row.value["manifest_key"]).values(
                sha256=changed.content_hash))
        elif corruption == "foreign_prefix":
            item["masked_key"] = f"media/{uuid4()}/masked.mp4"
        elif corruption == "duplicate_marker":
            db.add(Observation(session_id=session_id, run_id=run_id, kind=VEHICLE_MASK, value=value))
        else:
            value["source_manifest"]["pts"][0] = -1
        marker.value = value
        db.flush()
        assert not release_masked_segments(db, CTX, run)
    reconcile(factory, session_id)
    assert not rows(factory, Job, session_id, Job.stage == "segment")


def test_lease_expiry_during_publication_rolls_back_marker_artifacts_and_fanout(factory):
    session_id, _, run_id = assessment(factory)
    queue = JobQueue(factory)
    lease = queue.claim("mask-expiring", stages=("vehicle_mask",))
    assert lease
    before = len(rows(factory, Artifact, session_id))

    def publish(db):
        run = db.get(PipelineRun, run_id)
        put_marker(db, run)
        assert release_masked_segments(db, CTX, run, lease.job_id)
        db.execute(update(Job).where(Job.id == lease.job_id).values(
            lease_until=func.clock_timestamp() - timedelta(seconds=1)))

    assert not queue.complete(lease, publish)
    assert not rows(factory, Job, session_id, Job.stage == "segment")
    assert not rows(factory, Observation, session_id, Observation.kind == VEHICLE_MASK)
    assert len(rows(factory, Artifact, session_id)) == before
