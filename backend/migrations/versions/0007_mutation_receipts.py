"""Lost-acknowledgement replay receipts and per-run input-manifest CAS."""

from alembic import op

revision = "0007_mutation_receipts"
down_revision = "0006_blob_privacy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE mutation_receipts(
      project_id uuid NOT NULL REFERENCES projects, operation_kind text NOT NULL, operation_id uuid NOT NULL,
      actor_id uuid NOT NULL, input_fingerprint text NOT NULL, output json NOT NULL,
      PRIMARY KEY(project_id,operation_kind,operation_id)
    )""")
    op.execute(
        """CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON mutation_receipts FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"""
    )
    op.execute("""CREATE TABLE task_input_heads(
      project_id uuid NOT NULL, run_id uuid NOT NULL, task_id uuid NOT NULL, manifest_id uuid NOT NULL,
      lock_version bigint NOT NULL DEFAULT 0,
      run_kind text GENERATED ALWAYS AS ('task_run') STORED,
      task_kind text GENERATED ALWAYS AS ('task_spec') STORED,
      manifest_kind text GENERATED ALWAYS AS ('input_manifest') STORED,
      PRIMARY KEY(project_id,run_id),
      FOREIGN KEY(project_id,run_kind,run_id) REFERENCES operational_records(project_id,record_kind,record_id),
      FOREIGN KEY(project_id,task_kind,task_id) REFERENCES operational_records(project_id,record_kind,record_id),
      FOREIGN KEY(project_id,manifest_kind,manifest_id) REFERENCES operational_records(project_id,record_kind,record_id)
    )""")


def downgrade():
    raise RuntimeError("Mutation deduplication and input fencing cannot be weakened by downgrade.")
