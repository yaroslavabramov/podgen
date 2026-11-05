"""Scoped evaluation run and model invocation journal; immutable bodies remain in WP-01 storage."""

from alembic import op

revision = "0010_writing_evaluation"
down_revision = "0009_evaluator_bytes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE evaluation_writing_runs(
      project_id uuid NOT NULL REFERENCES projects,run_id uuid NOT NULL,actor_id uuid NOT NULL,
      input_fingerprint text NOT NULL,manifest_id uuid NOT NULL,status text NOT NULL,
      failure_code text,created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(project_id,run_id),
      FOREIGN KEY(project_id,manifest_id) REFERENCES runtime_records(project_id,record_id)
    )""")
    op.execute("""CREATE TABLE evaluation_model_invocations(
      project_id uuid NOT NULL,run_id uuid NOT NULL,invocation_id uuid NOT NULL,
      stage text NOT NULL CHECK(stage IN ('draft','review','revise')),
      status text NOT NULL,request_record_id uuid NOT NULL,provider_response_id text,
      output_record_id uuid, input_tokens bigint,output_tokens bigint,
      PRIMARY KEY(project_id,invocation_id),UNIQUE(project_id,run_id,stage),
      FOREIGN KEY(project_id,run_id) REFERENCES evaluation_writing_runs(project_id,run_id),
      FOREIGN KEY(project_id,request_record_id) REFERENCES runtime_records(project_id,record_id),
      FOREIGN KEY(project_id,output_record_id) REFERENCES runtime_records(project_id,record_id)
    )""")
    op.execute("""CREATE TABLE evaluation_invocation_events(
      project_id uuid NOT NULL,invocation_id uuid NOT NULL,event_id uuid NOT NULL,
      sequence bigserial NOT NULL,record_id uuid NOT NULL,event_kind text NOT NULL,
      PRIMARY KEY(project_id,event_id),
      FOREIGN KEY(project_id,invocation_id) REFERENCES evaluation_model_invocations(project_id,invocation_id),
      FOREIGN KEY(project_id,record_id) REFERENCES runtime_records(project_id,record_id)
    )""")
    op.execute(
        "CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON evaluation_invocation_events FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER immutable_history BEFORE UPDATE ON evaluation_writing_runs FOR EACH ROW WHEN (OLD.input_fingerprint IS DISTINCT FROM NEW.input_fingerprint OR OLD.manifest_id IS DISTINCT FROM NEW.manifest_id OR OLD.actor_id IS DISTINCT FROM NEW.actor_id OR OLD.run_id IS DISTINCT FROM NEW.run_id OR OLD.project_id IS DISTINCT FROM NEW.project_id) EXECUTE FUNCTION reject_history_mutation()"
    )

    op.execute(
        "CREATE TRIGGER immutable_delete BEFORE DELETE ON evaluation_writing_runs FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
    )


def downgrade():
    raise RuntimeError("Evaluation history must be retained.")
