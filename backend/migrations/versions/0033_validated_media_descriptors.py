"""Immutable structural outcome validation; execution history keeps its owners."""

from alembic import op

revision = "0033_validated_media_descriptors"
down_revision = "0032_hosted_research_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE writing_outcome_validations(
      project_id uuid NOT NULL,run_id uuid NOT NULL,validator_version text NOT NULL,
      binding jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(project_id,run_id,validator_version),
      FOREIGN KEY(project_id,run_id) REFERENCES evaluation_writing_runs(project_id,run_id))""")
    op.execute("""CREATE TRIGGER immutable_writing_outcome_validation BEFORE UPDATE OR DELETE
      ON writing_outcome_validations FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()""")


def downgrade():
    raise RuntimeError("Validated outcome and paid execution history must be retained")
