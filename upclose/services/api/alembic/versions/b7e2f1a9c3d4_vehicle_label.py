"""Vehicle label on sessions (owner display name).

Revision ID: b7e2f1a9c3d4
Revises: c9549a675cf6

Additive only: one nullable column. Existing rows keep NULL; consumers
fall back to prior behaviour when no label is set.
"""
from alembic import op
import sqlalchemy as sa

revision = "b7e2f1a9c3d4"
down_revision = "c9549a675cf6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("vehicle_label", sa.String(80), nullable=True))
    op.create_check_constraint(
        op.f("ck_sessions_vehicle_label_length"), "sessions",
        "vehicle_label IS NULL OR length(vehicle_label) BETWEEN 1 AND 80")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_sessions_vehicle_label_length"), "sessions", type_="check")
    op.drop_column("sessions", "vehicle_label")
