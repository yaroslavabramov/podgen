"""One explicit review anchor-repair attempt; keep original invocation and its usage."""

from alembic import op

revision = "0011_review_anchor_attempt"
down_revision = "0010_writing_evaluation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE evaluation_model_invocations ADD COLUMN attempt_index integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE evaluation_model_invocations ADD COLUMN parent_invocation_id uuid")
    op.execute(
        "ALTER TABLE evaluation_model_invocations ADD CONSTRAINT review_anchor_attempt_bound CHECK((attempt_index=0 AND parent_invocation_id IS NULL) OR (attempt_index=1 AND stage='review' AND parent_invocation_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE evaluation_model_invocations ADD COLUMN parent_attempt_index integer GENERATED ALWAYS AS (CASE WHEN parent_invocation_id IS NULL THEN NULL ELSE 0 END) STORED"
    )
    op.create_unique_constraint(
        "evaluation_invocation_target",
        "evaluation_model_invocations",
        ["project_id", "run_id", "stage", "attempt_index", "invocation_id"],
    )
    op.execute(
        "ALTER TABLE evaluation_model_invocations ADD CONSTRAINT review_anchor_parent_scope FOREIGN KEY(project_id,run_id,stage,parent_attempt_index,parent_invocation_id) REFERENCES evaluation_model_invocations(project_id,run_id,stage,attempt_index,invocation_id)"
    )
    op.drop_constraint(
        "evaluation_model_invocations_project_id_run_id_stage_key",
        "evaluation_model_invocations",
        type_="unique",
    )
    op.create_unique_constraint(
        "evaluation_stage_attempt",
        "evaluation_model_invocations",
        ["project_id", "run_id", "stage", "attempt_index"],
    )
    op.execute("""CREATE TRIGGER immutable_invocation_binding BEFORE UPDATE ON evaluation_model_invocations
      FOR EACH ROW WHEN(OLD.project_id IS DISTINCT FROM NEW.project_id OR OLD.run_id IS DISTINCT FROM NEW.run_id
      OR OLD.invocation_id IS DISTINCT FROM NEW.invocation_id OR OLD.stage IS DISTINCT FROM NEW.stage
      OR OLD.request_record_id IS DISTINCT FROM NEW.request_record_id OR OLD.attempt_index IS DISTINCT FROM NEW.attempt_index
      OR OLD.parent_invocation_id IS DISTINCT FROM NEW.parent_invocation_id) EXECUTE FUNCTION reject_history_mutation()""")
    op.execute(
        "CREATE TRIGGER immutable_delete BEFORE DELETE ON evaluation_model_invocations FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
    )


def downgrade():
    raise RuntimeError("Failed and repaired review attempts must remain independently addressable.")
