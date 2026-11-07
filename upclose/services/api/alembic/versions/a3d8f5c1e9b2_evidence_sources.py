"""Evidence sources: plugin telemetry / OBD data attached to an inspection before finalize.

Revision ID: a3d8f5c1e9b2
Revises: b7e2f1a9c3d4

Additive only: one new table. The ready-revision immutability triggers do not touch it; purge
deletes its rows with the session through the generic session_id scrub.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a3d8f5c1e9b2"
down_revision = "b7e2f1a9c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("evidence_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(13), nullable=False),
        sa.Column("provenance", sa.String(9), nullable=False),
        sa.Column("device", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("data_sha256", sa.String(64), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.UniqueConstraint("session_id", "kind"),
        sa.CheckConstraint("kind IN ('rig_telemetry','obd_snapshot')", name="evidence_source_kind"),
        sa.CheckConstraint("provenance IN ('synthetic','cached','live')", name="evidence_source_provenance"),
        sa.CheckConstraint("data_sha256 ~ '^[0-9a-f]{64}$'", name="data_hash"),
        sa.CheckConstraint("jsonb_typeof(data) = 'object' AND jsonb_typeof(summary) = 'object'", name="json_objects"))
    op.create_index("ix_evidence_sources_session_id", "evidence_sources", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_sources_session_id", table_name="evidence_sources")
    op.drop_table("evidence_sources")
