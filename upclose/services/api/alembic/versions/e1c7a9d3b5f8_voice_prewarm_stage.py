"""Allow the session-owned voice-guide prewarm job.

Revision ID: e1c7a9d3b5f8
Revises: d9b4e7a2f6c1
"""
from alembic import op

revision = "e1c7a9d3b5f8"
down_revision = "d9b4e7a2f6c1"
branch_labels = None
depends_on = None

_OLD = ("'ingest','segment','verify','read_dash','claims','score','narrate','render','billing_event','purge',"
        "'source_ingest','vehicle_mask','audio_ingest'")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD},'voice_prewarm')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_job_stage"), "jobs", type_="check")
    op.execute("DELETE FROM jobs WHERE stage = 'voice_prewarm'")
    op.create_check_constraint(op.f("ck_jobs_job_stage"), "jobs", f"stage IN ({_OLD})")
