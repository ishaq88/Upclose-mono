"""Allow the durable vehicle-mask predecessor job.

Revision ID: f7b1d4e5c0a6
Revises: e6a0c3d4b9f5
"""
from alembic import op

revision = "f7b1d4e5c0a6"
down_revision = "e6a0c3d4b9f5"
branch_labels = None
depends_on = None

_OLD = "'ingest','segment','verify','read_dash','claims','score','narrate','render','billing_event','purge','source_ingest'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD},'vehicle_mask')")


def downgrade() -> None:
    # Refuse downgrade with retained mask jobs rather than erase pipeline state.
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD})")
