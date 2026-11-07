"""Separate bounded source videos from canonical inspection parts.

Revision ID: e6a0c3d4b9f5
Revises: d5f9b2c3a8e4
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e6a0c3d4b9f5"
down_revision = "d5f9b2c3a8e4"
branch_labels = None
depends_on = None

_OLD_STAGES = "'ingest','segment','verify','read_dash','claims','score','narrate','render','billing_event','purge'"


def upgrade() -> None:
    op.add_column("sessions", sa.Column("capture_mode", sa.String(12), nullable=False, server_default="guided"))
    op.create_check_constraint(op.f("ck_sessions_capture_mode"), "sessions", "capture_mode IN ('guided','single_video')")
    op.create_table("video_uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("provider_upload_id", sa.String(1024), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("state", sa.String(10), nullable=False, server_default="open"),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("segments", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("processing_state", sa.String(7), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(240)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("raw_sha256", sa.String(64)),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.UniqueConstraint("id", "session_id"),
        sa.UniqueConstraint("object_key"), sa.UniqueConstraint("provider_upload_id"),
        sa.CheckConstraint("state IN ('open','completing','accepted','aborted')", name="video_upload_state"),
        sa.CheckConstraint("processing_state IN ('pending','queued','ready','failed')", name="video_processing_state"),
        sa.CheckConstraint("expected_bytes BETWEEN 1 AND 209715200", name="expected_bytes"),
        sa.CheckConstraint("content_type IN ('video/mp4','video/webm','video/quicktime')", name="content_type"),
        sa.CheckConstraint("length(object_key) > 0 AND object_key !~ '[:?#]'", name="object_key"),
        sa.CheckConstraint("length(provider_upload_id) > 0", name="provider_id"),
        sa.CheckConstraint("state != 'accepted' OR accepted_at IS NOT NULL", name="accepted_timestamp"),
        sa.CheckConstraint("jsonb_typeof(segments) = 'array' AND jsonb_array_length(segments) <= 8", name="segments"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms BETWEEN 1 AND 180250", name="duration"),
        sa.CheckConstraint("raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'", name="raw_hash"),
        sa.CheckConstraint("processing_state != 'failed' OR reason IS NOT NULL AND length(reason) > 0", name="failure_reason"))
    op.create_index("ix_video_uploads_session_id", "video_uploads", ["session_id"])
    op.create_index("uq_video_uploads_active_session", "video_uploads", ["session_id"], unique=True,
                    postgresql_where=sa.text("state IN ('open','completing','accepted')"))
    op.add_column("parts", sa.Column("source_video_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key("fk_parts_source_video", "parts", "video_uploads",
                         ["source_video_id", "session_id"], ["id", "session_id"])
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD_STAGES},'source_ingest')")


def downgrade() -> None:
    # Preserve the old job check: a populated source pipeline cannot silently disappear.
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD_STAGES})")
    op.drop_constraint("fk_parts_source_video", "parts", type_="foreignkey")
    op.drop_column("parts", "source_video_id")
    op.drop_table("video_uploads")
    op.drop_constraint(op.f("ck_sessions_capture_mode"), "sessions", type_="check")
    op.drop_column("sessions", "capture_mode")
