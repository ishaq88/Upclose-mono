"""Record who approved a privacy derivative (NULL = operator, 'demo-auto' = automated demo approval).

Revision ID: a8c2e5f1b7d3
Revises: e1c7a9d3b5f8
"""
import sqlalchemy as sa
from alembic import op

revision = "a8c2e5f1b7d3"
down_revision = "e1c7a9d3b5f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("privacy_derivatives", sa.Column("approved_by", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("privacy_derivatives", "approved_by")
