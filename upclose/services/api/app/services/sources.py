"""Evidence-source ingest: owner-authenticated, capturing sessions only, one source per kind.

Derived summaries are computed before the session lock is taken (authority is checked first), so a
large trace never holds the lock for the duration of the math. State is rechecked under the lock.
"""
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.enums import SessionState
from app.contracts.sources import (ObdSnapshotSource, RigTelemetrySource, SourceListItem, SourceListResponse,
                                   SourceRequest, SourceResponse, SourceSummary)
from app.domain.sources import SourcesConfig, load_sources_config, summarize_obd, summarize_rig
from app.models import EvidenceSource, Session
from app.pipeline.orchestration import CONFIG_DIR, canonical_hash
from app.settings import Settings
from .auth import owner_session
from .problems import conflict


@lru_cache(maxsize=1)
def load_sources() -> SourcesConfig:
    return load_sources_config(Path(CONFIG_DIR) / "sources.v1.json")


def source_data_hash(data: object) -> str:
    """Hash of the canonical JSON of the stored `data`; the manifest binds its summary to this."""
    return canonical_hash(data)


def summarize(request: RigTelemetrySource | ObdSnapshotSource) -> SourceSummary:
    config = load_sources()
    if isinstance(request, RigTelemetrySource):
        return summarize_rig(request.data, request.provenance, config)
    return summarize_obd(request.data, request.provenance, config)


class SourceService:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings):
        self.sessions = sessions
        self.settings = settings

    def attach(self, token: str, session_id: UUID, request: SourceRequest) -> SourceResponse:
        with self.sessions.begin() as db:  # authority and cheap state check before any heavy work
            self._capturing(owner_session(db, token, session_id))
        summary = summarize(request)
        data = request.data.model_dump(mode="json")
        digest = source_data_hash(data)
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            self._capturing(owner)
            row = db.scalar(select(EvidenceSource).where(EvidenceSource.session_id == owner.id,
                                                         EvidenceSource.kind == request.kind))
            if row is None:
                row = EvidenceSource(session_id=owner.id, kind=request.kind)
                db.add(row)
            row.provenance, row.device, row.captured_at = request.provenance, request.device, request.captured_at
            row.data, row.data_sha256 = data, digest
            row.summary = summary.model_dump(mode="json")
            db.flush()
            return SourceResponse(source_id=row.id, kind=request.kind, data_sha256=digest, summary=summary)

    def list(self, token: str, session_id: UUID) -> SourceListResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id)
            rows = db.scalars(select(EvidenceSource).where(EvidenceSource.session_id == owner.id)
                              .order_by(EvidenceSource.kind))
            return SourceListResponse(sources=tuple(SourceListItem(
                source_id=row.id, kind=row.kind, provenance=row.provenance, device=row.device,
                captured_at=row.captured_at, data_sha256=row.data_sha256, summary=row.summary) for row in rows))

    @staticmethod
    def _capturing(owner: Session) -> None:
        if owner.state != SessionState.CAPTURING:
            raise conflict()
