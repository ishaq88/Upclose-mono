"""Owner progress (T05b): part states, reason codes, the Part-1 free verdict and report state.

Free restriction before purchase: this DTO has no paid shape at all. The Part-1 verdict is computed from
persisted Part-1 verified findings and coverage ONLY (`ScoringInput` is built from that part's rows, so no
other zone's evidence can reach scoring or narration), with the deterministic scorer and template
narration. Other zones appear solely as locked zone totals once their part is processed (contract 2 and 7:
zone totals, no IDs, severities, costs, pins or per-concept breakdown). It never creates a report shell or
revision; the verdict carries no report identity (`ProgressResponse.report` does, once finalized).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.api import PartSummary, ProgressResponse, ReportProgress
from app.contracts.enums import FindingStatus, PartState, ReportState
from app.contracts.evidence import Coverage as CoverageContract
from app.contracts.reports import FreeFinding, LockedZoneSummary, Part1Verdict, PublicCoverage
from app.contracts.scoring import ScoringInput
from app.domain.claims import eligible_findings
from app.domain.dedup import INTERNAL_FLAGS
from app.domain.narration import NarrationConfig, load_narration_config, narrate_part1, part1_summary_text
from app.domain.scoring import score_assessment
from app.models import Coverage, Finding as FindingRow, Part, Report, ReportRevision
from app.pipeline.orchestration import CONFIG_DIR, PipelineConfig, assessment_run, load_pipeline_config, to_contract_finding
from app.settings import Settings
from .auth import owner_session
from .billing import has_active_entitlement
from .snapshots import ManifestSourceFrames, SnapshotIntegrityError

@lru_cache(maxsize=1)
def _narration() -> NarrationConfig:
    return load_narration_config(Path(CONFIG_DIR) / "narration.v1.json")


def free_verdict(db: DBSession, config: PipelineConfig, part1: Part, narration: NarrationConfig,
                 locked_parts: list[Part]) -> Part1Verdict | None:
    """Part-1 verdict, or None until Part 1 has been fully processed (barrier crossed)."""
    if part1.state != PartState.PROCESSED:
        return None
    run = assessment_run(db, part1.id, lock=False)
    if run is None:
        return None
    rows = list(db.scalars(select(FindingRow).where(FindingRow.run_id == run.id, FindingRow.part_id == part1.id)
                           .order_by(FindingRow.id)))
    coverage = [CoverageContract(run_id=row.run_id, zone=row.zone, concept=row.concept, state=row.state, reason=row.reason)
                for row in db.scalars(select(Coverage).where(Coverage.run_id == run.id, Coverage.zone == part1.zone))]
    inputs = ScoringInput(session_id=part1.session_id, run_ids=frozenset({run.id}),
                          findings=tuple(to_contract_finding(row, part1) for row in rows), coverage=tuple(coverage))
    score = score_assessment(inputs, config.scoring)
    zone = next(item for item in score.zones if item.zone == part1.zone)
    metadata = (part1.quality or {}).get("source_video")
    source = None if metadata is None else ManifestSourceFrames.model_validate(metadata)
    if source is not None and (source.source_upload_id != part1.source_video_id or zone.score is not None):
        raise SnapshotIntegrityError("unverified source provenance cannot establish a scored verdict")
    eligible = eligible_findings(inputs)
    if source is not None and any(item.evidence.frame_index >= len(source.frame_timestamps_ms) for item in eligible):
        raise SnapshotIntegrityError("finding frame is outside the source timeline")
    findings = tuple(FreeFinding(
        id=item.id, concept=item.concept, severity=item.severity, confidence=item.confidence,
        evidence_note=item.evidence_note, subzone=item.subzone, component_id=item.component_id,
        flags=tuple(flag for flag in item.flags if flag not in INTERNAL_FLAGS), bbox=item.evidence.bbox,
        evidence_available=False,
        source_timestamp_ms=None if source is None else source.frame_timestamps_ms[item.evidence.frame_index],
        mapping_basis=None if source is None else source.mapping_basis)
        for item in eligible)
    counts: dict[UUID, int] = {}
    if locked_parts:
        counts = dict(db.execute(select(FindingRow.part_id, func.count(FindingRow.id)).where(
            FindingRow.part_id.in_([p.id for p in locked_parts]), FindingRow.status == FindingStatus.VERIFIED,
            FindingRow.duplicate_of.is_(None)).group_by(FindingRow.part_id)).all())
    locked = tuple(LockedZoneSummary(zone=part.zone, finding_count=counts.get(part.id, 0)) for part in locked_parts)
    return Part1Verdict(coverage=(PublicCoverage(zone=zone.zone, state=zone.state,
        reason="video_zone_unverified" if source is not None else zone.reason),),
                        part1_score=zone.score, findings=findings, locked_zones=locked,
                        part1_summary=part1_summary_text(narrate_part1(inputs, score, narration)))


class ProgressService:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings, config: PipelineConfig | None = None) -> None:
        self.sessions, self.settings = sessions, settings
        self.config = config or load_pipeline_config()

    def get(self, token: str, session_id: UUID) -> ProgressResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id)
            parts = list(db.scalars(select(Part).where(Part.session_id == owner.id).order_by(Part.part_no)))
            report = db.scalar(select(Report).where(Report.session_id == owner.id))
            revision = None if report is None or report.current_revision_id is None else db.get(ReportRevision, report.current_revision_id)
            progress = None if report is None or revision is None else ReportProgress(
                slug=report.share_slug, state=revision.state,
                unlocked=revision.state == ReportState.READY and has_active_entitlement(db, report.id))
            verdict = free_verdict(
                db, self.config, parts[0], _narration(),
                [part for part in parts[1:] if part.state == PartState.PROCESSED])
            return ProgressResponse(
                state=owner.state, free_verdict=verdict, report=progress,
                parts=tuple(PartSummary(id=part.id, part_no=part.part_no, zone=part.zone, state=part.state,
                                        reason=part.failure_reason, active_upload_id=part.active_upload_id)
                            for part in parts))
