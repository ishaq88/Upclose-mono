"""Idempotent pipeline reconciler (T05b): rebuild downstream work from durable state, fail what can never
finish, re-evaluate the barriers.

One transaction per session, always `session -> part -> run -> children` lock order (the same order every
publication uses), so a reconciler pass and a worker publication can never deadlock or double-apply. Every
rule is a pure function of committed rows; a second pass over unchanged state changes nothing.

Rules, per live (capturing/finalized) session:

- part `uploaded` whose `ingest` job is `dead`: the part FAILS (`INGEST_JOB_DEAD`); the queue's deadline
  margin can dead-letter a job before the handler's own last-attempt path runs, and nothing else would
  ever move the part. No part waits forever in `uploaded`.
- part `processing` without an assessment run (fan-out lost or predating this card): rebuild it from the
  ingest artifacts (`create_assessment`), or fail the part if those artifacts are missing/ambiguous.
- mask-required assessment: validate the complete committed marker before any damage fan-out; rebuild
  missing mask work first, and terminal/dead work without a valid marker fails dependent concepts.
- concept run still `pending` whose `segment` job is missing: enqueue it (idempotent key); whose job is
  `dead`: the concept run FAILS (`SEGMENT_JOB_DEAD`) so coverage becomes partial, not a spinner.
- part barrier (dedup, verify scheduling, leftover candidates -> `unverified`, coverage, `processed`) and
  full-report barrier via `advance_part` / `advance_session`.
- finalized session whose building revision has a `dead` report-stage job: the revision FAILS.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.enums import PartState, ReportState, SessionState
from app.jobs.queue import JobSpec, enqueue
from app.models import ConceptRun, Job, Part, Report, ReportRevision, Session
from app.pipeline.orchestration import (INGEST_JOB_DEAD, SEGMENT_JOB_DEAD, OrchestrationContext, advance_part,
                                        advance_session, assessment_run, create_assessment, ingest_run,
                                        vehicle_mask_required, enqueue_vehicle_mask, release_masked_segments, fail_vehicle_mask,
                                        masked_orchestration_context)
from app.settings import Settings

_LIVE = (SessionState.CAPTURING, SessionState.FINALIZED)
ASSESSMENT_INPUT_MISSING = "ASSESSMENT_INPUT_MISSING"
REPORT_STAGES = ("claims", "score", "narrate", "render")


@dataclass(slots=True)
class ReconcileResult:
    sessions: int = 0
    changes: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.changes.values())


class PipelineReconciler:
    def __init__(self, factory: sessionmaker[DBSession], settings: Settings, ctx: OrchestrationContext | None = None) -> None:
        self.factory, self.settings = factory, settings
        self.ctx = ctx or OrchestrationContext()
        self._cursor: UUID | None = None  # round-robin so long-lived waiting sessions cannot starve others

    # ---- bounded sweep (worker loop)

    def sweep(self, limit: int | None = None) -> ReconcileResult:
        limit = limit or self.settings.pipeline_reconcile_batch
        result = ReconcileResult()
        for session_id in self._candidates(limit):
            try:
                result.changes.update(self.reconcile_session(session_id))
                result.sessions += 1
            except Exception:  # one bad session must not stop the sweep; retried next interval
                logging.exception("Pipeline reconciliation failed for one session")
        return result

    def _candidates(self, limit: int) -> list[UUID]:
        open_parts = exists(select(Part.id).where(Part.session_id == Session.id,
                                                  Part.state.in_((PartState.UPLOADED, PartState.PROCESSING))))
        building = and_(Session.state == SessionState.FINALIZED, exists(select(ReportRevision.id).where(
            ReportRevision.session_id == Session.id, ReportRevision.state == ReportState.BUILDING)))
        with self.factory() as db:
            for _ in range(2):  # second pass wraps around after the cursor ran off the end
                query = select(Session.id).where(Session.state.in_(_LIVE), or_(open_parts, building))
                if self._cursor is not None:
                    query = query.where(Session.id > self._cursor)
                ids = list(db.scalars(query.order_by(Session.id).limit(limit)))
                self._cursor = ids[-1] if ids else None
                if ids or self._cursor is None:
                    return ids
        return []

    # ---- one session

    def reconcile_session(self, session_id: UUID) -> Counter[str]:
        changes: Counter[str] = Counter()
        with self.factory.begin() as db:
            owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
            if owner is None or owner.state not in _LIVE:
                return changes
            jobs_before = db.scalar(select(func.count(Job.id)).where(Job.session_id == session_id))
            parts = list(db.scalars(select(Part).where(Part.session_id == session_id).order_by(Part.part_no).with_for_update()))
            for part in parts:
                if part.state == PartState.UPLOADED:
                    self._fail_dead_ingest(db, part, changes)
                elif part.state == PartState.PROCESSING:
                    self._repair_processing(db, part, changes)
            if owner.state == SessionState.FINALIZED:
                advance_session(db, session_id)
                self._fail_dead_revision(db, session_id, changes)
            jobs_after = db.scalar(select(func.count(Job.id)).where(Job.session_id == session_id))
            if jobs_after != jobs_before:
                changes["jobs_enqueued"] += jobs_after - jobs_before
        return changes

    @staticmethod
    def _fail_dead_ingest(db: DBSession, part: Part, changes: Counter[str]) -> None:
        run = ingest_run(db, part.id)
        if run is None:
            return
        dead = db.scalar(select(Job.id).where(Job.session_id == part.session_id, Job.stage == "ingest",
                                              Job.subject_kind == "part_run", Job.subject_id == str(run.id),
                                              Job.state == "dead"))
        if dead is None:
            return
        part.state = PartState.FAILED
        part.failure_reason = INGEST_JOB_DEAD
        if run.state in ("pending", "running"):
            run.state = "failed"
        db.flush()
        changes["parts_failed"] += 1

    def _repair_processing(self, db: DBSession, part: Part, changes: Counter[str]) -> None:
        run = assessment_run(db, part.id)
        if run is None:
            ingested = ingest_run(db, part.id)
            try:
                if ingested is None:
                    raise ValueError("no ingest run")
                create_assessment(db, self.ctx, part.id, ingested.id)
                changes["assessments_rebuilt"] += 1
            except ValueError:
                part.state = PartState.FAILED
                part.failure_reason = ASSESSMENT_INPUT_MISSING
                changes["parts_failed"] += 1
            db.flush()
            return
        if vehicle_mask_required(run):
            if not release_masked_segments(db, self.ctx, run):
                job = db.scalar(select(Job).where(Job.session_id == part.session_id, Job.stage == "vehicle_mask",
                                                  Job.subject_kind == "part_run", Job.subject_id == str(run.id)))
                if job is None:
                    enqueue_vehicle_mask(db, run)
                    changes["vehicle_mask_jobs_rebuilt"] += 1
                elif job.state in ("dead", "succeeded"):
                    fail_vehicle_mask(db, self.ctx, run, "VEHICLE_MASK_JOB_DEAD" if job.state == "dead" else "VEHICLE_MASK_OUTPUT_MISSING")
                    changes["vehicle_masks_failed"] += 1
                return
        for concept_run in db.scalars(select(ConceptRun).where(ConceptRun.run_id == run.id, ConceptRun.state == "pending")
                                      .order_by(ConceptRun.id).with_for_update()):
            job = db.scalar(select(Job).where(Job.session_id == part.session_id, Job.stage == "segment",
                                              Job.subject_kind == "concept_run", Job.subject_id == str(concept_run.id)))
            if job is None:
                enqueue(db, JobSpec(part.session_id, "concept_run", str(concept_run.id), "segment", run.input_hash,
                                    run.config_hash))
            elif job.state == "dead":
                concept_run.state = "failed"
                concept_run.reason = SEGMENT_JOB_DEAD
                changes["concept_runs_failed"] += 1
        db.flush()
        if advance_part(db, self.ctx, part.id):
            changes["parts_completed"] += 1

    @staticmethod
    def _fail_dead_revision(db: DBSession, session_id: UUID, changes: Counter[str]) -> None:
        report = db.scalar(select(Report).where(Report.session_id == session_id))
        if report is None or report.current_revision_id is None:
            return
        revision = db.scalar(select(ReportRevision).where(ReportRevision.id == report.current_revision_id).with_for_update())
        if revision is None or revision.state != ReportState.BUILDING:
            return
        dead = db.scalar(select(Job).where(Job.session_id == session_id, Job.subject_kind == "report_revision",
                                           Job.subject_id == str(revision.id), Job.stage.in_(REPORT_STAGES),
                                           Job.state == "dead").order_by(Job.created_at).limit(1))
        if dead is None:
            return
        revision.state = ReportState.FAILED
        if revision.manifest_hash is None:  # a frozen (hashed) manifest is never rewritten; the dead job row records the cause
            revision.manifest = {**(revision.manifest or {}), "failure": {"stage": dead.stage, "code": dead.last_error}}
        db.flush()
        changes["revisions_failed"] += 1


def pipeline_sweeper(settings: Settings, factory: sessionmaker[DBSession], ctx: OrchestrationContext | None = None):  # type: ignore[no-untyped-def]
    """Periodic reconciliation in the existing worker loop: bounded, no extra threads (like `upload_sweeper`)."""
    import time

    reconciler = PipelineReconciler(factory, settings, ctx or masked_orchestration_context(live=settings.model_mode == "live"))
    next_run = time.monotonic()

    def maybe_sweep() -> None:
        nonlocal next_run
        if time.monotonic() < next_run:
            return
        next_run = time.monotonic() + settings.pipeline_reconcile_interval_seconds
        try:
            result = reconciler.sweep()
            if result.total:
                logging.info("Pipeline reconciliation: %s", dict(result.changes))
        except Exception:
            logging.exception("Pipeline reconciliation failed")  # retried next interval; no payloads logged

    return maybe_sweep
