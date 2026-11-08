"""PostgreSQL persistence schema. Domain calculations and provider I/O live elsewhere."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey,
    ForeignKeyConstraint, Index, Integer, MetaData, Numeric, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.contracts.enums import (ClaimVerdict, ComponentId, Concept, CoverageState,
    FindingStatus, PartState, PrivacyState, ReportState, SessionState, Subzone, UploadState, Zone)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={"ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"})


def enum_type(enum: type[StrEnum], name: str) -> Enum:
    return Enum(enum, name=name, native_enum=False, create_constraint=True,
                values_callable=lambda cls: [item.value for item in cls], validate_strings=True)


def states(name: str, *values: str) -> Enum:
    return Enum(*values, name=name, native_enum=False, create_constraint=True, validate_strings=True)


def hash_column(nullable: bool = False) -> Mapped[str]:
    return mapped_column(String(64), nullable=nullable)


class Record:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Session(Record, Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Nullable by design: rows created before accounts existed (and anonymous
    # buyer sessions) keep NULL. Non-NULL links the inspection to its account (T15a).
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    account: Mapped[Account | None] = relationship(back_populates="sessions")
    purpose: Mapped[str] = mapped_column(states("session_purpose", "buyer"), server_default="buyer")
    capture_mode: Mapped[str] = mapped_column(states("capture_mode", "guided", "single_video"), server_default="guided")
    consent_version: Mapped[str] = mapped_column(String(128))
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    listing_text: Mapped[str | None] = mapped_column(Text)
    listing_version: Mapped[int] = mapped_column(Integer, server_default="1")
    state: Mapped[SessionState] = mapped_column(enum_type(SessionState, "session_state"), server_default="capturing")
    vehicle_label: Mapped[str | None] = mapped_column(String(80))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parts: Mapped[list[Part]] = relationship(back_populates="session", foreign_keys="Part.session_id")
    __table_args__ = (CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash"),
        CheckConstraint("length(consent_version) > 0", name="consent_version"),
        CheckConstraint("listing_text IS NULL OR length(listing_text) <= 10000", name="listing_length"),
        CheckConstraint("listing_version >= 1", name="listing_version"),
        CheckConstraint("vehicle_label IS NULL OR length(vehicle_label) BETWEEN 1 AND 80", name="vehicle_label_length"))


class Part(Record, Base):
    __tablename__ = "parts"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    part_no: Mapped[int] = mapped_column(Integer)
    zone: Mapped[Zone] = mapped_column(enum_type(Zone, "part_zone"))
    state: Mapped[PartState] = mapped_column(enum_type(PartState, "part_state"), server_default="pending")
    active_upload_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_video_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    raw_key: Mapped[str | None] = mapped_column(String(1024))
    raw_sha256: Mapped[str | None] = hash_column(nullable=True)
    quality: Mapped[dict | None] = mapped_column(JSONB)
    failure_reason: Mapped[str | None] = mapped_column(String(240))
    session: Mapped[Session] = relationship(back_populates="parts", foreign_keys=[session_id])
    __table_args__ = (UniqueConstraint("session_id", "part_no"), UniqueConstraint("id", "session_id"),
        CheckConstraint("part_no BETWEEN 1 AND 8", name="part_no"),
        CheckConstraint("zone = (ARRAY['driver_side','passenger_side','front_rear_body','tires','dash_electrical','engine_bay','underbody','interior'])[part_no]", name="canonical_zone"),
        CheckConstraint("raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'", name="raw_hash"),
        CheckConstraint("state != 'failed' OR length(failure_reason) > 0 AND failure_reason IS NOT NULL", name="failed_reason"),
        ForeignKeyConstraint(["source_video_id", "session_id"], ["video_uploads.id", "video_uploads.session_id"], name="fk_parts_source_video"),
        ForeignKeyConstraint(["active_upload_id", "id", "session_id"], ["uploads.id", "uploads.part_id", "uploads.session_id"], use_alter=True, name="fk_parts_active_upload"),
        ForeignKeyConstraint(["raw_key", "id", "session_id"], ["uploads.object_key", "uploads.part_id", "uploads.session_id"], use_alter=True, name="fk_parts_raw_upload"))


class Upload(Record, Base):
    __tablename__ = "uploads"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    part_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    provider_upload_id: Mapped[str] = mapped_column(String(1024), unique=True)
    expected_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(128))
    state: Mapped[UploadState] = mapped_column(enum_type(UploadState, "upload_state"), server_default="open")
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("id", "part_id", "session_id"), UniqueConstraint("object_key", "part_id", "session_id"),
        ForeignKeyConstraint(["part_id", "session_id"], ["parts.id", "parts.session_id"]),
        CheckConstraint("expected_bytes BETWEEN 1 AND 209715200", name="expected_bytes"),
        CheckConstraint("content_type IN ('video/mp4','video/webm','video/quicktime')", name="content_type"),
        CheckConstraint("length(object_key) > 0 AND object_key !~ '[:?#]'", name="object_key"),
        CheckConstraint("length(provider_upload_id) > 0", name="provider_id"),
        CheckConstraint("state != 'accepted' OR accepted_at IS NOT NULL", name="accepted_timestamp"),
        Index("uq_uploads_active_part", "part_id", unique=True, postgresql_where=text("state IN ('open','completing')")))


class VideoUpload(Record, Base):
    __tablename__ = "video_uploads"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    provider_upload_id: Mapped[str] = mapped_column(String(1024), unique=True)
    expected_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(128))
    state: Mapped[UploadState] = mapped_column(enum_type(UploadState, "video_upload_state"), server_default="open")
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    segments: Mapped[list[dict]] = mapped_column(JSONB, server_default="[]")
    processing_state: Mapped[str] = mapped_column(states("video_processing_state", "pending", "queued", "ready", "failed"), server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(240))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    raw_sha256: Mapped[str | None] = hash_column(nullable=True)
    __table_args__ = (UniqueConstraint("id", "session_id"),
        CheckConstraint("expected_bytes BETWEEN 1 AND 209715200", name="expected_bytes"),
        CheckConstraint("content_type IN ('video/mp4','video/webm','video/quicktime')", name="content_type"),
        CheckConstraint("length(object_key) > 0 AND object_key !~ '[:?#]'", name="object_key"),
        CheckConstraint("length(provider_upload_id) > 0", name="provider_id"),
        CheckConstraint("state != 'accepted' OR accepted_at IS NOT NULL", name="accepted_timestamp"),
        CheckConstraint("jsonb_typeof(segments) = 'array' AND jsonb_array_length(segments) <= 8", name="segments"),
        CheckConstraint("duration_ms IS NULL OR duration_ms BETWEEN 1 AND 180250", name="duration"),
        CheckConstraint("raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'", name="raw_hash"),
        CheckConstraint("processing_state != 'failed' OR reason IS NOT NULL AND length(reason) > 0", name="failure_reason"),
        Index("uq_video_uploads_active_session", "session_id", unique=True,
              postgresql_where=text("state IN ('open','completing','accepted')")))


class PipelineRun(Record, Base):
    __tablename__ = "pipeline_runs"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    part_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    subject_key: Mapped[str] = mapped_column(String(256))
    input_hash: Mapped[str] = hash_column()
    config_hashes: Mapped[dict] = mapped_column(JSONB)
    config_hash: Mapped[str] = hash_column()
    state: Mapped[str] = mapped_column(states("pipeline_state", "pending", "running", "succeeded", "failed"), server_default="pending")
    __table_args__ = (UniqueConstraint("subject_key", "input_hash", "config_hash"), UniqueConstraint("subject_key", "input_hash", "config_hashes"),
        UniqueConstraint("id", "session_id"), UniqueConstraint("id", "part_id", "session_id"),
        ForeignKeyConstraint(["part_id", "session_id"], ["parts.id", "parts.session_id"]),
        CheckConstraint("input_hash ~ '^[0-9a-f]{64}$' AND config_hash ~ '^[0-9a-f]{64}$'", name="hashes"),
        CheckConstraint("jsonb_typeof(config_hashes) = 'object'", name="config_object"),
        CheckConstraint("length(subject_key) > 0", name="subject_key"))


class ConceptRun(Record, Base):
    __tablename__ = "concept_runs"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    part_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    concept: Mapped[Concept] = mapped_column(enum_type(Concept, "concept_run_concept"))
    prompt_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(states("concept_run_state", "pending", "running", "succeeded", "failed", "unsupported", "budget_exhausted"), server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(240))
    provider_call_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    __table_args__ = (UniqueConstraint("run_id", "prompt_id"), UniqueConstraint("id", "run_id", "part_id", "session_id"),
        ForeignKeyConstraint(["run_id", "part_id", "session_id"], ["pipeline_runs.id", "pipeline_runs.part_id", "pipeline_runs.session_id"]),
        ForeignKeyConstraint(["provider_call_id", "session_id"], ["model_calls.id", "model_calls.session_id"], use_alter=True, name="fk_concept_runs_provider_call"),
        CheckConstraint("length(prompt_id) > 0", name="prompt_id"),
        CheckConstraint("state NOT IN ('failed','unsupported','budget_exhausted') OR reason IS NOT NULL AND length(reason) > 0", name="failure_reason"))


class Job(Record, Base):
    __tablename__ = "jobs"
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("sessions.id"), index=True)
    subject_kind: Mapped[str] = mapped_column(states("job_subject", "part_run", "concept_run", "finding", "duplicate_group", "report_revision", "stripe_event", "session"))
    subject_id: Mapped[str] = mapped_column(String(255))
    stage: Mapped[str] = mapped_column(states("job_stage", "ingest", "segment", "verify", "read_dash", "claims", "score", "narrate", "render", "billing_event", "purge", "source_ingest", "vehicle_mask", "audio_ingest"))
    input_hash: Mapped[str] = hash_column()
    config_hash: Mapped[str] = hash_column()
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(states("job_state", "queued", "running", "succeeded", "dead"), server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(String(240))
    __table_args__ = (UniqueConstraint("id", "session_id"),
        CheckConstraint("(subject_kind = 'stripe_event' AND stage = 'billing_event') OR session_id IS NOT NULL", name="owned_subject"),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="attempts"),
        CheckConstraint("input_hash ~ '^[0-9a-f]{64}$' AND config_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'", name="hashes"),
        CheckConstraint("length(subject_id) > 0", name="subject_id"),
        CheckConstraint("state != 'running' OR lease_until IS NOT NULL AND lease_token IS NOT NULL AND worker_id IS NOT NULL AND attempt_started_at IS NOT NULL", name="running_lease"),
        Index("ix_jobs_ready", "available_at", "id", postgresql_where=text("state = 'queued'")),
        Index("ix_jobs_expired_lease", "lease_until", postgresql_where=text("state = 'running'")))


class Report(Record, Base):
    __tablename__ = "reports"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), unique=True)
    share_slug: Mapped[str] = mapped_column(String(128), unique=True)
    current_revision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    share_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    __table_args__ = (UniqueConstraint("id", "session_id"), CheckConstraint("length(share_slug) >= 22", name="share_slug"),
        ForeignKeyConstraint(["current_revision_id", "id", "session_id"], ["report_revisions.id", "report_revisions.report_id", "report_revisions.session_id"], use_alter=True, name="fk_reports_current_revision"))


class ReportRevision(Record, Base):
    __tablename__ = "report_revisions"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    revision_no: Mapped[int] = mapped_column(Integer)
    state: Mapped[ReportState] = mapped_column(enum_type(ReportState, "revision_state"), server_default="building")
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    manifest_hash: Mapped[str | None] = hash_column(nullable=True)
    score_result: Mapped[dict | None] = mapped_column(JSONB)
    free_narration: Mapped[dict | None] = mapped_column(JSONB)
    paid_narration: Mapped[dict | None] = mapped_column(JSONB)
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("report_id", "revision_no"), UniqueConstraint("id", "session_id"), UniqueConstraint("id", "report_id", "session_id"),
        ForeignKeyConstraint(["report_id", "session_id"], ["reports.id", "reports.session_id"]),
        CheckConstraint("revision_no > 0", name="revision_no"),
        CheckConstraint("manifest_hash IS NULL OR manifest_hash ~ '^[0-9a-f]{64}$'", name="manifest_hash"),
        CheckConstraint("state != 'ready' OR manifest IS NOT NULL AND manifest_hash IS NOT NULL AND score_result IS NOT NULL AND rendered_at IS NOT NULL", name="ready_snapshot"))


class Artifact(Record, Base):
    __tablename__ = "artifacts"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    revision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(states("artifact_kind", "raw", "proxy", "frame", "mask", "derivative", "demo"))
    key: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = hash_column()
    privacy_state: Mapped[PrivacyState] = mapped_column(enum_type(PrivacyState, "artifact_privacy"), server_default="pending")
    purge_state: Mapped[str] = mapped_column(states("artifact_purge", "retained", "pending", "purged", "failed"), server_default="retained")
    __table_args__ = (UniqueConstraint("key", "session_id"),
        ForeignKeyConstraint(["revision_id", "session_id"], ["report_revisions.id", "report_revisions.session_id"]),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("length(key) > 0 AND key !~ '[:?#]'", name="key"),
        CheckConstraint("privacy_state != 'approved' OR kind IN ('derivative','demo')", name="approved_derivative_only"))


class Finding(Record, Base):
    __tablename__ = "findings"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    part_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    concept_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    track_id: Mapped[str] = mapped_column(String(128))
    frame_key: Mapped[str] = mapped_column(String(1024))
    frame_index: Mapped[int] = mapped_column(Integer)
    frame_width: Mapped[int] = mapped_column(Integer)
    frame_height: Mapped[int] = mapped_column(Integer)
    bbox: Mapped[dict] = mapped_column(JSONB)
    mask_key: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[FindingStatus] = mapped_column(enum_type(FindingStatus, "finding_status"), server_default="candidate")
    concept: Mapped[Concept] = mapped_column(enum_type(Concept, "finding_concept"))
    subzone: Mapped[Subzone | None] = mapped_column(enum_type(Subzone, "finding_subzone"))
    component_id: Mapped[ComponentId | None] = mapped_column(enum_type(ComponentId, "finding_component"))
    severity: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    evidence_note: Mapped[str] = mapped_column(String(240))
    flags: Mapped[list] = mapped_column(JSONB, server_default="[]")
    duplicate_of: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    part: Mapped[Part] = relationship(Part, primaryjoin="foreign(Finding.part_id) == Part.id", viewonly=True)

    @property
    def zone(self) -> Zone:
        """Canonical persisted Part.zone; findings never maintain a competing zone value."""
        return self.part.zone

    @property
    def part_no(self) -> int:
        return self.part.part_no
    __table_args__ = (UniqueConstraint("concept_run_id", "track_id"), UniqueConstraint("id", "session_id"), UniqueConstraint("id", "part_id", "session_id"), UniqueConstraint("id", "frame_key", "session_id"),
        ForeignKeyConstraint(["concept_run_id", "run_id", "part_id", "session_id"], ["concept_runs.id", "concept_runs.run_id", "concept_runs.part_id", "concept_runs.session_id"]),
        ForeignKeyConstraint(["frame_key", "session_id"], ["artifacts.key", "artifacts.session_id"]),
        ForeignKeyConstraint(["mask_key", "session_id"], ["artifacts.key", "artifacts.session_id"]),
        ForeignKeyConstraint(["duplicate_of", "part_id", "session_id"], ["findings.id", "findings.part_id", "findings.session_id"]),
        CheckConstraint("frame_index >= 0 AND frame_width > 0 AND frame_height > 0", name="frame_geometry"),
        CheckConstraint("jsonb_typeof(bbox) = 'object' AND bbox ?& ARRAY['x','y','w','h'] AND jsonb_typeof(bbox->'x') = 'number' AND jsonb_typeof(bbox->'y') = 'number' AND jsonb_typeof(bbox->'w') = 'number' AND jsonb_typeof(bbox->'h') = 'number' AND (bbox->>'x')::numeric >= 0 AND (bbox->>'y')::numeric >= 0 AND (bbox->>'w')::numeric > 0 AND (bbox->>'h')::numeric > 0 AND (bbox->>'x')::numeric + (bbox->>'w')::numeric <= 1 AND (bbox->>'y')::numeric + (bbox->>'h')::numeric <= 1", name="bbox"),
        CheckConstraint("(status = 'verified' AND severity BETWEEN 1 AND 5 AND severity IS NOT NULL) OR (status != 'verified' AND severity IS NULL)", name="verified_severity"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint("duplicate_of IS NULL OR duplicate_of != id AND status = 'rejected'", name="duplicate_target"),
        CheckConstraint("jsonb_typeof(flags) = 'array'", name="flags_array"),
        CheckConstraint("length(track_id) > 0", name="track_id"))


class Coverage(Record, Base):
    __tablename__ = "coverage"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    zone: Mapped[Zone] = mapped_column(enum_type(Zone, "coverage_zone"))
    concept: Mapped[Concept] = mapped_column(enum_type(Concept, "coverage_concept"))
    state: Mapped[CoverageState] = mapped_column(enum_type(CoverageState, "coverage_state"))
    reason: Mapped[str | None] = mapped_column(String(240))
    __table_args__ = (UniqueConstraint("run_id", "zone", "concept"),
        ForeignKeyConstraint(["run_id", "session_id"], ["pipeline_runs.id", "pipeline_runs.session_id"]),
        CheckConstraint("state = 'assessed' OR reason IS NOT NULL AND length(reason) > 0", name="missing_reason"))


class Observation(Record, Base):
    __tablename__ = "observations"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict] = mapped_column(JSONB)
    frame_key: Mapped[str | None] = mapped_column(String(1024))
    reason: Mapped[str | None] = mapped_column(String(240))
    __table_args__ = (ForeignKeyConstraint(["run_id", "session_id"], ["pipeline_runs.id", "pipeline_runs.session_id"]),
        ForeignKeyConstraint(["frame_key", "session_id"], ["artifacts.key", "artifacts.session_id"]),)


class Claim(Record, Base):
    __tablename__ = "claims"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    report_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    claim_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    verdict: Mapped[ClaimVerdict] = mapped_column(enum_type(ClaimVerdict, "claim_verdict"))
    rationale: Mapped[str] = mapped_column(String(1000))
    __table_args__ = (UniqueConstraint("report_revision_id", "claim_index"), UniqueConstraint("id", "report_revision_id", "session_id"),
        ForeignKeyConstraint(["report_revision_id", "session_id"], ["report_revisions.id", "report_revisions.session_id"]),
        CheckConstraint("claim_index >= 0 AND length(text) BETWEEN 1 AND 10000 AND length(rationale) > 0", name="claim_content"))


class RevisionFinding(Base):
    __tablename__ = "revision_findings"
    revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    finding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    __table_args__ = (UniqueConstraint("revision_id", "finding_id", "session_id"),
        ForeignKeyConstraint(["revision_id", "session_id"], ["report_revisions.id", "report_revisions.session_id"]),
        ForeignKeyConstraint(["finding_id", "session_id"], ["findings.id", "findings.session_id"]),)


class ClaimEvidence(Base):
    """Normalized finding/frame references enforce ownership instead of unverifiable JSON ID arrays."""
    __tablename__ = "claim_evidence"
    claim_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    finding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    report_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    frame_key: Mapped[str] = mapped_column(String(1024))
    __table_args__ = (ForeignKeyConstraint(["claim_id", "report_revision_id", "session_id"], ["claims.id", "claims.report_revision_id", "claims.session_id"]),
        ForeignKeyConstraint(["report_revision_id", "finding_id", "session_id"], ["revision_findings.revision_id", "revision_findings.finding_id", "revision_findings.session_id"]),
        ForeignKeyConstraint(["finding_id", "frame_key", "session_id"], ["findings.id", "findings.frame_key", "findings.session_id"]),)


class Purchase(Record, Base):
    __tablename__ = "purchases"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    checkout_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    payment_intent_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    expected_amount: Mapped[int] = mapped_column(Integer, server_default="1900")
    currency: Mapped[str] = mapped_column(String(3), server_default="usd")
    state: Mapped[str] = mapped_column(states("purchase_state", "pending", "paid", "refunded", "failed"), server_default="pending")
    __table_args__ = (UniqueConstraint("id", "report_id", "session_id"),
        ForeignKeyConstraint(["report_id", "session_id"], ["reports.id", "reports.session_id"]),
        CheckConstraint("expected_amount = 1900 AND currency = 'usd'", name="buyer_price"),
        CheckConstraint("state NOT IN ('paid','refunded') OR checkout_id IS NOT NULL AND payment_intent_id IS NOT NULL", name="paid_provider_ids"))


class Entitlement(Record, Base):
    __tablename__ = "entitlements"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_purchase_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(states("entitlement_kind", "report_unlock"), server_default="report_unlock")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("source_purchase_id", "kind"),
        ForeignKeyConstraint(["source_purchase_id", "report_id", "session_id"], ["purchases.id", "purchases.report_id", "purchases.session_id"]),)


class StripeEvent(Base):
    __tablename__ = "stripe_events"
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB)
    processing_state: Mapped[str] = mapped_column(states("stripe_event_state", "pending", "processing", "processed", "ignored", "failed"), server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(240))
    __table_args__ = (CheckConstraint("length(event_id) > 0 AND length(event_type) > 0", name="event_identity"),)


class ModelCall(Record, Base):
    __tablename__ = "model_calls"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), unique=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(128))
    price_version: Mapped[str] = mapped_column(String(128))
    requested_units: Mapped[dict] = mapped_column(JSONB)
    actual_units: Mapped[dict | None] = mapped_column(JSONB)
    reserved_cost: Mapped[int] = mapped_column(BigInteger)
    actual_cost: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(states("model_call_state", "reserved", "succeeded", "failed", "unknown", "reconciled"), server_default="reserved")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("id", "session_id"),
        ForeignKeyConstraint(["job_id", "session_id"], ["jobs.id", "jobs.session_id"]),
        CheckConstraint("reserved_cost >= 0 AND (actual_cost IS NULL OR actual_cost >= 0) AND (latency_ms IS NULL OR latency_ms >= 0)", name="nonnegative_usage"),
        CheckConstraint("length(model) > 0 AND length(price_version) > 0", name="price_identity"))


class BudgetBucket(Base):
    __tablename__ = "budget_buckets"
    scope_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    limit_cents: Mapped[int] = mapped_column(BigInteger)
    reserved_cents: Mapped[int] = mapped_column(BigInteger, server_default="0")
    spent_cents: Mapped[int] = mapped_column(BigInteger, server_default="0")
    __table_args__ = (CheckConstraint("length(scope_key) > 0 AND limit_cents >= 0 AND reserved_cents >= 0 AND spent_cents >= 0", name="nonnegative_budget"),)


class RequestKey(Record, Base):
    __tablename__ = "request_keys"
    authority_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    route: Mapped[str] = mapped_column(String(255))
    key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = hash_column()
    response: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("authority_id", "route", "key"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND length(route) > 0 AND length(key) > 0", name="identity"),
        CheckConstraint("NOT (response ?| ARRAY['session_token','token','owner_token','signed_url','url','checkout_url'])", name="no_secret_response"))


class Account(Record, Base):
    """Dealer/flipper account (T15a). Only a salted PBKDF2 digest is stored.

    ``password_hash`` encodes its own parameters as
    ``pbkdf2_sha256$<iterations>$<hex digest>`` so the iteration count can be
    raised later without a schema change; ``password_salt`` is the per-account
    random salt (hex). Neither value, nor any plaintext password/token, may
    appear in logs, errors, or ``repr`` output.
    """
    __tablename__ = "accounts"
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    password_salt: Mapped[str] = mapped_column(String(64))
    disabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    tokens: Mapped[list[AccountToken]] = relationship(back_populates="account")
    sessions: Mapped[list[Session]] = relationship(back_populates="account")

    def __repr__(self) -> str:  # never include email, hash, or salt
        return f"Account(id={self.id!r}, disabled={self.disabled!r})"

    __table_args__ = (CheckConstraint("email = lower(email) AND email = btrim(email) AND length(email) BETWEEN 3 AND 320 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$'", name="email_shape"),
        CheckConstraint("password_salt ~ '^[0-9a-f]{64}$'", name="password_salt"),
        CheckConstraint("length(password_hash) > 0", name="password_hash"))


class PasswordResetToken(Record, Base):
    """Single-use password-reset credential; only its SHA-256 hash persists.

    The plaintext token is sent once by email and never stored or logged.
    A token is unusable once ``used_at`` is set or ``expires_at`` has passed;
    issuing a new token marks every earlier unused token used.
    """

    __tablename__ = "password_reset_tokens"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account: Mapped[Account] = relationship()

    def __repr__(self) -> str:  # never include the token hash
        return f"PasswordResetToken(id={self.id!r}, account_id={self.account_id!r})"

    __table_args__ = (CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash"),
        CheckConstraint("expires_at > created_at", name="token_expiry"),
        CheckConstraint("used_at IS NULL OR used_at >= created_at", name="token_used"))


class AccountToken(Record, Base):
    """Opaque account bearer token; only its SHA-256 hash is persisted.

    Mirrors the owner-token convention in ``app.services.auth``: the plaintext
    token is returned once at issuance and never stored. A token is unusable
    once ``revoked_at`` is set or ``expires_at`` has passed.
    """
    __tablename__ = "account_tokens"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account: Mapped[Account] = relationship(back_populates="tokens")

    def __repr__(self) -> str:  # never include the token hash
        return f"AccountToken(id={self.id!r}, account_id={self.account_id!r})"

    __table_args__ = (CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash"),
        CheckConstraint("expires_at > created_at", name="token_expiry"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name="token_revocation"))


class EvidenceSource(Record, Base):
    """Plugin evidence attached before finalize: raw data bound by hash, plus its derived summary."""
    __tablename__ = "evidence_sources"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(states("evidence_source_kind", "rig_telemetry", "obd_snapshot", "engine_audio"))
    provenance: Mapped[str] = mapped_column(states("evidence_source_provenance", "synthetic", "cached", "live"))
    device: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB)
    data_sha256: Mapped[str] = hash_column()
    summary: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (UniqueConstraint("session_id", "kind"),
        CheckConstraint("data_sha256 ~ '^[0-9a-f]{64}$'", name="data_hash"),
        CheckConstraint("jsonb_typeof(data) = 'object' AND jsonb_typeof(summary) = 'object'", name="json_objects"))


class AudioUpload(Record, Base):
    """Private engine-audio clip (<= 10 MiB) and what the worker derived from it."""
    __tablename__ = "audio_uploads"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    provider_upload_id: Mapped[str] = mapped_column(String(1024), unique=True)
    expected_bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(states("audio_content_type", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/x-m4a"))
    state: Mapped[UploadState] = mapped_column(enum_type(UploadState, "audio_upload_state"), server_default="open")
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_state: Mapped[str] = mapped_column(states("audio_processing_state", "pending", "queued", "ready", "failed"), server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(240))
    raw_sha256: Mapped[str | None] = hash_column(nullable=True)
    spectrogram_key: Mapped[str | None] = mapped_column(String(1024))
    spectrogram_sha256: Mapped[str | None] = hash_column(nullable=True)
    __table_args__ = (CheckConstraint("expected_bytes BETWEEN 1 AND 10485760", name="expected_bytes"),
        CheckConstraint("length(object_key) > 0 AND object_key !~ '[:?#]'", name="object_key"),
        CheckConstraint("length(provider_upload_id) > 0", name="provider_id"),
        CheckConstraint("state != 'accepted' OR accepted_at IS NOT NULL", name="accepted_timestamp"),
        CheckConstraint("raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'", name="raw_hash"),
        CheckConstraint("spectrogram_sha256 IS NULL OR spectrogram_sha256 ~ '^[0-9a-f]{64}$'", name="spectrogram_hash"),
        Index("uq_audio_uploads_open_session", "session_id", unique=True, postgresql_where=text("state = 'open'")))
