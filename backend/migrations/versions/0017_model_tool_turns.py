"""Physical model turns within an unchanged semantic invocation."""

from alembic import op

revision = "0017_model_tool_turns"
down_revision = "0016_input_budget_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE evaluation_model_turns(
      project_id uuid NOT NULL,root_invocation_id uuid NOT NULL,
      turn_index integer NOT NULL CHECK(turn_index>=0), invocation_id uuid NOT NULL,
      status text NOT NULL,request_record_id uuid NOT NULL,provider_response_id text,
      input_tokens bigint,output_tokens bigint,
      PRIMARY KEY(project_id,root_invocation_id,turn_index),UNIQUE(project_id,invocation_id),
      FOREIGN KEY(project_id,root_invocation_id) REFERENCES evaluation_model_invocations(project_id,invocation_id),
      FOREIGN KEY(project_id,request_record_id) REFERENCES runtime_records(project_id,record_id)
    )""")
    op.execute("""CREATE TRIGGER immutable_model_turn_identity BEFORE UPDATE ON evaluation_model_turns
      FOR EACH ROW WHEN(OLD.root_invocation_id IS DISTINCT FROM NEW.root_invocation_id
      OR OLD.turn_index IS DISTINCT FROM NEW.turn_index OR OLD.invocation_id IS DISTINCT FROM NEW.invocation_id
      OR OLD.request_record_id IS DISTINCT FROM NEW.request_record_id OR OLD.project_id IS DISTINCT FROM NEW.project_id)
      EXECUTE FUNCTION reject_history_mutation()""")
    op.execute(
        "CREATE TRIGGER immutable_model_turn_delete BEFORE DELETE ON evaluation_model_turns FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
    )


def downgrade():
    op.drop_table("evaluation_model_turns")
