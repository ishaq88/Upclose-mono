"""Persistence of the dashboard-light reading: an observation of the dash part's assessment run.

The frozen form (`FrozenDash`) is read by the manifest builder, so the Mechanical score replays from it.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.contracts.mechanical import FrozenDash, FrozenDashLit
from app.domain.dash_lights import DashAssessment
from app.models import Observation

DASH_ASSESSMENT = "dash_assessment"


def frozen_from_assessment(assessment: DashAssessment, crop_sha256: str | None) -> FrozenDash:
    return FrozenDash(state=assessment.state, reason=assessment.reason,
                      lit=tuple(FrozenDashLit(indicator=row.indicator, severity=row.severity, confidence=row.confidence,
                                              deduction=row.deduction) for row in assessment.lit),
                      deduction=assessment.deduction, flags=assessment.flags, config_hash=assessment.config_hash,
                      crop_sha256=crop_sha256)


def store_dash(db: DBSession, session_id: UUID, run_id: UUID, dash: FrozenDash, crop_key: str | None = None) -> None:
    """Idempotent: one assessment per session; a second call for the same run keeps the first."""
    existing = db.scalar(select(Observation).where(Observation.session_id == session_id, Observation.kind == DASH_ASSESSMENT))
    if existing is not None:
        return
    db.add(Observation(session_id=session_id, run_id=run_id, kind=DASH_ASSESSMENT,
                       value=dash.model_dump(mode="json"), frame_key=crop_key, reason=dash.reason))
    db.flush()


def load_frozen_dash(db: DBSession, session_id: UUID) -> FrozenDash | None:
    row = db.scalar(select(Observation).where(Observation.session_id == session_id, Observation.kind == DASH_ASSESSMENT)
                    .order_by(Observation.created_at).limit(1))
    if row is None:
        return None
    try:
        return FrozenDash.model_validate(row.value)
    except ValidationError:
        return None
