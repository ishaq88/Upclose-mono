"""Dealer/flipper accounts, account tokens, and session ownership (T15a).

Revision ID: 9f3a7c1e8b42
Revises: f7b1d4e5c0a6

Additive only: existing ``sessions`` rows keep ``account_id IS NULL``
(anonymous buyer sessions). No existing data is rewritten or backfilled.
Only password digests and token hashes are stored; never plaintext secrets.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "9f3a7c1e8b42"
down_revision = "f7b1d4e5c0a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("password_salt", sa.String(64), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.UniqueConstraint("email", name=op.f("uq_accounts_email")),
        sa.CheckConstraint("email = lower(email) AND email = btrim(email) AND length(email) BETWEEN 3 AND 320 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$'", name=op.f("ck_accounts_email_shape")),
        sa.CheckConstraint("password_salt ~ '^[0-9a-f]{64}$'", name=op.f("ck_accounts_password_salt")),
        sa.CheckConstraint("length(password_hash) > 0", name=op.f("ck_accounts_password_hash")))
    op.create_table("account_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name=op.f("fk_account_tokens_account_id_accounts")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_account_tokens_token_hash")),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_account_tokens_token_hash")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_account_tokens_token_expiry")),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name=op.f("ck_account_tokens_token_revocation")))
    op.create_index(op.f("ix_account_tokens_account_id"), "account_tokens", ["account_id"])
    # Nullable FK: pre-existing and anonymous sessions are untouched (NULL).
    op.add_column("sessions", sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(op.f("fk_sessions_account_id_accounts"), "sessions", "accounts", ["account_id"], ["id"])
    op.create_index(op.f("ix_sessions_account_id"), "sessions", ["account_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_sessions_account_id"), table_name="sessions")
    op.drop_constraint(op.f("fk_sessions_account_id_accounts"), "sessions", type_="foreignkey")
    op.drop_column("sessions", "account_id")
    op.drop_index(op.f("ix_account_tokens_account_id"), table_name="account_tokens")
    op.drop_table("account_tokens")
    op.drop_table("accounts")
