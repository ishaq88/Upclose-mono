"""Stage orchestration (T05b): fan-out after ingest, part barrier, full-report barrier.

Every function here runs INSIDE a transaction whose session row is already locked by the caller (queue
publication, finalize, or the reconciler), so the lock order is always session -> part -> run/children and
concurrent last-job completions serialize on the session lock. The second one re-reads what the first
committed and therefore sees the barrier already crossed.

Durable state (no new schema):

- `pipeline_runs` row `assess:{part_id}`: the part's assessment run. Its `config_hashes` freeze the
  prompt/rubric/scoring/dedup config and the segmenter/verifier identities (invariant 8); concept runs,
  findings and coverage hang off it. The ingest run is closed (`succeeded`) when the part completes.
- `observations` rows, discriminated by `kind` (they are the only existing JSONB store keyed by run):
  `assess_input` (frozen ingest artifacts this assessment reads), `track_footprint` (every frame a
  candidate's track was seen on, needed for dedup), `dedup_run` (the cross-concept-run dedup result,
  including merge provenance and ambiguous overlaps; its presence means "dedup done, verification
  scheduled"), `verify_group` (subject of a `duplicate_group` verify job), `verification_provenance`
  (rubric and frame provenance of each resolution) and `verification_failed`.

Barriers:

- Part: every concept run terminal -> dedup once and schedule verification -> every verify job terminal ->
  leftover candidates become `unverified` -> coverage rows via `derive_zone_coverage` -> part `processed`.
  A part with no runnable concept completes immediately (zero candidates drain the same way).
- Full report: session finalized, every part terminal (`processed`, `failed`, `skipped`) and no segment or
  verify job outstanding -> exactly one `claims` job for the revision created at finalize (its idempotency
  key is derived from the revision id only, so re-evaluation can never add a second chain).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession

from app.contracts.enums import ConceptRunState, FindingStatus, PartState, ReportState, SessionState
from app.contracts.evidence import BoundingBox, ConceptRun as ConceptRunContract, EvidenceRef, Finding
from app.domain.config import ScoringConfig, load_scoring_config
from app.domain.dedup import AmbiguousOverlap, DedupResult, FrameBox, TrackFootprint, apply_dedup, dedup_candidates
from app.domain.rubrics import (PromptConfig, RubricRegistry, load_prompt_config, load_rubric_registry,
                                plan_part_prompts, validate_plan_against_scoring)
from app.domain.verification import derive_zone_coverage
from app.jobs.queue import JobSpec, enqueue
from app.models import (Artifact, ConceptRun, Coverage, Finding as FindingRow, Job, Observation, Part,
                        PipelineRun, Report, ReportRevision, Session, VideoUpload)

if TYPE_CHECKING:
    from app.pipeline.ingest import IngestOutcome
    from app.jobs.worker import Publication

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
TERMINAL_PART_STATES = (PartState.PROCESSED, PartState.FAILED, PartState.SKIPPED)
OPEN_JOB_STATES = ("queued", "running")

# Observation kinds (see module docstring).
ASSESS_INPUT = "assess_input"
VEHICLE_MASK = "vehicle_mask"
TRACK_FOOTPRINT = "track_footprint"
DEDUP_RUN = "dedup_run"
VERIFY_GROUP = "verify_group"
VERIFICATION_PROVENANCE = "verification_provenance"
VERIFICATION_FAILED = "verification_failed"

# Stable machine reasons (<= 240 chars, never paths or provider text).
SEGMENT_JOB_DEAD = "SEGMENT_JOB_DEAD"
INGEST_JOB_DEAD = "INGEST_JOB_DEAD"
RUBRIC_DISABLED_PREFIX = "rubric_unsupported"

REPORT_CHAIN_VERSION = "report-chain-v1"


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


# Fixture-mode provider identities: frozen into every assessment run so a later live run is never
# mistaken for (or replayed as) a fixture one.
FIXTURE_PROVIDERS: Mapping[str, str] = {
    "segmenter": canonical_hash({"provider": "fixture-segmenter", "provenance": "synthetic", "version": 1}),
    "verifier": canonical_hash({"provider": "fixture-verifier", "provenance": "synthetic", "version": 1}),
}



# Live-mode provider identities: frozen into live assessment runs so report provenance
# derives `live` and never replays as fixture. Demo (non-Standard) runs use the same
# identities; the non-Standard disclosure lives in the demo exception receipt + ledger.
MASKED_LIVE_PROVIDERS: Mapping[str, str] = {
    "segmenter": canonical_hash({"provider": "meta-sam-live", "model": "sam-3.1", "provenance": "live", "version": 3,
                                 "vehicle_mask": {"prompt": "car", "policy": "masked-only"}}),
    "vehicle_mask": canonical_hash({"version": "vehicle-mask-v1", "prompt": "car", "policy": "masked-only"}),
    "verifier": canonical_hash({"provider": "live-pending-verifier", "provenance": "live", "version": 1}),
}


# Keep runtime on its accepted identity until the durable stage, migration and
# masked-only consumer are integrated together. T13 is not enabled by a partial edit.
LIVE_PROVIDERS: Mapping[str, str] = {
    "segmenter": canonical_hash({"provider": "meta-sam-live", "model": "sam-3.1", "provenance": "live", "version": 2,
                                 "containment": {"prompt": "car", "threshold": 0.5, "policy": "advisory"}}),
    "verifier": canonical_hash({"provider": "live-pending-verifier", "provenance": "live", "version": 1}),
}


MASKED_FIXTURE_PROVIDERS: Mapping[str, str] = {
    **FIXTURE_PROVIDERS,
    "segmenter": canonical_hash({"provider": "fixture-segmenter", "provenance": "synthetic",
                                  "version": 2, "policy": "masked-only"}),
    "vehicle_mask": canonical_hash({"version": "vehicle-mask-v1", "prompt": "car", "policy": "masked-only"}),
}


# Only a worker that actually installs the Spark verifier may freeze this identity: coverage
# treats any verifier other than the live pending one as able to resolve candidates.
SPARK_VERIFIER: str = canonical_hash({"provider": "meta-spark-verifier", "model": "muse-spark-1.3",
                                      "provenance": "live", "version": 1,
                                      "input_token_bound": "text-bytes+8x8-pixel-blocks"})


def masked_orchestration_context(live: bool = False, *, spark_verifier: bool = False) -> OrchestrationContext:
    providers = dict(MASKED_LIVE_PROVIDERS if live else MASKED_FIXTURE_PROVIDERS)
    if live and spark_verifier:
        providers["verifier"] = SPARK_VERIFIER
    return OrchestrationContext(providers=providers)


def live_orchestration_context(config: PipelineConfig | None = None) -> OrchestrationContext:
    return OrchestrationContext(config=config or load_pipeline_config(), providers=dict(LIVE_PROVIDERS))

# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    prompts: PromptConfig
    rubrics: RubricRegistry
    scoring: ScoringConfig

    def hashes(self, providers: Mapping[str, str]) -> dict[str, str]:
        return {"prompts": self.prompts.content_hash, "rubrics": self.rubrics.config_hash,
                "scoring": self.scoring.content_hash, "dedup": self.prompts.dedup.content_hash,
                "segmenter": providers["segmenter"], "verifier": providers["verifier"],
                **({"vehicle_mask": providers["vehicle_mask"]} if "vehicle_mask" in providers else {})}


@lru_cache(maxsize=1)
def load_pipeline_config() -> PipelineConfig:
    prompts = load_prompt_config(CONFIG_DIR / "prompts.v1.json")
    scoring = load_scoring_config(CONFIG_DIR / "scoring.v1.json")
    validate_plan_against_scoring(prompts, scoring)
    return PipelineConfig(prompts, load_rubric_registry(CONFIG_DIR / "rubrics.v1.json"), scoring)


@dataclass(frozen=True, slots=True)
class OrchestrationContext:
    """Everything the fan-out, handlers and reconciler must agree on."""
    config: PipelineConfig = field(default_factory=load_pipeline_config)
    providers: Mapping[str, str] = field(default_factory=lambda: dict(FIXTURE_PROVIDERS))

    @property
    def config_hashes(self) -> dict[str, str]:
        return self.config.hashes(self.providers)


# --------------------------------------------------------------------------- row <-> contract


def to_contract_finding(row: FindingRow, part: Part) -> Finding:
    return Finding(
        id=row.id, session_id=row.session_id, run_id=row.run_id, part_id=row.part_id,
        concept_run_id=row.concept_run_id, part_no=part.part_no, zone=part.zone, track_id=row.track_id,
        status=row.status, concept=row.concept,
        evidence=EvidenceRef(frame_key=row.frame_key, frame_index=row.frame_index, frame_width=row.frame_width,
                             frame_height=row.frame_height, bbox=BoundingBox(**row.bbox), mask_key=row.mask_key),
        severity=row.severity, confidence=None if row.confidence is None else float(row.confidence),
        evidence_note=row.evidence_note, subzone=row.subzone, component_id=row.component_id,
        flags=tuple(row.flags), duplicate_of=row.duplicate_of)


def apply_to_row(row: FindingRow, finding: Finding) -> None:
    """Copy the resolved fields of a validated contract finding onto its row."""
    row.status = finding.status
    row.severity = finding.severity
    row.confidence = None if finding.confidence is None else Decimal(str(finding.confidence))
    row.evidence_note = finding.evidence_note
    row.subzone = finding.subzone
    row.component_id = finding.component_id
    row.flags = list(finding.flags)
    row.duplicate_of = finding.duplicate_of


def assessment_run(db: DBSession, part_id: UUID, *, lock: bool = True) -> PipelineRun | None:
    query = select(PipelineRun).where(PipelineRun.subject_key == f"assess:{part_id}").order_by(PipelineRun.created_at).limit(1)
    return db.scalar(query.with_for_update() if lock else query)


def ingest_run(db: DBSession, part_id: UUID, *, lock: bool = True) -> PipelineRun | None:
    query = select(PipelineRun).where(PipelineRun.subject_key == f"ingest:{part_id}").order_by(PipelineRun.created_at).limit(1)
    return db.scalar(query.with_for_update() if lock else query)


def observations(db: DBSession, run_id: UUID, kind: str) -> list[Observation]:
    return list(db.scalars(select(Observation).where(Observation.run_id == run_id, Observation.kind == kind)
                           .order_by(Observation.created_at, Observation.id)))


# --------------------------------------------------------------------------- fan-out after ingest


def ingest_artifacts(db: DBSession, part: Part, ingest_run_id: UUID) -> dict[str, object]:
    """The committed ingest artifacts of this part, found by key prefix (artifact rows lack part/run
    columns). Exactly one attempt can have published, so exactly one proxy and manifest must exist."""
    prefix = f"media/{part.session_id}/{part.id}/{ingest_run_id}/a"
    rows = list(db.scalars(select(Artifact).where(Artifact.session_id == part.session_id,
                                                  Artifact.key.like(prefix + "%")).order_by(Artifact.key)))
    proxies = [r for r in rows if r.kind == "proxy"]
    manifests = [r for r in rows if r.kind == "derivative"]
    if len(proxies) != 1 or len(manifests) != 1:
        raise ValueError("ingest artifacts are missing or ambiguous")
    frames = [{"frame_index": int(r.key.rsplit("_", 1)[1].split(".")[0]), "key": r.key, "sha256": r.sha256}
              for r in rows if r.kind == "frame"]
    return {"ingest_run_id": str(ingest_run_id), "proxy_key": proxies[0].key, "proxy_sha256": proxies[0].sha256,
            "manifest_key": manifests[0].key, "manifest_sha256": manifests[0].sha256,
            "frames": sorted(frames, key=lambda item: item["frame_index"])}


def create_assessment(db: DBSession, ctx: OrchestrationContext, part_id: UUID, ingest_run_id: UUID) -> UUID | None:
    """Create assessment/concept rows and their mask predecessor or segment jobs; idempotent.

    Caller holds the session lock. Rubric-disabled concepts are recorded as `unsupported` concept runs
    with the rubric reason and are never run. A part with nothing runnable completes right here.
    """
    part = db.scalar(select(Part).where(Part.id == part_id).with_for_update())
    if part is None or part.state != PartState.PROCESSING:
        return None
    existing = assessment_run(db, part_id)
    if existing is not None:
        return existing.id
    facts = ingest_artifacts(db, part, ingest_run_id)
    ingested = db.get(PipelineRun, ingest_run_id)
    if ingested is None or ingested.part_id != part.id:
        raise ValueError("ingest run does not belong to this part")
    hashes = ctx.config_hashes
    config = ctx.config
    run = PipelineRun(id=uuid4(), session_id=part.session_id, part_id=part.id, subject_key=f"assess:{part.id}",
                      input_hash=canonical_hash({"raw_sha256": part.raw_sha256, "proxy": facts["proxy_sha256"],
                                                 "manifest": facts["manifest_sha256"]}),
                      config_hashes={**hashes, "media": str(ingested.config_hashes.get("media", ""))},
                      config_hash=canonical_hash(hashes), state="running")
    db.add(run)
    db.flush()
    db.add(Observation(session_id=part.session_id, run_id=run.id, kind=ASSESS_INPUT, value=facts))
    plan = plan_part_prompts(config.prompts, config.rubrics, part.part_no)
    expected = config.prompts.expected_prompts(part.part_no)
    pending: list[ConceptRun] = []
    for entry in plan.runnable:
        row = ConceptRun(id=uuid4(), session_id=part.session_id, run_id=run.id, part_id=part.id,
                         concept=entry.concept, prompt_id=entry.prompt_id, state="pending")
        pending.append(row)
        db.add(row)
    for disabled in plan.disabled_concepts:
        for prompt_id in expected[disabled.concept]:
            db.add(ConceptRun(id=uuid4(), session_id=part.session_id, run_id=run.id, part_id=part.id,
                              concept=disabled.concept, prompt_id=prompt_id, state="unsupported",
                              reason=disabled.reason[:240]))
    db.flush()
    if pending and vehicle_mask_required(run):
        enqueue_vehicle_mask(db, run)
    else:
        for row in pending:
            enqueue(db, JobSpec(part.session_id, "concept_run", str(row.id), "segment", run.input_hash, run.config_hash))
    advance_part(db, ctx, part.id)  # nothing runnable (dash, interior, all rubrics disabled): drains here
    return run.id


def vehicle_mask_required(run: PipelineRun) -> bool:
    return "vehicle_mask" in run.config_hashes


def enqueue_vehicle_mask(db: DBSession, run: PipelineRun) -> UUID:
    return enqueue(db, JobSpec(run.session_id, "part_run", str(run.id), "vehicle_mask", run.input_hash, run.config_hash))


def release_masked_segments(db: DBSession, ctx: OrchestrationContext, run: PipelineRun,
                            completing_job: UUID | None = None) -> bool:
    """Release fan-out only from a complete, persisted and validated mask binding."""
    from app.pipeline.vehicle_mask_contract import load_vehicle_mask_marker

    if not vehicle_mask_required(run):
        return False
    try:
        load_vehicle_mask_marker(db, run)
    except ValueError:
        return False
    for row in db.scalars(select(ConceptRun).where(ConceptRun.run_id == run.id,
                                                   ConceptRun.state == "pending").order_by(ConceptRun.id).with_for_update()):
        enqueue(db, JobSpec(run.session_id, "concept_run", str(row.id), "segment", run.input_hash, run.config_hash))
    advance_part(db, ctx, run.part_id, completing_job)
    return True


def fail_vehicle_mask(db: DBSession, ctx: OrchestrationContext, run: PipelineRun, reason: str,
                      completing_job: UUID | None = None) -> bool:
    """A missing assessment never becomes a clean, unmasked damage run."""
    safe_reason = reason if reason and len(reason) <= 240 and all(c.isupper() or c.isdigit() or c == "_" for c in reason) else "VEHICLE_MASK_FAILED"
    for row in db.scalars(select(ConceptRun).where(ConceptRun.run_id == run.id,
                                                   ConceptRun.state == "pending").order_by(ConceptRun.id).with_for_update()):
        row.state, row.reason = "failed", safe_reason
    db.flush()
    return advance_part(db, ctx, run.part_id, completing_job)


def plan_downstream(ctx: OrchestrationContext, outcome: IngestOutcome) -> Publication:
    """The T05b fan-out seam: write the assessment inside the ingest publication transaction."""
    from app.jobs.worker import Publication

    def publish(db: DBSession) -> None:
        create_assessment(db, ctx, outcome.part_id, outcome.run_id)

    return Publication(publish=publish)


# --------------------------------------------------------------------------- part barrier


def _components(groups: Sequence[frozenset[UUID]]) -> list[frozenset[UUID]]:
    merged: list[set[UUID]] = []
    for group in groups:
        overlapping = [existing for existing in merged if existing & group]
        combined = set(group).union(*overlapping) if overlapping else set(group)
        merged = [existing for existing in merged if existing not in overlapping] + [combined]
    return [frozenset(item) for item in merged]


def _finding_input_hash(rows: Sequence[FindingRow]) -> str:
    return canonical_hash([{"id": str(row.id), "frame_key": row.frame_key, "frame_index": row.frame_index,
                            "bbox": row.bbox, "concept": row.concept.value} for row in sorted(rows, key=lambda r: r.id)])


def _dedup_candidates(db: DBSession, ctx: OrchestrationContext, part: Part, run: PipelineRun) -> None:
    """Cross-concept-run dedup for the part, once. Persists the result (merge provenance included)."""
    rows = list(db.scalars(select(FindingRow).where(FindingRow.run_id == run.id, FindingRow.status == FindingStatus.CANDIDATE)
                           .order_by(FindingRow.id).with_for_update()))
    frames_by_finding: dict[str, list[FrameBox]] = {}
    for obs in observations(db, run.id, TRACK_FOOTPRINT):
        frames_by_finding[obs.value["finding_id"]] = [FrameBox.model_validate(item) for item in obs.value["frames"]]
    footprints = []
    for row in rows:
        finding = to_contract_finding(row, part)
        frames = tuple(frames_by_finding.get(str(row.id)) or
                       [FrameBox(frame_index=finding.evidence.frame_index, bbox=finding.evidence.bbox)])
        footprints.append(TrackFootprint(finding=finding, frames=frames))
    result = dedup_candidates(footprints, ctx.config.prompts.dedup) if footprints else DedupResult(
        config_hash=ctx.config.prompts.dedup.content_hash, survivor_ids=(), merges=(), ambiguous=())
    if footprints:
        by_id = {row.id: row for row in rows}
        for updated in apply_dedup([item.finding for item in footprints], result):
            apply_to_row(by_id[updated.id], updated)
    value = result.model_dump(mode="json")
    value["candidate_ids"] = [str(row.id) for row in rows]
    db.add(Observation(session_id=part.session_id, run_id=run.id, kind=DEDUP_RUN, value=value))
    db.flush()


def _schedule_verification(db: DBSession, run: PipelineRun, marker: Observation) -> None:
    """One verify job per live candidate, or per explicit duplicate group for ambiguous overlaps.
    Idempotent: job keys are unique, and a group observation is reused for the same member set."""
    ambiguous = [AmbiguousOverlap.model_validate(item) for item in marker.value["ambiguous"]]
    components = _components([item.finding_ids for item in ambiguous])
    live = {row.id: row for row in db.scalars(select(FindingRow).where(
        FindingRow.run_id == run.id, FindingRow.status == FindingStatus.CANDIDATE).order_by(FindingRow.id))}
    grouped = set().union(*components) if components else set()
    for finding_id, row in live.items():
        if finding_id not in grouped:
            enqueue(db, JobSpec(run.session_id, "finding", str(finding_id), "verify", _finding_input_hash([row]),
                                run.config_hash))
    known = {frozenset(UUID(item) for item in obs.value["finding_ids"]): obs for obs in observations(db, run.id, VERIFY_GROUP)}
    for component in components:
        members = [live[item] for item in component if item in live]
        if not members:
            continue
        obs = known.get(component)
        if obs is None:
            obs = Observation(id=uuid4(), session_id=run.session_id, run_id=run.id, kind=VERIFY_GROUP,
                              value={"finding_ids": sorted(str(item) for item in component)})
            db.add(obs)
            db.flush()
        enqueue(db, JobSpec(run.session_id, "duplicate_group", str(obs.id), "verify", _finding_input_hash(members),
                            run.config_hash))


def _open_verify_jobs(db: DBSession, run: PipelineRun, completing_job: UUID | None) -> bool:
    finding_ids = [str(item) for item in db.scalars(select(FindingRow.id).where(FindingRow.run_id == run.id))]
    group_ids = [str(obs.id) for obs in observations(db, run.id, VERIFY_GROUP)]
    clauses = []
    if finding_ids:
        clauses.append((Job.subject_kind == "finding") & Job.subject_id.in_(finding_ids))
    if group_ids:
        clauses.append((Job.subject_kind == "duplicate_group") & Job.subject_id.in_(group_ids))
    if not clauses:
        return False
    condition = clauses[0] if len(clauses) == 1 else clauses[0] | clauses[1]
    return db.scalar(select(Job.id).where(Job.session_id == run.session_id, Job.stage == "verify",
                                          Job.state.in_(OPEN_JOB_STATES), Job.id != completing_job, condition).limit(1)) is not None


def advance_part(db: DBSession, ctx: OrchestrationContext, part_id: UUID, completing_job: UUID | None = None) -> bool:
    """Evaluate the part barrier; True only on the call that completes the part.

    `completing_job` is the job whose publication is running this evaluation: it is still `running` inside
    its own transaction (the queue marks it `succeeded` last), so it must not count as outstanding work."""
    part = db.scalar(select(Part).where(Part.id == part_id).with_for_update())
    if part is None or part.state != PartState.PROCESSING:
        return False
    run = assessment_run(db, part.id)
    if run is None:
        return False  # the reconciler rebuilds a missing assessment
    concept_runs = list(db.scalars(select(ConceptRun).where(ConceptRun.run_id == run.id).order_by(ConceptRun.id).with_for_update()))
    if any(row.state in ("pending", "running") for row in concept_runs):
        return False
    markers = observations(db, run.id, DEDUP_RUN)
    if not markers:
        _dedup_candidates(db, ctx, part, run)
        markers = observations(db, run.id, DEDUP_RUN)
    _schedule_verification(db, run, markers[0])  # idempotent rebuild of any missing verify work
    if _open_verify_jobs(db, run, completing_job):
        return False
    return _complete_part(db, ctx, part, run, concept_runs, markers[0], completing_job)


def _complete_part(db: DBSession, ctx: OrchestrationContext, part: Part, run: PipelineRun,
                   concept_runs: Sequence[ConceptRun], marker: Observation, completing_job: UUID | None = None) -> bool:
    findings = list(db.scalars(select(FindingRow).where(FindingRow.run_id == run.id).order_by(FindingRow.id).with_for_update()))
    for row in findings:
        if row.status == FindingStatus.CANDIDATE:
            row.status = FindingStatus.UNVERIFIED  # verification never concluded (dead job): never a clean result
    db.flush()
    config = ctx.config
    required = next(row.required_concepts for row in config.scoring.zones if row.zone == part.zone)
    quality = part.quality or {}
    contract_runs = [ConceptRunContract(id=row.id, run_id=row.run_id, part_id=row.part_id, zone=part.zone,
                                        concept=row.concept, prompt_id=row.prompt_id, state=ConceptRunState(row.state),
                                        reason=row.reason) for row in concept_runs]
    coverage = derive_zone_coverage(
        run_id=run.id, zone=part.zone, required_concepts=required,
        expected_prompts=config.prompts.expected_prompts(part.part_no), concept_runs=contract_runs,
        findings=[to_contract_finding(row, part) for row in findings],
        ambiguous=[AmbiguousOverlap.model_validate(item) for item in marker.value["ambiguous"]],
        rubrics=config.rubrics, quality_usable=quality.get("usable_for_scoring") is True,
        verification_available=run.config_hashes.get("verifier") != LIVE_PROVIDERS["verifier"])
    for item in coverage:
        db.execute(insert(Coverage).values(id=uuid4(), session_id=part.session_id, run_id=item.run_id, zone=item.zone,
                                           concept=item.concept, state=item.state, reason=item.reason)
                   .on_conflict_do_nothing(index_elements=[Coverage.run_id, Coverage.zone, Coverage.concept]))
    part.state = PartState.PROCESSED
    run.state = "succeeded"
    closed = ingest_run(db, part.id)
    if closed is not None and closed.state == "running":
        closed.state = "succeeded"
    db.flush()
    advance_session(db, part.session_id, completing_job)
    return True


# --------------------------------------------------------------------------- full-report barrier


def report_chain_spec(session_id: UUID, revision_id: UUID) -> JobSpec:
    """First stage of the report chain. Identity depends only on the revision, never on mutable state."""
    return JobSpec(session_id, "report_revision", str(revision_id), "claims",
                   canonical_hash({"report_revision": str(revision_id), "chain": REPORT_CHAIN_VERSION}),
                   canonical_hash({"chain": REPORT_CHAIN_VERSION}))


def advance_session(db: DBSession, session_id: UUID, completing_job: UUID | None = None) -> bool:
    """True when the session's report chain is (now or already) scheduled by this evaluation.
    `completing_job`: see `advance_part`."""
    owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
    if owner is None or owner.state != SessionState.FINALIZED:
        return False
    if db.scalar(select(VideoUpload.id).where(VideoUpload.session_id == session_id,
        VideoUpload.state.in_(("open", "completing", "accepted")),
        VideoUpload.processing_state.in_(("pending", "queued"))).limit(1)) is not None:
        return False
    parts = list(db.scalars(select(Part).where(Part.session_id == session_id).order_by(Part.part_no).with_for_update()))
    if any(part.state not in TERMINAL_PART_STATES for part in parts):
        return False
    if db.scalar(select(Job.id).where(Job.session_id == session_id, Job.stage.in_(("vehicle_mask", "segment", "verify")),
                                      Job.state.in_(OPEN_JOB_STATES), Job.id != completing_job).limit(1)) is not None:
        return False
    report = db.scalar(select(Report).where(Report.session_id == session_id).with_for_update())
    revision = None if report is None or report.current_revision_id is None else db.scalar(
        select(ReportRevision).where(ReportRevision.id == report.current_revision_id).with_for_update())
    if revision is None or revision.state != ReportState.BUILDING:
        return False
    enqueue(db, report_chain_spec(session_id, revision.id))
    return True


# --------------------------------------------------------------------------- worker wiring


def install_pipeline_handlers(registry, factory, storage, settings, ctx: OrchestrationContext | None = None, *,  # type: ignore[no-untyped-def]
                              segmenter_factory=None, verifier=None, budget_service=None) -> OrchestrationContext:
    """Register ingest, the optional durable vehicle-mask predecessor, segment and verify.

    The ingest publication is wrapped so a FAILED part also re-evaluates the full-report barrier in the
    same transaction (the ingest handler's failure path has no fan-out hook of its own)."""
    from app.jobs.worker import Publication
    from app.pipeline.ingest import IngestHandler
    from app.pipeline.segment import default_segmenter_factory, register_segment_handler
    from app.pipeline.verify import register_verify_handler

    ctx = ctx or OrchestrationContext()
    ingest = IngestHandler(factory, storage, settings, fanout=lambda outcome: plan_downstream(ctx, outcome))

    def with_barrier(lease):  # type: ignore[no-untyped-def]
        publication = ingest(lease)
        if publication.publish is None or lease.session_id is None:
            return publication
        inner, session_id = publication.publish, lease.session_id

        def publish(db: DBSession) -> None:
            inner(db)
            advance_session(db, session_id, lease.job_id)

        return Publication(publish=publish, downstream=publication.downstream)

    registry.register("ingest", with_barrier)
    if "vehicle_mask" in ctx.providers:
        from app.pipeline.vehicle_mask import VehicleMaskHandler, synthetic_vehicle_segmenter_factory

        registry.register("vehicle_mask", VehicleMaskHandler(
            factory, storage, settings, ctx,
            segmenter_factory=segmenter_factory or synthetic_vehicle_segmenter_factory,
            budget_service=budget_service))
    register_segment_handler(registry, factory, storage, settings, ctx,
                             segmenter_factory=segmenter_factory or default_segmenter_factory, budget_service=budget_service)
    register_verify_handler(registry, factory, ctx, verifier=verifier)
    return ctx
