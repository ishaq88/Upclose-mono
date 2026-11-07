"""Password-reset tokens (single-use, hashed at rest).

Revision ID: c9549a675cf6
Revises: 9f3a7c1e8b42

Additive only: one new table. Only the SHA-256 of each reset token is
stored; the plaintext token travels once in the emailed link fragment.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c9549a675cf6"
down_revision = "9f3a7c1e8b42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("password_reset_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name=op.f("fk_password_reset_tokens_account_id_accounts")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_reset_tokens_token_hash")),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_password_reset_tokens_token_hash")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_password_reset_tokens_token_expiry")),
        sa.CheckConstraint("used_at IS NULL OR used_at >= created_at", name=op.f("ck_password_reset_tokens_token_used")))
    op.create_index(op.f("ix_password_reset_tokens_account_id"), "password_reset_tokens", ["account_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_password_reset_tokens_account_id"), table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
