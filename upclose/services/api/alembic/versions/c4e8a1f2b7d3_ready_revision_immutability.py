"""Block mutation of a ready report revision's snapshot and its claim/association rows.

Revision ID: c4e8a1f2b7d3
Revises: 9b21c7a0d4e1

A `ready` revision's identity and snapshot fields (state, manifest, manifest_hash, score_result,
free_narration, paid_narration, rendered_at, report_id, session_id, revision_no) cannot change, and
claims / claim_evidence / revision_findings rows of a ready revision cannot be inserted, updated or
deleted. The one exception is a session that is `deleting` or `deleted`: deletion must be able to
scrub snapshot content (listing text, claims, narration) and remove the rows.
"""
from alembic import op

revision = "c4e8a1f2b7d3"
down_revision = "9b21c7a0d4e1"
branch_labels = None
depends_on = None

_DELETING = "EXISTS (SELECT 1 FROM sessions s WHERE s.id = {sid} AND s.state IN ('deleting', 'deleted'))"

_REVISION_FUNCTION = f"""
CREATE FUNCTION upclose_guard_ready_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'ready' AND NOT {_DELETING.format(sid='OLD.session_id')} THEN
    IF TG_OP = 'DELETE' THEN
      RAISE EXCEPTION 'a ready report revision cannot be deleted' USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.id, NEW.state, NEW.manifest, NEW.manifest_hash, NEW.score_result, NEW.free_narration, NEW.paid_narration,
        NEW.rendered_at, NEW.report_id, NEW.session_id, NEW.revision_no)
       IS DISTINCT FROM
       (OLD.id, OLD.state, OLD.manifest, OLD.manifest_hash, OLD.score_result, OLD.free_narration, OLD.paid_narration,
        OLD.rendered_at, OLD.report_id, OLD.session_id, OLD.revision_no) THEN
      RAISE EXCEPTION 'a ready report revision snapshot is immutable' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END
$$
"""

# One function per child table, because the revision column is named differently in each.
_CHILD_FUNCTION = """
CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  ids uuid[];
  locked boolean;
BEGIN
  IF TG_OP = 'INSERT' THEN
    ids := ARRAY[NEW.{column}];
  ELSIF TG_OP = 'DELETE' THEN
    ids := ARRAY[OLD.{column}];
  ELSE
    ids := ARRAY[OLD.{column}, NEW.{column}];
  END IF;
  -- Serialize child mutations with the transition to ready. A plain MVCC read
  -- can miss a concurrent publication and allow a late child mutation.
  PERFORM 1 FROM report_revisions WHERE id = ANY(ids) ORDER BY id FOR UPDATE;
  SELECT EXISTS (
    SELECT 1 FROM report_revisions r
    WHERE r.id = ANY(ids) AND r.state = 'ready'
      AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = r.session_id AND s.state IN ('deleting', 'deleted'))
  ) INTO locked;
  IF locked THEN
    RAISE EXCEPTION 'rows of a ready report revision are immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END
$$
"""

_CHILDREN = (("claims", "upclose_guard_ready_claims", "report_revision_id"),
             ("claim_evidence", "upclose_guard_ready_claim_evidence", "report_revision_id"),
             ("revision_findings", "upclose_guard_ready_revision_findings", "revision_id"))


def upgrade() -> None:
    op.execute(_REVISION_FUNCTION)
    op.execute("CREATE TRIGGER upclose_ready_revision_immutable BEFORE UPDATE OR DELETE ON report_revisions "
               "FOR EACH ROW EXECUTE FUNCTION upclose_guard_ready_revision()")
    for table, function, column in _CHILDREN:
        op.execute(_CHILD_FUNCTION.format(name=function, column=column))
        op.execute(f"CREATE TRIGGER {function} BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION {function}()")


def downgrade() -> None:
    for table, function, _ in reversed(_CHILDREN):
        op.execute(f"DROP TRIGGER {function} ON {table}")
        op.execute(f"DROP FUNCTION {function}()")
    op.execute("DROP TRIGGER upclose_ready_revision_immutable ON report_revisions")
    op.execute("DROP FUNCTION upclose_guard_ready_revision()")
