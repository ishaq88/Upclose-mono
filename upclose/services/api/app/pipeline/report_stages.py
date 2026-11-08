"""Report-stage handlers (T07b): `claims -> score -> narrate -> render` for one report revision.

Chain: the full-report barrier (orchestration) enqueues `claims` once. Each stage computes from the frozen
manifest and stored stage outputs, then its publication writes its output and enqueues the next stage in the
same fenced transaction (`Publication.downstream`); `render` is the only code that sets a revision `ready`.

| stage   | reads                                  | writes (one fenced publication)                                   | next    |
|---------|----------------------------------------|--------------------------------------------------------------------|---------|
| claims  | persisted findings/coverage (ONCE)     | manifest + manifest_hash + revision_findings, claims + claim_evidence, paid_narration.claims | score |
| score   | manifest                               | score_result (scores + repairs)                                   | narrate |
| narrate | manifest, score_result, claims         | free_narration, paid_narration.narration                          | render  |
| render  | manifest + every stored output         | rendered_at, state = ready                                         | -       |

Rules:

- Stages never read mutable finding rows after the snapshot. The claims handler builds the manifest outside
  the publication; the publication rebuilds it under the session/revision locks and refuses to freeze if it
  differs (nothing changes findings after the barrier, so a mismatch means retry, never a silent drift).
- Optional model seams (`claims_model`, `narration_model`) are called in the handler, OUTSIDE the publication
  transaction; their output is untrusted and only reaches storage through `resolve_claims` /
  `resolve_*_narration`, which fall back to the deterministic templates on any invalid or failed output. The
  worker installs none: the offline template path is the default. A live checker/narrator must be reached
  through an adapter that reserves budget first.
- Scope: the free narration is computed from Part-1 evidence only; the paid narration and claims never enter
  a free DTO.
- Terminal failures (`INVALID_INPUT`: bad manifest hash, config drift, unbuildable DTOs) are not retried and
  the reconciler then fails the revision. A deleting session cannot publish (queue fence).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.storage import ObjectStorage
from app.contracts.enums import ReportState
from app.contracts.evidence import ClaimResult, NarratedStatement
from app.contracts.scoring import ScoringInput
from app.domain.claims import ClaimsOutput, resolve_claims
from app.domain.narration import (NarrationInputError, NarrationOutcome, NarrationScope, narrate_full, narrate_part1,
                                  resolve_full_narration, resolve_part1_narration)
from app.jobs.queue import Failure, JobSpec, Lease
from app.jobs.worker import Publication, StageFailure
from app.contracts.briefing import NarrationAudioMeta, StoredBriefing
from app.contracts.reports import PaidReport
from app.contracts.sources import RigTelemetryData
from app.models import Claim, ClaimEvidence, EvidenceSource, Report, ReportRevision
from app.pipeline.briefing import build_briefing
from app.pipeline.orchestration import canonical_hash
from app.report import render_report
from app.services.snapshots import (ClaimsMeta, Provenance, ReportConfig, ReportManifest,
                                    RevisionSnapshot, SnapshotIncomplete, SnapshotIntegrityError, SnapshotNotReady,
                                    StoredFreeNarration, StoredPaidNarration, StoredScore, build_manifest,
                                    build_reports, check_configs, claims_digest, compute_score, evidence_input, freeze_manifest,
                                    is_frozen, load_manifest, load_report_config, load_snapshot, manifest_payload,
                                    require_building, score_payload, stored_claims)
from app.settings import Settings

ClaimsModel = Callable[[str | None, ScoringInput], object]
NarrationModel = Callable[[NarrationScope, ScoringInput, Sequence[NarratedStatement]], object]
REPORT_STAGE_NAMES = ("claims", "score", "narrate", "render")
MODEL_UNAVAILABLE = "model_unavailable"
_DEFAULT_PROVENANCE: dict[str, Provenance] = {"fixture": "synthetic", "replay": "cached", "live": "live"}
_Subject = tuple[ReportRevision, bool]


def _terminal() -> StageFailure:
    return StageFailure(Failure("INVALID_INPUT", retryable=False))


def stage_spec(session_id: UUID, revision_id: UUID, stage: str, manifest_hash: str, manifest: ReportManifest) -> JobSpec:
    """Identity of a later chain stage: the revision, the frozen manifest hash and the frozen configs."""
    return JobSpec(session_id, "report_revision", str(revision_id), stage,  # type: ignore[arg-type]
                   manifest_hash, canonical_hash({"stage": stage, "configs": manifest.configs.model_dump(mode="json")}))


class ReportStages:
    def __init__(self, factory: sessionmaker[DBSession], settings: Settings, config: ReportConfig | None = None, *,
                 claims_model: ClaimsModel | None = None, narration_model: NarrationModel | None = None,
                 storage: ObjectStorage | None = None) -> None:
        self.factory, self.settings, self.storage = factory, settings, storage
        self.config = config or load_report_config()
        self.claims_model, self.narration_model = claims_model, narration_model
        self.default_provenance = _DEFAULT_PROVENANCE[settings.model_mode]

    # ------------------------------------------------------------------ shared plumbing

    @staticmethod
    def _locate(db: DBSession, lease: Lease, *, lock: bool) -> _Subject | None:
        """(revision, writable) for the lease subject. None: nothing to do (not current or no longer building)."""
        try:
            revision_id = UUID(lease.subject_id)
        except ValueError:
            raise _terminal() from None
        if lease.session_id is None:
            raise _terminal()
        query = select(ReportRevision).where(ReportRevision.id == revision_id, ReportRevision.session_id == lease.session_id)
        revision = db.scalar(query.with_for_update() if lock else query)
        if revision is None:
            raise _terminal()
        report = db.get(Report, revision.report_id)
        current = report is not None and report.current_revision_id == revision.id
        return revision, current and revision.state == ReportState.BUILDING

    def _frozen(self, revision: ReportRevision) -> tuple[ReportManifest, str]:
        try:
            manifest = load_manifest(revision)
            check_configs(manifest, self.config)
        except (SnapshotIntegrityError, SnapshotIncomplete, ValidationError):
            raise _terminal() from None
        return manifest, str(revision.manifest_hash)

    def _locked_for_publish(self, db: DBSession, lease: Lease) -> ReportRevision | None:
        located = self._locate(db, lease, lock=True)
        if located is None or not located[1]:
            return None
        return located[0]

    @staticmethod
    def _expect(revision: ReportRevision, digest: str) -> None:
        require_building(revision)
        if revision.manifest_hash != digest:
            raise SnapshotIntegrityError("revision changed between handler and publication")

    # ------------------------------------------------------------------ claims (snapshot first)

    def claims(self, lease: Lease) -> Publication:
        with self.factory() as db:
            located = self._locate(db, lease, lock=False)
            if located is None or not located[1]:
                return Publication()
            revision = located[0]
            try:
                manifest = load_manifest(revision) if is_frozen(revision) else build_manifest(
                    db, revision, self.config, self.default_provenance)
                check_configs(manifest, self.config)
            except SnapshotNotReady:
                raise StageFailure(Failure("TRANSIENT")) from None
            except (SnapshotIntegrityError, SnapshotIncomplete, ValidationError):
                raise _terminal() from None
        digest = canonical_hash(manifest_payload(manifest))
        output = self._claims_output(manifest)
        meta = ClaimsMeta(source=output.source, notes=output.notes, errors=output.errors, config_hash=output.config_hash,
                          listing_hash=output.listing_hash, claim_count=len(output.claims),
                          claims_hash=claims_digest(output.claims))

        def publish(db: DBSession) -> None:
            revision = self._locked_for_publish(db, lease)
            if revision is None:
                return
            require_building(revision)
            if not is_frozen(revision):
                rebuilt = build_manifest(db, revision, self.config, self.default_provenance)
                if canonical_hash(manifest_payload(rebuilt)) != digest:
                    raise SnapshotIntegrityError("evidence changed between the claims handler and its publication")
                freeze_manifest(db, revision, rebuilt)
            self._expect(revision, digest)
            if revision.paid_narration is None:
                _write_claims(db, revision, manifest, output.claims)
                revision.paid_narration = StoredPaidNarration(manifest_hash=digest, claims=meta).model_dump(mode="json")

        return Publication(publish=publish, downstream=(stage_spec(manifest.session_id, manifest.revision_id, "score", digest, manifest),))

    def _claims_output(self, manifest: ReportManifest) -> ClaimsOutput:
        evidence, text = evidence_input(manifest), manifest.listing.text
        raw: object | None = None
        failed = False
        if self.claims_model is not None:
            try:
                raw = self.claims_model(text, evidence)
            except Exception:  # the seam is untrusted; never log its payload
                logging.warning("Claim checker unavailable; using deterministic claims")
                failed = True
        output = resolve_claims(text, evidence, self.config.claims, raw)
        if failed:
            output = output.model_copy(update={"source": "fallback", "errors": (MODEL_UNAVAILABLE,)})
        return output

    # ------------------------------------------------------------------ score

    def score(self, lease: Lease) -> Publication:
        with self.factory() as db:
            located = self._locate(db, lease, lock=False)
            if located is None or not located[1]:
                return Publication()
            manifest, digest = self._frozen(located[0])
        payload = score_payload(compute_score(manifest, digest, self.config))

        def publish(db: DBSession) -> None:
            revision = self._locked_for_publish(db, lease)
            if revision is None:
                return
            self._expect(revision, digest)
            if revision.score_result is None:
                revision.score_result = payload
            elif revision.score_result != payload:
                raise SnapshotIntegrityError("stored score differs from the recomputed score")

        return Publication(publish=publish, downstream=(stage_spec(manifest.session_id, manifest.revision_id, "narrate", digest, manifest),))

    # ------------------------------------------------------------------ narrate

    def narrate(self, lease: Lease) -> Publication:
        with self.factory() as db:
            located = self._locate(db, lease, lock=False)
            if located is None or not located[1]:
                return Publication()
            revision = located[0]
            manifest, digest = self._frozen(revision)
            try:
                if revision.score_result is None or revision.paid_narration is None:
                    raise SnapshotIncomplete("narration needs the stored score and claims")
                score = StoredScore.model_validate(revision.score_result)
                paid_in = StoredPaidNarration.model_validate(revision.paid_narration)
                if score.manifest_hash != digest or paid_in.manifest_hash != digest:
                    raise SnapshotIntegrityError("stage output belongs to another manifest")
            except (SnapshotIntegrityError, SnapshotIncomplete, ValidationError):
                raise _terminal() from None
            try:
                load_snapshot(db, revision, config=self.config)
            except (SnapshotIntegrityError, SnapshotIncomplete, ValidationError, ValueError):
                raise _terminal() from None
            claim_rows = stored_claims(db, revision.id)
        findings = {row.id: row for row in manifest.all_findings}
        claims = [ClaimResult(claim_index=item.claim_index, text=item.text, verdict=item.verdict, finding_ids=item.finding_ids,
                              evidence_frame_keys=tuple(sorted({findings[fid].evidence.frame_key for fid in item.finding_ids})),
                              rationale=item.rationale) for item in claim_rows]
        try:
            free, paid = self._narrations(manifest, score, claims)
        except NarrationInputError:
            raise _terminal() from None
        briefing = paid_in.briefing or self._briefing(manifest)  # a retry keeps the first provider answers
        free_payload = StoredFreeNarration(manifest_hash=digest, outcome=free).model_dump(mode="json")
        paid_payload = paid_in.model_copy(update={"narration": paid, "briefing": briefing}).model_dump(mode="json")

        def publish(db: DBSession) -> None:
            revision = self._locked_for_publish(db, lease)
            if revision is None:
                return
            self._expect(revision, digest)
            if revision.score_result != score.model_dump(mode="json") or revision.paid_narration != paid_in.model_dump(mode="json"):
                raise SnapshotIntegrityError("narration inputs changed before publication")
            if stored_claims(db, revision.id) != claim_rows:
                raise SnapshotIntegrityError("claims changed between the narrate handler and its publication")
            if revision.free_narration is None:
                revision.free_narration = free_payload
            elif revision.free_narration != free_payload:
                raise SnapshotIntegrityError("stored free narration differs from the recomputed narration")
            if (revision.paid_narration or {}).get("narration") is None:
                revision.paid_narration = paid_payload
            elif revision.paid_narration != paid_payload:
                raise SnapshotIntegrityError("stored paid narration differs from the recomputed narration")

        return Publication(publish=publish, downstream=(stage_spec(manifest.session_id, manifest.revision_id, "render", digest, manifest),))

    def _briefing(self, manifest: ReportManifest) -> StoredBriefing:
        """Advisory, VIN check and vibration series. Provider calls happen here, outside any transaction."""
        rig = None
        if any(row.kind == "rig_telemetry" for row in manifest.sources):
            with self.factory() as db:
                row = db.scalar(select(EvidenceSource).where(EvidenceSource.session_id == manifest.session_id,
                                                             EvidenceSource.kind == "rig_telemetry"))
                if row is not None and canonical_hash(row.data) == row.data_sha256:
                    try:
                        rig = RigTelemetryData.model_validate(row.data)
                    except ValidationError:
                        rig = None
        return build_briefing(manifest.sources, rig, str(manifest.session_id), self.settings)

    def _narration_audio(self, snapshot: RevisionSnapshot) -> NarrationAudioMeta | None:
        return _narration_audio_for(snapshot, self.storage)

    def _narrations(self, manifest: ReportManifest, score: StoredScore, claims: Sequence[ClaimResult]) -> tuple[NarrationOutcome, NarrationOutcome]:
        config = self.config.narration
        part1, everything = evidence_input(manifest, (1,)), evidence_input(manifest)  # free scope sees Part 1 only
        free_template = narrate_part1(part1, score.scores, config)
        paid_template = narrate_full(everything, score.scores, score.repairs, claims, config)
        free_raw, free_failed = self._narration_model(NarrationScope.FREE, part1, free_template)
        paid_raw, paid_failed = self._narration_model(NarrationScope.PAID, everything, paid_template)
        free = resolve_part1_narration(part1, score.scores, config, free_raw)
        paid = resolve_full_narration(everything, score.scores, score.repairs, claims, config, paid_raw)
        if free_failed:
            free = free.model_copy(update={"source": "fallback", "errors": (MODEL_UNAVAILABLE,)})
        if paid_failed:
            paid = paid.model_copy(update={"source": "fallback", "errors": (MODEL_UNAVAILABLE,)})
        return free, paid

    def _narration_model(self, scope: NarrationScope, evidence: ScoringInput, template: Sequence[NarratedStatement]) -> tuple[Any, bool]:
        if self.narration_model is None:
            return None, False
        try:
            return self.narration_model(scope, evidence, template), False
        except Exception:
            logging.warning("Narrator unavailable; using template narration")
            return None, True

    # ------------------------------------------------------------------ render (the only path to ready)

    def render(self, lease: Lease) -> Publication:
        with self.factory() as db:
            located = self._locate(db, lease, lock=False)
            if located is None or not located[1]:
                return Publication()
            revision = located[0]
            self._frozen(revision)
            try:
                _assemble(load_snapshot(db, revision, config=self.config), validate_html=True)
            except (SnapshotIntegrityError, SnapshotIncomplete, ValidationError, ValueError):
                raise _terminal() from None
            digest = str(revision.manifest_hash)
            snapshot = load_snapshot(db, revision, config=self.config)
        audio_meta = self._narration_audio(snapshot)  # provider call outside the read transaction

        def publish(db: DBSession) -> None:
            revision = self._locked_for_publish(db, lease)
            if revision is None:
                return
            self._expect(revision, digest)
            if audio_meta is not None:
                stored = StoredPaidNarration.model_validate(revision.paid_narration)
                if stored.briefing is not None and stored.briefing.narration_audio is None:
                    revision.paid_narration = stored.model_copy(update={"briefing": stored.briefing.model_copy(
                        update={"narration_audio": audio_meta})}).model_dump(mode="json")
            _assemble(load_snapshot(db, revision, config=self.config), validate_html=False)  # re-validated against the locked rows
            revision.rendered_at = db.scalar(select(func.clock_timestamp()))
            revision.state = ReportState.READY

        return Publication(publish=publish)


def _narration_audio_for(snapshot: RevisionSnapshot, storage: ObjectStorage | None) -> NarrationAudioMeta | None:
    """Spoken version of the paid narration, cached by input hash. Never fails the report."""
    from app.briefing.narration_audio import NarrationAudioError, build_narration_audio
    from app.services.auxiliary_providers import LIVE_BUDGET_UNAVAILABLE, live_auxiliary_requested
    briefing = None if snapshot.paid_narration is None else snapshot.paid_narration.briefing
    if storage is None or briefing is None or briefing.narration_audio is not None:
        return None
    if live_auxiliary_requested("VOICE_PROVIDER"):
        logging.warning(LIVE_BUDGET_UNAVAILABLE)
        return None
    try:
        ready = replace(snapshot, state=ReportState.READY, rendered_at=datetime.now(timezone.utc))
        paid = build_reports(ready)[1]
        assert isinstance(paid, PaidReport)
        return NarrationAudioMeta.model_validate(
            build_narration_audio(paid, snapshot.session_id, storage, simulated=snapshot.manifest.provenance != "live").metadata())
    except (NarrationAudioError, ValidationError, ValueError, OSError):
        logging.warning("Narration audio unavailable; the report publishes without it")
        return None


def _assemble(snapshot: RevisionSnapshot, *, validate_html: bool) -> None:
    """Both scope DTOs must build from the stored snapshot; optionally prove the renderer accepts them."""
    ready = replace(snapshot, state=ReportState.READY, rendered_at=datetime.now(timezone.utc))
    free, paid = build_reports(ready)
    if validate_html:
        render_report(free)
        render_report(paid)


def _write_claims(db: DBSession, revision: ReportRevision, manifest: ReportManifest, claims: Sequence[ClaimResult]) -> None:
    """Claims + evidence links for this revision; the association foreign keys bind them to its findings."""
    frames = {row.id: row.evidence.frame_key for row in manifest.all_findings}
    for item in claims:
        row = Claim(id=uuid4(), session_id=revision.session_id, report_revision_id=revision.id, claim_index=item.claim_index,
                    text=item.text, verdict=item.verdict, rationale=item.rationale)
        db.add(row)
        db.flush()
        for finding_id in item.finding_ids:
            db.add(ClaimEvidence(claim_id=row.id, finding_id=finding_id, report_revision_id=revision.id,
                                 session_id=revision.session_id, frame_key=frames[finding_id]))
    db.flush()


def install_report_handlers(registry, factory: sessionmaker[DBSession], settings: Settings,  # type: ignore[no-untyped-def]
                            config: ReportConfig | None = None, *, claims_model: ClaimsModel | None = None,
                            narration_model: NarrationModel | None = None,
                            storage: ObjectStorage | None = None) -> ReportStages:
    """Register claims, score, narrate and render. Offline by default: no model seam is installed."""
    stages = ReportStages(factory, settings, config, claims_model=claims_model, narration_model=narration_model, storage=storage)
    for name in REPORT_STAGE_NAMES:
        registry.register(name, getattr(stages, name))
    return stages
