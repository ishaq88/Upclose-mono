"""T05b stage orchestration on real PostgreSQL, Floci and ffmpeg (synthetic media; fixture providers only).

Uploads go through the real API routes and every job is driven through the real queue claim path.
"""
from __future__ import annotations

import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.adapters.meta import ProviderFailure
from app.contracts.enums import (ConceptRunState, CoverageState, FindingStatus, PartState, ReportState, SessionState)
from app.domain.rubrics import build_rubric_registry, load_rubric_config
from app.jobs.queue import Failure, JobQueue
from app.jobs.worker import Publication, StageRegistry, run_once
from app.models import (Artifact, ConceptRun, Coverage, Finding, Job, Observation, Part, PipelineRun, Report,
                        ReportRevision, Session)
from app.pipeline import orchestration
from app.pipeline.ingest import IngestHandler
from app.pipeline.orchestration import (ASSESS_INPUT, DEDUP_RUN, OrchestrationContext, PipelineConfig,
                                        install_pipeline_handlers, load_pipeline_config)
from app.pipeline.reconcile import PipelineReconciler
from app.pipeline.segment import (ScriptedSegmenter, ScriptedTrack, default_segmenter_factory,
                                  register_segment_handler)
from app.pipeline.verify import FixtureVerifier, register_verify_handler
from tests import media_fixtures as fx
from tests.test_ingest import accept, artifacts_of, clip, media_dir, scratch_root, stored  # noqa: F401
from tests.test_uploads import Harness, bucket, make, session_factory  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")
STAGES = ("ingest", "segment", "verify")


@pytest.fixture(autouse=True)
def isolate(session_factory):
    """Other tests leave queued jobs in the shared test database; only ours may be claimed."""
    with session_factory.begin() as db:
        db.execute(update(Job).where(Job.state.in_(("queued", "running"))).values(
            state="dead", lease_until=None, lease_token=None, worker_id=None))


def track(*points: tuple[float, float, float, float, float, float | None], track_id: str = "1") -> tuple[ScriptedTrack, ...]:
    return (ScriptedTrack(track_id, tuple(points)),)


RUST_AND_DENT = {"rust_a": track((0.3, 0.10, 0.10, 0.20, 0.20, 0.9)), "dent": track((0.6, 0.50, 0.50, 0.20, 0.20, 0.9))}


class Pipe:
    """A worker registry with the real orchestration handlers and injectable fixture providers."""

    def __init__(self, h: Harness, *, script=None, failures=None, verifier=None, ctx=None, fanout: bool = True,
                 factory=None) -> None:
        self.h = h
        self.queue = JobQueue(h.db)
        self.registry = StageRegistry()
        self.ctx = ctx or OrchestrationContext()
        self.segmenter = factory or (lambda part_no: ScriptedSegmenter(script or {}, failures))
        if fanout:
            install_pipeline_handlers(self.registry, h.db, h.storage, h.settings, self.ctx,
                                      segmenter_factory=self.segmenter, verifier=verifier)
        else:  # ingest without the T05b fan-out: the reconciler must rebuild the assessment
            self.registry.register("ingest", IngestHandler(h.db, h.storage, h.settings, fanout=lambda outcome: Publication()))
            register_segment_handler(self.registry, h.db, h.storage, h.settings, self.ctx, segmenter_factory=self.segmenter)
            register_verify_handler(self.registry, h.db, self.ctx, verifier=verifier)
        self.reconciler = PipelineReconciler(h.db, h.settings, self.ctx)

    def step(self) -> bool:
        return run_once(self.queue, self.registry, "orch-test")

    def drain(self, limit: int = 200) -> int:
        """Run every available job; retry delays are collapsed so three-attempt paths finish quickly."""
        ran = 0
        for _ in range(limit):
            if self.step():
                ran += 1
                continue
            with self.h.db.begin() as db:
                bumped = db.execute(update(Job).where(Job.session_id == self.h.session_id, Job.state == "queued",
                                                      Job.available_at > func.clock_timestamp(),
                                                      Job.available_at < func.clock_timestamp() + timedelta(minutes=5))
                                    .values(available_at=func.clock_timestamp() - timedelta(seconds=1))).rowcount
            if not bumped:
                return ran
        raise AssertionError("queue did not drain")

    def reconcile(self) -> Counter[str]:
        return self.reconciler.reconcile_session(self.h.session_id)


def upload(h: Harness, media_dir: Path, *part_nos: int, name: str = "clip.mp4") -> None:
    path = clip(media_dir, name)
    for part_no in part_nos:
        accept(h, path, part_no=part_no)


def setup(make, scratch_root, **settings) -> Harness:
    return make(ingest_scratch_root=str(scratch_root), **settings)


def rows(h: Harness, model, *where, order=None) -> list:
    with h.db() as db:
        query = select(model).where(model.session_id == h.session_id, *where)
        return list(db.scalars(query.order_by(order) if order is not None else query))


def part(h: Harness, part_no: int) -> Part:
    return rows(h, Part, Part.part_no == part_no)[0]


def jobs(h: Harness, stage: str | None = None, state: str | None = None) -> list[Job]:
    clauses = [] if stage is None else [Job.stage == stage]
    if state is not None:
        clauses.append(Job.state == state)
    return rows(h, Job, *clauses, order=Job.created_at)


def coverage(h: Harness, part_no: int = 1) -> dict[str, Coverage]:
    zone = part(h, part_no).zone
    return {row.concept.value: row for row in rows(h, Coverage, Coverage.zone == zone)}


def concept_runs(h: Harness) -> dict[str, ConceptRun]:
    return {row.prompt_id: row for row in rows(h, ConceptRun)}


def findings(h: Harness) -> list[Finding]:
    return rows(h, Finding, order=Finding.id)


def kill(h: Harness, job_id: UUID) -> None:
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(state="dead", last_error="ATTEMPTS_EXHAUSTED",
                                                              lease_token=None, lease_until=None, worker_id=None))


def finalize(h: Harness) -> str:
    response = h.http.post(f"/api/v1/sessions/{h.session_id}/finalize", headers=h.headers, json={})
    assert response.status_code == 202, response.text
    return response.json()["report_slug"]


def fingerprint(h: Harness) -> tuple:
    with h.db() as db:
        counts = [db.scalar(select(func.count()).select_from(model).where(model.session_id == h.session_id))
                  for model in (Job, ConceptRun, Finding, Coverage, Observation, Artifact, PipelineRun)]
        states = (sorted((p.part_no, p.state.value) for p in rows(h, Part)), sorted(c.state for c in rows(h, ConceptRun)),
                  [r.state.value for r in rows(h, ReportRevision)], sorted((j.stage, j.state) for j in rows(h, Job)))
    return tuple(counts), states


# --------------------------------------------------------------------------- fan-out


def test_ingest_fans_out_one_concept_run_and_segment_job_per_prompt(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h)
    upload(h, media_dir, 1)
    assert pipe.step() is True  # ingest only: it is the sole claimable job
    assert part(h, 1).state == PartState.PROCESSING
    runs = concept_runs(h)
    assert sorted(runs) == ["dent", "paint_bubbling", "rust_a", "rust_b", "scratch"]
    assert {row.state for row in runs.values()} == {"pending"}
    segment = jobs(h, "segment")
    assert sorted(j.subject_id for j in segment) == sorted(str(r.id) for r in runs.values())
    assert {j.subject_kind for j in segment} == {"concept_run"} and {j.state for j in segment} == {"queued"}
    assess = [r for r in rows(h, PipelineRun) if r.subject_key.startswith("assess:")]
    assert len(assess) == 1 and assess[0].state == "running"
    assert set(assess[0].config_hashes) == {"prompts", "rubrics", "scoring", "dedup", "segmenter", "verifier", "media"}
    assert assess[0].config_hashes == {**pipe.ctx.config_hashes, "media": assess[0].config_hashes["media"]}
    assert len(rows(h, Observation, Observation.kind == ASSESS_INPUT)) == 1


@pytest.mark.parametrize("part_no", [5, 8])
def test_parts_without_concepts_go_straight_to_processed_and_not_assessed(make, media_dir, scratch_root, part_no) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h)
    upload(h, media_dir, part_no)
    assert pipe.step() is True
    assert part(h, part_no).state == PartState.PROCESSED
    assert rows(h, ConceptRun) == [] and jobs(h, "segment") == [] and jobs(h, "verify") == []
    assert coverage(h, part_no) == {}  # no concept to attach: scoring reports the zone not_assessed, never clean
    assert pipe.step() is False


def test_rubric_disabled_concept_is_recorded_with_its_reason_and_never_run(make, media_dir, scratch_root) -> None:
    rubric_config = load_rubric_config(Path(orchestration.CONFIG_DIR) / "rubrics.v1.json")
    base = load_pipeline_config()
    reduced = replace(base, rubrics=build_rubric_registry(rubric_config.model_copy(update={
        "concepts": tuple(c for c in rubric_config.concepts if c.concept.value != "scratch")})))
    h = setup(make, scratch_root)
    pipe = Pipe(h, ctx=OrchestrationContext(config=reduced))
    upload(h, media_dir, 1)
    pipe.drain()
    runs = concept_runs(h)
    assert runs["scratch"].state == "unsupported" and runs["scratch"].reason.startswith("rubric_unsupported")
    assert all(runs[p].state == "succeeded" for p in ("rust_a", "rust_b", "paint_bubbling", "dent"))
    assert [j for j in jobs(h, "segment") if j.subject_id == str(runs["scratch"].id)] == []
    cov = coverage(h)
    assert cov["scratch"].state == CoverageState.NOT_ASSESSED and "rubric_unsupported" in cov["scratch"].reason
    assert cov["rust"].state == CoverageState.ASSESSED
    assert part(h, 1).state == PartState.PROCESSED


# --------------------------------------------------------------------------- segment, dedup, verify


def test_default_fixture_pipeline_dedups_across_prompts_and_persists_provenance(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, factory=default_segmenter_factory)
    upload(h, media_dir, 1)
    pipe.drain()
    assert part(h, 1).state == PartState.PROCESSED
    assert {r.state for r in concept_runs(h).values()} == {"succeeded"}
    found = findings(h)
    assert len(found) == 2 and {f.concept.value for f in found} == {"rust"}
    survivor, duplicate = found[0], found[1]  # stable lowest ID wins
    assert survivor.status == FindingStatus.VERIFIED and survivor.severity == 2 and survivor.duplicate_of is None
    assert duplicate.status == FindingStatus.REJECTED and duplicate.duplicate_of == survivor.id and duplicate.severity is None
    assert "dedup_merged" in duplicate.flags
    marker, = rows(h, Observation, Observation.kind == DEDUP_RUN)
    merge, = marker.value["merges"]
    assert (merge["survivor_id"], merge["duplicate_id"], merge["reason"]) == (str(survivor.id), str(duplicate.id), "iou")
    assert marker.value["config_hash"] == pipe.ctx.config.prompts.dedup.content_hash and marker.value["ambiguous"] == []
    verify = jobs(h, "verify")
    assert [(j.subject_kind, j.subject_id, j.state) for j in verify] == [("finding", str(survivor.id), "succeeded")]
    prov, = rows(h, Observation, Observation.kind == orchestration.VERIFICATION_PROVENANCE)
    assert prov.value["provenance"]["rubric_id"].endswith(":rust") and prov.value["confidence_label"] == "uncalibrated"
    cov = coverage(h)
    assert {c.state for c in cov.values()} == {CoverageState.ASSESSED} and set(cov) == {"rust", "paint_bubbling", "dent", "scratch"}
    ingest_run = next(r for r in rows(h, PipelineRun) if r.subject_key.startswith("ingest:"))
    assert ingest_run.state == "succeeded"


def test_cited_frames_are_extracted_lazily_under_an_attempt_scoped_prefix_and_linked(make, media_dir, scratch_root) -> None:
    import hashlib
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={"rust_a": track((0.3, 0.1, 0.1, 0.2, 0.2, 0.9)), "dent": track((0.6, 0.5, 0.5, 0.2, 0.2, 0.9))})
    upload(h, media_dir, 1)
    assert pipe.step() is True
    assert [a.kind for a in artifacts_of(h) if a.kind == "frame"] == []  # nothing extracted at ingest
    pipe.drain()
    frames = [a for a in artifacts_of(h) if a.kind == "frame"]
    assert len(frames) == 2  # exactly the cited frames, one per candidate
    run = next(r for r in rows(h, PipelineRun) if r.subject_key.startswith("assess:"))
    for a in frames:
        assert a.key.startswith(f"media/{h.session_id}/{part(h, 1).id}/{run.id}/seg/") and "/a1/frames/frame_" in a.key
        assert hashlib.sha256(stored(h, a.key)).hexdigest() == a.sha256 and a.privacy_state.value == "pending"
    assert {f.frame_key for f in findings(h)} == {a.key for a in frames}
    assert all(f.status == FindingStatus.VERIFIED for f in findings(h))


def test_zero_candidates_does_not_stall_the_part(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={})
    upload(h, media_dir, 1)
    pipe.drain()
    assert part(h, 1).state == PartState.PROCESSED
    assert {r.state for r in concept_runs(h).values()} == {"succeeded"} and findings(h) == [] and jobs(h, "verify") == []
    assert {c.state for c in coverage(h).values()} == {CoverageState.ASSESSED}  # successful empty detection, never "damage free"
    assert rows(h, Observation, Observation.kind == DEDUP_RUN)[0].value["candidate_ids"] == []


def test_one_failed_concept_leaves_others_usable_with_partial_coverage(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT, failures={"dent": "provider_failed", "scratch": "price_unverified"})
    upload(h, media_dir, 1)
    pipe.drain()
    runs = concept_runs(h)
    assert (runs["dent"].state, runs["dent"].reason) == ("failed", "SEGMENT_PROVIDER_FAILED")
    assert (runs["scratch"].state, runs["scratch"].reason) == ("unsupported", "SEGMENT_PRICE_UNVERIFIED")
    assert runs["rust_a"].state == runs["rust_b"].state == runs["paint_bubbling"].state == "succeeded"
    attempts = {j.subject_id: j.attempts for j in jobs(h, "segment")}
    assert attempts[str(runs["dent"].id)] == 3 and attempts[str(runs["scratch"].id)] == 1  # budget vs terminal
    assert part(h, 1).state == PartState.PROCESSED
    cov = coverage(h)
    assert cov["rust"].state == cov["paint_bubbling"].state == CoverageState.ASSESSED
    assert cov["dent"].state == CoverageState.PARTIAL and "concept_failed" in cov["dent"].reason
    assert cov["scratch"].state == CoverageState.NOT_ASSESSED
    verified = [f for f in findings(h) if f.status == FindingStatus.VERIFIED]
    assert [f.concept.value for f in verified] == ["rust"]  # rust evidence is still usable


def test_malformed_verdict_uses_the_attempt_budget_then_leaves_the_finding_unverified(make, media_dir, scratch_root) -> None:
    bad = lambda c, ctx, r: {"finding_id": str(c.id), "verdict": "confirmed", "concept": "rust", "severity": 9,
                             "evidence_note": "x"}
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT, verifier=FixtureVerifier({"rust": bad}))
    upload(h, media_dir, 1)
    pipe.drain()
    by_concept = {f.concept.value: f for f in findings(h)}
    assert by_concept["rust"].status == FindingStatus.UNVERIFIED and by_concept["rust"].severity is None
    assert by_concept["dent"].status == FindingStatus.VERIFIED
    assert sorted(j.attempts for j in jobs(h, "verify")) == [1, 3]
    assert part(h, 1).state == PartState.PROCESSED
    cov = coverage(h)
    assert cov["rust"].state == CoverageState.PARTIAL and "unresolved_candidates" in cov["rust"].reason
    assert cov["dent"].state == CoverageState.ASSESSED
    failure, = rows(h, Observation, Observation.kind == orchestration.VERIFICATION_FAILED)
    assert failure.value["reason"] == "VERIFY_VERDICT_INVALID"


def test_unknown_verdict_fields_are_rejected_before_any_write(make, media_dir, scratch_root) -> None:
    extra = lambda c, ctx, r: {"finding_id": str(c.id), "verdict": "artifact", "concept": "rust", "evidence_note": "x",
                               "run_this": "rm -rf /"}
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={"rust_a": track((0.3, 0.1, 0.1, 0.2, 0.2, 0.9))}, verifier=FixtureVerifier({"rust": extra}))
    upload(h, media_dir, 1)
    pipe.drain()
    assert [f.status for f in findings(h)] == [FindingStatus.UNVERIFIED]


def test_quality_unusable_part_still_runs_concepts_but_coverage_is_partial(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={"rust_a": track((0.3, 0.1, 0.1, 0.2, 0.2, 0.9))})
    upload(h, media_dir, 1)
    assert pipe.step() is True
    with h.db.begin() as db:
        p = db.scalar(select(Part).where(Part.session_id == h.session_id, Part.part_no == 1))
        p.quality = {**p.quality, "usable_for_scoring": False}
    pipe.drain()
    assert {r.state for r in concept_runs(h).values()} == {"succeeded"}
    assert [f.status for f in findings(h)] == [FindingStatus.VERIFIED]  # evidence stays visible
    assert {c.state for c in coverage(h).values()} == {CoverageState.PARTIAL}
    assert all("capture_quality_unusable" in c.reason for c in coverage(h).values())


def overlap_script() -> dict:
    return {"rust_a": track((0.3, 0.1, 0.1, 0.4, 0.4, 0.9)), "rust_b": track((0.3, 0.3, 0.1, 0.4, 0.4, 0.8))}


def test_ambiguous_overlap_is_verified_as_a_group_and_stays_partial_when_unresolved(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=overlap_script())
    upload(h, media_dir, 1)
    pipe.drain()
    verify = jobs(h, "verify")
    assert [(j.subject_kind, j.state) for j in verify] == [("duplicate_group", "succeeded")]
    group, = rows(h, Observation, Observation.kind == orchestration.VERIFY_GROUP)
    assert verify[0].subject_id == str(group.id) and len(group.value["finding_ids"]) == 2
    assert [f.status for f in findings(h)] == [FindingStatus.VERIFIED] * 2  # both retained, nothing merged
    cov = coverage(h)
    assert cov["rust"].state == CoverageState.PARTIAL and "duplicate_review_unresolved" in cov["rust"].reason


def test_verifier_duplicate_link_in_a_group_resolves_the_overlap(make, media_dir, scratch_root) -> None:
    def link(candidate, context, rubric):
        first = min(item.id for item in context)
        verdict = {"finding_id": str(candidate.id), "verdict": "confirmed", "concept": "rust", "severity": 3,
                   "evidence_note": "x", "duplicate_of": None}
        if candidate.id != first:
            verdict.update(verdict="duplicate", severity=None, duplicate_of=str(first))
        return verdict
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=overlap_script(), verifier=FixtureVerifier({"rust": link}))
    upload(h, media_dir, 1)
    pipe.drain()
    first, second = findings(h)
    assert (first.status, second.status, second.duplicate_of) == (FindingStatus.VERIFIED, FindingStatus.REJECTED, first.id)
    assert coverage(h)["rust"].state == CoverageState.ASSESSED


def test_stills_part_segments_each_stored_still_without_extracting_new_frames(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={"tire_wear": track((0.0, 0.1, 0.1, 0.2, 0.2, 0.9))})
    upload(h, media_dir, 4)
    pipe.drain()
    stills = [a for a in artifacts_of(h) if a.kind == "frame"]
    assert len(stills) == 8 and not any("/seg/" in a.key for a in stills)  # reused, not re-extracted
    found = findings(h)
    assert len(found) == 8 and {f.frame_key for f in found} == {a.key for a in stills}
    assert all(f.track_id.startswith("s") and f.track_id.endswith("-1") for f in found)
    assert part(h, 4).state == PartState.PROCESSED and coverage(h, 4)["tire_wear"].state == CoverageState.ASSESSED


# --------------------------------------------------------------------------- fencing


def claim_stage(pipe: Pipe, stage: str):
    lease = pipe.queue.claim("orch-test", [stage])
    assert lease is not None
    return lease


def test_deleting_session_publishes_nothing(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1)
    assert pipe.step() is True
    lease = claim_stage(pipe, "segment")
    publication = pipe.registry.handler("segment")(lease)
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.DELETING))
    assert pipe.queue.complete(lease, publication.publish, publication.downstream) is False
    assert findings(h) == [] and [a for a in artifacts_of(h) if a.kind == "frame"] == []
    assert {r.state for r in concept_runs(h).values()} == {"pending"}


def test_stale_worker_cannot_publish_after_losing_its_lease(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1)
    assert pipe.step() is True
    lease = claim_stage(pipe, "segment")
    publication = pipe.registry.handler("segment")(lease)
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == lease.job_id).values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))
    assert pipe.queue.complete(lease, publication.publish, publication.downstream) is False
    assert findings(h) == [] and {r.state for r in concept_runs(h).values()} == {"pending"}


# --------------------------------------------------------------------------- barriers


def test_all_terminal_parts_after_finalize_enqueue_exactly_one_report_chain(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1, 2)
    finalize(h)  # processing is still allowed; every other slot is skipped
    assert jobs(h, "claims") == []
    pipe.drain()
    assert [part(h, n).state for n in (1, 2)] == [PartState.PROCESSED] * 2
    assert {part(h, n).state for n in range(3, 9)} == {PartState.SKIPPED}
    claims = jobs(h, "claims")
    revision, = rows(h, ReportRevision)
    assert len(claims) == 1 and (claims[0].subject_kind, claims[0].subject_id, claims[0].state) == (
        "report_revision", str(revision.id), "queued")
    assert [j.stage for j in jobs(h) if j.stage in ("score", "narrate", "render")] == []  # later stages belong to T07b/T08a
    pipe.reconcile()
    pipe.reconcile()
    pipe.drain()
    assert len(jobs(h, "claims")) == 1 and revision.state == ReportState.BUILDING


def test_barrier_waits_for_a_part_that_is_not_terminal(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1, 2)
    finalize(h)
    held = next(j for j in jobs(h, "ingest") if j.subject_id == str(next(
        r.id for r in rows(h, PipelineRun) if r.part_id == part(h, 2).id)))
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == held.id).values(available_at=func.clock_timestamp() + timedelta(hours=1)))
    pipe.drain()
    assert part(h, 1).state == PartState.PROCESSED and part(h, 2).state == PartState.UPLOADED
    assert jobs(h, "claims") == []
    with h.db.begin() as db:
        db.execute(update(Job).where(Job.id == held.id).values(available_at=func.clock_timestamp() - timedelta(seconds=1)))
    pipe.drain()
    assert part(h, 2).state == PartState.PROCESSED and len(jobs(h, "claims")) == 1


def test_finalize_after_everything_finished_schedules_the_chain_in_the_finalize_transaction(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1)
    pipe.drain()
    assert jobs(h, "claims") == []  # Part 1 finishing alone never finalizes or builds the report
    assert rows(h, Report) == [] and rows(h, ReportRevision) == []
    finalize(h)
    assert len(jobs(h, "claims")) == 1


def test_failed_and_skipped_parts_are_terminal_for_the_barrier(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    accept(h, fx.make_garbage(media_dir / "bad.mp4").path, part_no=1)
    finalize(h)
    pipe.drain()
    assert part(h, 1).state == PartState.FAILED and part(h, 1).failure_reason.startswith("MEDIA_")
    assert len(jobs(h, "claims")) == 1  # ingest failure re-evaluates the barrier in its own publication


def test_concurrent_last_job_completions_converge_on_one_transition_and_one_chain(make, media_dir, scratch_root, monkeypatch) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1)
    finalize(h)
    while (lease := pipe.queue.claim("orch-test", ["ingest", "segment"])) is not None:
        publication = pipe.registry.handler(lease.stage)(lease)
        assert pipe.queue.complete(lease, publication.publish, publication.downstream)
    assert part(h, 1).state == PartState.PROCESSING and len(jobs(h, "verify", "queued")) == 2
    leases = [claim_stage(pipe, "verify"), claim_stage(pipe, "verify")]
    publications = [pipe.registry.handler("verify")(lease) for lease in leases]
    completions: list[str] = []
    original = orchestration._complete_part

    def counting(*args, **kwargs):
        completions.append("completed")
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_complete_part", counting)
    start = Barrier(3)

    def finish(index: int) -> bool:
        start.wait()
        return pipe.queue.complete(leases[index], publications[index].publish, publications[index].downstream)

    def reconcile() -> None:
        start.wait()
        pipe.reconcile()

    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(finish, 0), pool.submit(finish, 1), pool.submit(reconcile)]
        assert [f.result() for f in futures[:2]] == [True, True]
        futures[2].result()
    assert completions == ["completed"]
    assert part(h, 1).state == PartState.PROCESSED and len(rows(h, Coverage)) == 4
    assert len(jobs(h, "claims")) == 1
    assert {f.status for f in findings(h)} == {FindingStatus.VERIFIED}


# --------------------------------------------------------------------------- report-stage failure


def test_report_stage_failure_reaches_a_terminal_revision_state(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={})
    finalize(h)  # all slots skipped: the chain is scheduled at finalize
    claims, = jobs(h, "claims")
    lease = pipe.queue.claim("orch-test", ["claims"])
    assert lease is not None and lease.job_id == claims.id
    assert pipe.queue.fail(lease, Failure("INVALID_INPUT", retryable=False)) is True
    assert jobs(h, "claims")[0].state == "dead"
    changes = pipe.reconcile()
    assert changes["revisions_failed"] == 1
    revision, = rows(h, ReportRevision)
    assert revision.state == ReportState.FAILED and revision.manifest["failure"] == {"stage": "claims", "code": "INVALID_INPUT"}
    assert pipe.reconcile() == Counter()  # terminal: nothing more to do


# --------------------------------------------------------------------------- reconciler


def test_dead_ingest_job_fails_the_part_and_counts_as_terminal(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={})
    upload(h, media_dir, 1)
    finalize(h)
    ingest, = jobs(h, "ingest")
    kill(h, ingest.id)
    assert pipe.step() is False  # a dead job is never claimed
    assert part(h, 1).state == PartState.UPLOADED
    changes = pipe.reconcile()
    assert changes["parts_failed"] == 1
    assert (part(h, 1).state, part(h, 1).failure_reason) == (PartState.FAILED, "INGEST_JOB_DEAD")
    assert next(r for r in rows(h, PipelineRun) if r.part_id == part(h, 1).id).state == "failed"
    assert len(jobs(h, "claims")) == 1
    assert pipe.reconcile() == Counter()


def test_dead_and_missing_segment_jobs_are_repaired(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1)
    assert pipe.step() is True
    runs = concept_runs(h)
    by_subject = {j.subject_id: j for j in jobs(h, "segment")}
    kill(h, by_subject[str(runs["dent"].id)].id)
    with h.db.begin() as db:
        db.execute(Job.__table__.delete().where(Job.id == by_subject[str(runs["scratch"].id)].id))
    pipe.reconcile()  # dead -> concept run failed; missing -> job re-enqueued under the same identity
    runs = concept_runs(h)
    assert (runs["dent"].state, runs["dent"].reason) == ("failed", "SEGMENT_JOB_DEAD")
    assert len([j for j in jobs(h, "segment") if j.subject_id == str(runs["scratch"].id)]) == 1
    assert part(h, 1).state == PartState.PROCESSING
    pipe.drain()
    assert part(h, 1).state == PartState.PROCESSED
    cov = coverage(h)
    assert cov["dent"].state == CoverageState.PARTIAL and cov["scratch"].state == CoverageState.ASSESSED


def test_missing_assessment_is_rebuilt_from_ingest_artifacts(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT, fanout=False)
    upload(h, media_dir, 1)
    assert pipe.step() is True
    assert part(h, 1).state == PartState.PROCESSING and rows(h, ConceptRun) == []
    changes = pipe.reconcile()
    assert changes["assessments_rebuilt"] == 1 and len(jobs(h, "segment")) == 5
    assert pipe.reconcile()["assessments_rebuilt"] == 0
    pipe.drain()
    assert part(h, 1).state == PartState.PROCESSED
    assert {f.status for f in findings(h)} == {FindingStatus.VERIFIED}


def test_reconciler_is_a_noop_on_settled_state_and_sweep_is_bounded(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script=RUST_AND_DENT)
    upload(h, media_dir, 1)
    finalize(h)
    pipe.drain()
    before = fingerprint(h)
    for _ in range(3):
        assert pipe.reconcile() == Counter()
    assert fingerprint(h) == before
    result = pipe.reconciler.sweep(limit=5)  # may touch other tests' sessions; ours must stay unchanged
    assert result.sessions <= 5 and fingerprint(h) == before
