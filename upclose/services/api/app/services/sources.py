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
from app.adapters.storage import ObjectStorage
from app.contracts.enums import UploadState
from app.contracts.sources import (EngineAudioSource, ObdSnapshotSource, RigTelemetrySource, SourceListItem,
                                   SourceListResponse, SourceRequest, SourceResponse, SourceSummary)
from app.domain.sources import (SourcesConfig, load_sources_config, pending_audio_summary, summarize_obd,
                                summarize_rig)
from app.models import AudioUpload, EvidenceSource, Session
from app.pipeline.orchestration import CONFIG_DIR, canonical_hash
from app.settings import Settings
from .auth import owner_session
from .audio_uploads import AudioUploadService, queue_audio_processing
from .problems import conflict, validation


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
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage | None = None):
        self.sessions = sessions
        self.settings = settings
        self.audio = None if storage is None else AudioUploadService(sessions, settings, storage)

    def attach(self, token: str, session_id: UUID, request: SourceRequest) -> SourceResponse:
        if isinstance(request, EngineAudioSource):
            return self._attach_audio(token, session_id, request)
        with self.sessions.begin() as db:  # authority and cheap state check before any heavy work
            self._capturing(owner_session(db, token, session_id))
        summary = summarize(request)
        data = request.data.model_dump(mode="json")
        if data.get("model_verdict") is None:
            data.pop("model_verdict", None)  # a recording without a verdict hashes exactly as before
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

    def _attach_audio(self, token: str, session_id: UUID, request: EngineAudioSource) -> SourceResponse:
        if self.audio is None:
            raise conflict()
        with self.sessions.begin() as db:  # authority and cheap state check before the object is read
            self._capturing(owner_session(db, token, session_id))
        self.audio.verify_clip(token, session_id, request.data.upload_id, request.data.sha256)
        config = load_sources()
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            self._capturing(owner)
            upload = db.scalar(select(AudioUpload).where(AudioUpload.id == request.data.upload_id,
                                                         AudioUpload.session_id == owner.id).with_for_update())
            if upload is None or upload.state != UploadState.ACCEPTED:
                raise conflict()
            row = db.scalar(select(EvidenceSource).where(EvidenceSource.session_id == owner.id,
                                                         EvidenceSource.kind == "engine_audio"))
            if upload.processing_state != "pending" and row is not None and row.data.get("upload_id") == str(upload.id):
                return SourceResponse(source_id=row.id, kind="engine_audio", data_sha256=row.data_sha256,
                                      summary=row.summary)  # the same clip posted again: nothing to redo
            if upload.processing_state != "pending":
                raise conflict()
            for other in db.scalars(select(AudioUpload).where(AudioUpload.session_id == owner.id,
                                                              AudioUpload.id != upload.id,
                                                              AudioUpload.state == UploadState.ACCEPTED)):
                other.state, other.reason = UploadState.ABORTED, "SUPERSEDED"  # its job becomes a no-op
            data = {"upload_id": str(upload.id), "sha256": request.data.sha256, "bytes": upload.expected_bytes,
                    "content_type": upload.content_type}
            summary = pending_audio_summary(request.provenance, upload.expected_bytes, upload.content_type, config)
            if row is None:
                row = EvidenceSource(session_id=owner.id, kind="engine_audio")
                db.add(row)
            row.provenance, row.device, row.captured_at = request.provenance, request.device, request.captured_at
            row.data, row.data_sha256 = data, source_data_hash(data)
            row.summary = summary.model_dump(mode="json")
            upload.raw_sha256 = request.data.sha256
            db.flush()
            queue_audio_processing(db, upload)
            return SourceResponse(source_id=row.id, kind="engine_audio", data_sha256=row.data_sha256, summary=summary)

    def list_sources(self, token: str, session_id: UUID) -> SourceListResponse:
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
