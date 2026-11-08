"""Engine audio: private audio uploads, the engine_audio source kind and the audio_ingest job stage.

Revision ID: d9b4e7a2f6c1
Revises: a3d8f5c1e9b2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d9b4e7a2f6c1"
down_revision = "a3d8f5c1e9b2"
branch_labels = None
depends_on = None

_OLD_STAGES = ("'ingest','segment','verify','read_dash','claims','score','narrate','render','billing_event','purge',"
               "'source_ingest','vehicle_mask'")


def upgrade() -> None:
    op.create_table("audio_uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("provider_upload_id", sa.String(1024), nullable=False),
        sa.Column("expected_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(11), nullable=False),
        sa.Column("state", sa.String(10), nullable=False, server_default="open"),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("processing_state", sa.String(7), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(240)),
        sa.Column("raw_sha256", sa.String(64)),
        sa.Column("spectrogram_key", sa.String(1024)),
        sa.Column("spectrogram_sha256", sa.String(64)),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.UniqueConstraint("object_key"), sa.UniqueConstraint("provider_upload_id"),
        sa.CheckConstraint("content_type IN ('audio/wav','audio/x-wav','audio/mpeg','audio/mp4','audio/x-m4a')", name="audio_content_type"),
        sa.CheckConstraint("state IN ('open','completing','accepted','aborted')", name="audio_upload_state"),
        sa.CheckConstraint("processing_state IN ('pending','queued','ready','failed')", name="audio_processing_state"),
        sa.CheckConstraint("expected_bytes BETWEEN 1 AND 10485760", name="expected_bytes"),
        sa.CheckConstraint("length(object_key) > 0 AND object_key !~ '[:?#]'", name="object_key"),
        sa.CheckConstraint("length(provider_upload_id) > 0", name="provider_id"),
        sa.CheckConstraint("state != 'accepted' OR accepted_at IS NOT NULL", name="accepted_timestamp"),
        sa.CheckConstraint("raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'", name="raw_hash"),
        sa.CheckConstraint("spectrogram_sha256 IS NULL OR spectrogram_sha256 ~ '^[0-9a-f]{64}$'", name="spectrogram_hash"))
    op.create_index("ix_audio_uploads_session_id", "audio_uploads", ["session_id"])
    op.create_index("uq_audio_uploads_open_session", "audio_uploads", ["session_id"], unique=True,
                    postgresql_where=sa.text("state = 'open'"))
    op.drop_constraint(op.f("ck_evidence_sources_evidence_source_kind"), "evidence_sources", type_="check")
    op.create_check_constraint(op.f("ck_evidence_sources_evidence_source_kind"), "evidence_sources",
                               "kind IN ('rig_telemetry','obd_snapshot','engine_audio')")
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD_STAGES},'audio_ingest')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.execute("DELETE FROM jobs WHERE stage = 'audio_ingest'")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD_STAGES})")
    op.execute("DELETE FROM evidence_sources WHERE kind = 'engine_audio'")
    op.drop_constraint(op.f("ck_evidence_sources_evidence_source_kind"), "evidence_sources", type_="check")
    op.create_check_constraint(op.f("ck_evidence_sources_evidence_source_kind"), "evidence_sources",
                               "kind IN ('rig_telemetry','obd_snapshot')")
    op.drop_index("uq_audio_uploads_open_session", table_name="audio_uploads")
    op.drop_index("ix_audio_uploads_session_id", table_name="audio_uploads")
    op.drop_table("audio_uploads")
