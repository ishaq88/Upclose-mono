"""Shared source rate-limit buckets.

Revision ID: 9b21c7a0d4e1
Revises: 488757f523df
"""
from alembic import op
import sqlalchemy as sa

revision = "9b21c7a0d4e1"
down_revision = "488757f523df"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("rate_limit_buckets",
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("source_hash", "window_start", name="pk_rate_limit_buckets"),
        sa.CheckConstraint("source_hash ~ '^[0-9a-f]{64}$'", name="ck_rate_limit_buckets_source_hash"),
        sa.CheckConstraint("count >= 0", name="ck_rate_limit_buckets_nonnegative_count"))


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
