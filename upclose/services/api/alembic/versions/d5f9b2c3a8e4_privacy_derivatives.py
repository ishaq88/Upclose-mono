"""Exact-hash manual derivative review bindings."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "d5f9b2c3a8e4"
down_revision = "c4e8a1f2b7d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('privacy_derivatives',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('session_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('revision_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('finding_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source_key', sa.String(1024), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('object_key', sa.String(1024), nullable=False, unique=True),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('review_receipt', sa.String(128)),
        sa.ForeignKeyConstraint(['revision_id','finding_id','session_id'], ['revision_findings.revision_id','revision_findings.finding_id','revision_findings.session_id']),
        sa.ForeignKeyConstraint(['finding_id','source_key','session_id'], ['findings.id','findings.frame_key','findings.session_id']),
        sa.CheckConstraint("object_key = 'media/' || session_id::text || '/derivatives/' || id::text", name="derivative_namespace"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND source_sha256 ~ '^[0-9a-f]{64}$'", name='hashes'),
        sa.CheckConstraint("(approved_at IS NULL AND review_receipt IS NULL) OR (approved_at IS NOT NULL AND review_receipt = 'permission-cleared-manually-reviewed-v1')", name='review'))
    op.create_index('ix_privacy_derivatives_session_id', 'privacy_derivatives', ['session_id'])
    op.execute("""CREATE FUNCTION upclose_guard_privacy_derivative() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM sessions WHERE id=OLD.session_id AND state IN ('deleting','deleted')) THEN
          RAISE EXCEPTION 'privacy binding is immutable' USING ERRCODE='check_violation';
        END IF;
        RETURN OLD;
      END IF;
      IF (NEW.id,NEW.session_id,NEW.revision_id,NEW.finding_id,NEW.source_key,NEW.source_sha256,NEW.object_key,NEW.sha256,NEW.created_at)
         IS DISTINCT FROM (OLD.id,OLD.session_id,OLD.revision_id,OLD.finding_id,OLD.source_key,OLD.source_sha256,OLD.object_key,OLD.sha256,OLD.created_at)
         OR OLD.approved_at IS NOT NULL THEN
        RAISE EXCEPTION 'privacy binding is immutable' USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER privacy_derivative_immutable BEFORE UPDATE OR DELETE ON privacy_derivatives FOR EACH ROW EXECUTE FUNCTION upclose_guard_privacy_derivative()")


def downgrade() -> None:
    op.execute("DROP TABLE privacy_derivatives")
    op.execute("DROP FUNCTION upclose_guard_privacy_derivative()")
