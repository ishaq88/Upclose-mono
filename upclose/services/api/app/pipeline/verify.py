"""Verify stage (T05b, fixture Verifier only): candidates -> validated verdicts -> resolved findings.

A `verify` job's subject is one `finding`, or a `duplicate_group` (an observation row naming findings with
an ambiguous overlap that must be judged together). The handler:

1. reads the still-`candidate` findings (a short read-only transaction),
2. calls the Verifier outside any transaction. The verifier returns RAW output (JSON text or a mapping),
   which is parsed with `parse_verdict`, checked as a group and resolved with `resolve_group`; only then
   is anything written (invariant: validate every model response before database writes),
3. publishes inside the lease-fenced, session-locked transaction: `apply_resolution` results onto the
   finding rows, a `verification_provenance` observation per finding (rubric ids/hashes + frame
   provenance), then the part barrier.

A malformed or invalid verdict uses the same three-attempt budget as every stage; on the last attempt the
finding(s) become `unverified` (coverage partial, never clean) rather than the job stalling. A job that dies
without publishing is handled by the reconciler/part barrier the same way.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.meta.parser import ProviderFailure
from app.contracts.enums import FindingStatus, PartState, SessionState
from app.contracts.evidence import Finding
from app.domain.rubrics import render_rubric_text
from app.domain.verification import (FindingResolution, VerdictRejected, apply_resolution, parse_verdict,
                                     resolve_group)
from app.jobs.queue import Failure, Lease
from app.jobs.worker import Publication, StageFailure
from app.models import Finding as FindingRow, Observation, Part, PipelineRun, Session
from app.pipeline.ingest import PublicationRejected
from app.pipeline.orchestration import (VERIFICATION_FAILED, VERIFICATION_PROVENANCE, VERIFY_GROUP,
                                        OrchestrationContext, advance_part, apply_to_row, to_contract_finding)

from app.services.budgets import BudgetError

if TYPE_CHECKING:
    from app.adapters.meta.verifier import MetaSparkVerifier, PreparedVerification
    from app.services.budgets import BudgetService

MAX_ATTEMPTS = 3
_LIVE = (SessionState.CAPTURING, SessionState.FINALIZED)

# Stable reasons recorded in `verification_failed` observations.
CONFIG_MISMATCH = "VERIFY_CONFIG_MISMATCH"
RUBRIC_UNAVAILABLE = "VERIFY_RUBRIC_UNAVAILABLE"
VERDICT_INVALID = "VERIFY_VERDICT_INVALID"
VERIFIER_ERROR = "VERIFY_VERIFIER_ERROR"


class RawVerifier(Protocol):
    """Returns untrusted verdict output: JSON text or a mapping. Never a pre-validated object."""
    def verify_raw(self, candidate: Finding, context: tuple[Finding, ...], rubric: str) -> str | bytes | Mapping[str, object]: ...


class FixtureVerifier:
    """Synthetic verifier: confirms each candidate at severity 2. It proves orchestration, not perception.
    `overrides` maps a concept value to a callable returning the raw verdict, for tests."""

    def __init__(self, overrides: Mapping[str, object] | None = None) -> None:
        self.overrides = dict(overrides or {})

    def verify_raw(self, candidate: Finding, context: tuple[Finding, ...], rubric: str) -> str | bytes | Mapping[str, object]:
        custom = self.overrides.get(candidate.concept.value)
        if callable(custom):
            return custom(candidate, context, rubric)
        return {"finding_id": str(candidate.id), "verdict": "confirmed", "concept": candidate.concept.value,
                "severity": 2, "confidence": None,
                "evidence_note": "Synthetic fixture verdict; no real inference was performed.",
                "subzone": None, "component_id": None, "duplicate_of": None, "flags": []}


class LivePendingVerifier:
    # Stage-1 live seam: never invents a model verdict. Live candidates stay unverified
    # (coverage partial) until a Spark verifier reserves budget and validates verdicts.
    def verify_raw(self, candidate: Finding, context: tuple[Finding, ...], rubric: str) -> dict[str, object]:
        return {"finding_id": str(candidate.id), "verdict": "unverifiable", "concept": candidate.concept.value,
                "severity": None, "confidence": None,
                "evidence_note": ("Live verification unavailable; candidate preserved unverified. " + candidate.evidence_note)[:240],
                "subzone": None, "component_id": None, "duplicate_of": None, "flags": []}


class SparkRawVerifier:
    """Live Spark verdicts behind per-candidate budget reservation; output stays untrusted raw JSON."""
    requires_lease = True

    def __init__(self, spark: "MetaSparkVerifier", budget_service: "BudgetService") -> None:
        self.spark, self.budget = spark, budget_service

    def verify_raw(self, candidate: Finding, context: tuple[Finding, ...], rubric: str, *,
                   lease: Lease) -> Mapping[str, object]:
        from app.adapters.meta.verifier import MODEL
        from app.services.budgets import (PRICE_VERSION as BUDGET_PRICE_VERSION, ReservationRequest, UsageUnits,
                                          documented_prices, price_usage)

        if lease.session_id is None:
            raise ProviderFailure("budget_not_reserved")
        prepared = self.spark.prepare(candidate, context, rubric)
        attempt_id = uuid5(NAMESPACE_URL, f"budget:{lease.job_id}:{lease.attempt}:verify:{candidate.id}")
        dispatched = False

        def grant(item: "PreparedVerification", _request_id: UUID) -> bool:
            nonlocal dispatched
            units = UsageUnits(input_tokens=item.input_token_bound or 0, output_tokens=item.max_output_tokens)
            self.budget.reserve(ReservationRequest(lease.session_id, lease.job_id, attempt_id, lease.token,
                                                   lease.deadline, MODEL, BUDGET_PRICE_VERSION, units))
            dispatched = self.budget.mark_dispatched(attempt_id, lease.token)
            return dispatched

        try:
            response = self.spark.execute(prepared, uuid5(NAMESPACE_URL, f"verify:{lease.job_id}:{lease.attempt}:{candidate.id}"),
                                          grant=grant)
        except ProviderFailure as error:
            logging.warning("Spark request failed code=%s", error.code)
            if dispatched:
                try:
                    self.budget.mark_unknown(attempt_id, failure_code=error.code, http_status=error.http_status)
                except BudgetError:
                    pass
            raise
        if response.input_tokens is None or response.output_tokens is None:
            self.budget.mark_unknown(attempt_id)  # unknown billing stays reserved, never free
        else:
            actual = UsageUnits(input_tokens=response.input_tokens, cached_input_tokens=response.cached_input_tokens or 0,
                                output_tokens=response.output_tokens)
            try:
                self.budget.settle(attempt_id, actual_units=actual, actual_micro_usd=price_usage(MODEL, actual, documented_prices()),
                                   outcome="succeeded", evidence_code="meta-usage-reported")
            except BudgetError:
                logging.warning("Budget settlement deferred; ledger keeps unknown charge.")
        return response.raw_verdict


class _Retryable(Exception):
    def __init__(self, reason: str, queue_code: str, *, retry: bool = True) -> None:
        super().__init__(reason)
        self.reason, self.queue_code, self.retry = reason, queue_code, retry


class VerifyHandler:
    def __init__(self, factory: sessionmaker[DBSession], ctx: OrchestrationContext, *,
                 verifier: RawVerifier | None = None) -> None:
        self.factory, self.ctx = factory, ctx
        self.verifier: RawVerifier = verifier or FixtureVerifier()

    def __call__(self, lease: Lease) -> Publication:
        loaded = self._load(lease)
        if isinstance(loaded, Publication):
            return loaded
        run_id, part_id, members = loaded
        try:
            resolutions = self._verify(members, lease)
        except _Retryable as error:
            if error.retry and lease.attempt < MAX_ATTEMPTS:
                raise StageFailure(Failure(error.queue_code)) from None
            return self._unverified(lease, run_id, part_id, members, error.reason)  # budget spent: fail closed
        return self._resolved(lease, run_id, part_id, members, resolutions)

    # ---- phase 1

    def _load(self, lease: Lease) -> Publication | tuple[UUID, UUID, list[Finding]]:
        terminal = StageFailure(Failure("INVALID_INPUT", retryable=False))
        try:
            subject = UUID(lease.subject_id)
        except ValueError:
            raise terminal from None
        if lease.session_id is None or lease.stage != "verify":
            raise terminal
        with self.factory() as db:
            owner = db.get(Session, lease.session_id)
            if owner is None or owner.state not in _LIVE:
                raise terminal
            if lease.subject_kind == "finding":
                ids = [subject]
            elif lease.subject_kind == "duplicate_group":
                group = db.scalar(select(Observation).where(Observation.id == subject, Observation.session_id == lease.session_id,
                                                            Observation.kind == VERIFY_GROUP))
                if group is None:
                    raise terminal
                ids = [UUID(item) for item in group.value["finding_ids"]]
            else:
                raise terminal
            rows = list(db.scalars(select(FindingRow).where(FindingRow.id.in_(ids), FindingRow.session_id == lease.session_id)
                                   .order_by(FindingRow.id)))
            if len(rows) != len(ids):
                raise terminal
            if all(row.status != FindingStatus.CANDIDATE for row in rows):
                return Publication()  # already resolved by an earlier attempt: converge
            if any(row.status != FindingStatus.CANDIDATE for row in rows) or len({r.run_id for r in rows}) != 1 \
                    or len({r.part_id for r in rows}) != 1:
                raise terminal
            run = db.scalar(select(PipelineRun).where(PipelineRun.id == rows[0].run_id, PipelineRun.session_id == lease.session_id))
            part = db.scalar(select(Part).where(Part.id == rows[0].part_id, Part.session_id == lease.session_id))
            if run is None or part is None or part.state != PartState.PROCESSING:
                raise terminal
            members = [to_contract_finding(row, part) for row in rows]
            hashes = self.ctx.config_hashes
            if any(run.config_hashes.get(key) != hashes[key] for key in ("rubrics", "verifier", "prompts")):
                return self._unverified(lease, run.id, part.id, members, CONFIG_MISMATCH)
            return run.id, part.id, members

    # ---- phase 2: verifier call and validation (no transaction open)

    def _verify(self, members: list[Finding], lease: Lease) -> tuple[FindingResolution, ...]:
        rubrics = self.ctx.config.rubrics
        context = tuple(members)
        verdicts = []
        extra = {"lease": lease} if getattr(self.verifier, "requires_lease", False) else {}
        try:
            for candidate in members:
                rubric = rubrics.get(candidate.concept)
                if rubric is None:
                    raise _Retryable(RUBRIC_UNAVAILABLE, "UNSUPPORTED")  # same budget, then unverified
                raw = self.verifier.verify_raw(candidate, context, render_rubric_text(rubric), **extra)
                verdicts.append(parse_verdict(raw))
            return resolve_group(members, verdicts, rubrics)
        except VerdictRejected:
            raise _Retryable(VERDICT_INVALID, "SCHEMA_INVALID") from None
        except _Retryable:
            raise
        except ProviderFailure as error:
            # A billed response we rejected (or absent budget/access) would repeat its charge: fail closed now.
            raise _Retryable(VERIFIER_ERROR, "TRANSIENT", retry=error.retryable) from None
        except Exception as error:
            logging.warning("Verifier call failed: %s", type(error).__name__)
            raise _Retryable(VERIFIER_ERROR, "TRANSIENT") from None

    # ---- phase 3

    def _resolved(self, lease: Lease, run_id: UUID, part_id: UUID, members: list[Finding],
                  resolutions: Sequence[FindingResolution]) -> Publication:
        ctx = self.ctx
        by_id = {item.id: item for item in members}
        results = {res.finding_id: apply_resolution(by_id[res.finding_id], res) for res in resolutions}

        def publish(db: DBSession) -> None:
            rows = self._lock(db, lease, part_id, list(results))
            if rows is None:
                return
            for row in rows:
                apply_to_row(row, results[row.id])
            db.flush()  # duplicate_of targets exist; checks require status+link in the same row
            for res in sorted(resolutions, key=lambda item: item.finding_id):
                db.add(Observation(session_id=lease.session_id, run_id=run_id, kind=VERIFICATION_PROVENANCE,
                                   frame_key=res.provenance.frame_key,
                                   value={"finding_id": str(res.finding_id), "reason": res.reason.value,
                                          "confidence_label": res.confidence_label,
                                          "provenance": json.loads(res.provenance.model_dump_json())}))
            db.flush()
            advance_part(db, ctx, part_id, lease.job_id)

        return Publication(publish=publish)

    def _unverified(self, lease: Lease, run_id: UUID, part_id: UUID, members: list[Finding], reason: str) -> Publication:
        ctx = self.ctx

        def publish(db: DBSession) -> None:
            rows = self._lock(db, lease, part_id, [item.id for item in members])
            if rows is None:
                return
            for row in rows:
                row.status = FindingStatus.UNVERIFIED
                db.add(Observation(session_id=lease.session_id, run_id=run_id, kind=VERIFICATION_FAILED,
                                   value={"finding_id": str(row.id), "reason": reason}))
            db.flush()
            advance_part(db, ctx, part_id, lease.job_id)

        return Publication(publish=publish)

    @staticmethod
    def _lock(db: DBSession, lease: Lease, part_id: UUID, ids: list[UUID]) -> list[FindingRow] | None:
        part = db.scalar(select(Part).where(Part.id == part_id, Part.session_id == lease.session_id).with_for_update())
        rows = list(db.scalars(select(FindingRow).where(FindingRow.id.in_(ids), FindingRow.session_id == lease.session_id)
                               .order_by(FindingRow.id).with_for_update()))
        if part is None or len(rows) != len(ids) or part.state != PartState.PROCESSING:
            raise PublicationRejected("findings or part changed since the attempt started")
        if all(row.status != FindingStatus.CANDIDATE for row in rows):
            return None  # converged: an earlier publication already resolved them
        if any(row.status != FindingStatus.CANDIDATE for row in rows):
            raise PublicationRejected("group partially resolved")
        return rows


def register_verify_handler(registry, factory: sessionmaker[DBSession], ctx: OrchestrationContext, *,  # type: ignore[no-untyped-def]
                            verifier: RawVerifier | None = None) -> VerifyHandler:
    handler = VerifyHandler(factory, ctx, verifier=verifier)
    registry.register("verify", handler)
    return handler
