"""Explicit, parent-bound credit refusal retries without changing existing attempt identities."""

from alembic import op

revision = "0013_credit_refusal_attempts"
down_revision = "0012_own_plan_stages"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE evaluation_model_invocations ADD COLUMN attempt_kind text NOT NULL DEFAULT 'role'"
    )
    op.execute(
        "ALTER TABLE evaluation_model_invocations ADD COLUMN operation_kind text NOT NULL DEFAULT 'role'"
    )
    op.execute(
        "UPDATE evaluation_model_invocations SET attempt_kind='anchor_repair',operation_kind='anchor_repair' WHERE attempt_index=1"
    )
    op.drop_constraint("review_anchor_parent_scope", "evaluation_model_invocations", type_="foreignkey")
    op.drop_constraint("review_anchor_attempt_bound", "evaluation_model_invocations", type_="check")
    op.execute("ALTER TABLE evaluation_model_invocations ALTER COLUMN parent_attempt_index DROP EXPRESSION")
    op.execute("""ALTER TABLE evaluation_model_invocations ADD CONSTRAINT evaluation_attempt_shape CHECK(
      (attempt_index=0 AND attempt_kind='role' AND operation_kind='role' AND parent_invocation_id IS NULL AND parent_attempt_index IS NULL)
      OR (attempt_index>0 AND parent_invocation_id IS NOT NULL AND parent_attempt_index IS NOT NULL AND parent_attempt_index>=0 AND parent_attempt_index<attempt_index
        AND ((attempt_kind='anchor_repair' AND operation_kind='anchor_repair' AND stage='review')
          OR (attempt_kind='credit_retry' AND operation_kind IN ('role','anchor_repair') AND (operation_kind='role' OR stage='review')))))""")
    op.create_foreign_key(
        "evaluation_attempt_parent_scope",
        "evaluation_model_invocations",
        "evaluation_model_invocations",
        ["project_id", "run_id", "stage", "parent_attempt_index", "parent_invocation_id"],
        ["project_id", "run_id", "stage", "attempt_index", "invocation_id"],
    )
    op.execute(
        "CREATE UNIQUE INDEX evaluation_one_child_per_action ON evaluation_model_invocations(project_id,parent_invocation_id,attempt_kind) WHERE parent_invocation_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX evaluation_one_anchor_repair ON evaluation_model_invocations(project_id,run_id,stage) WHERE attempt_kind='anchor_repair'"
    )
    op.execute("""CREATE TRIGGER immutable_attempt_kind BEFORE UPDATE ON evaluation_model_invocations
      FOR EACH ROW WHEN(OLD.attempt_kind IS DISTINCT FROM NEW.attempt_kind OR OLD.operation_kind IS DISTINCT FROM NEW.operation_kind
      OR OLD.parent_attempt_index IS DISTINCT FROM NEW.parent_attempt_index) EXECUTE FUNCTION reject_history_mutation()""")


def downgrade():
    raise RuntimeError("Attempt archives must be retained.")
