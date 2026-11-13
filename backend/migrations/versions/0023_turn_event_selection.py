"""Project physical-turn identity into indexed journal control metadata."""

from alembic import op

revision = "0023_turn_event_selection"
down_revision = "0022_streaming_search"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE evaluation_invocation_events
        ADD COLUMN source_turn_index integer CHECK(source_turn_index >= 0)""")
    op.execute("ALTER TABLE evaluation_invocation_events DISABLE TRIGGER immutable_history")
    op.execute("""UPDATE evaluation_invocation_events e
        SET source_turn_index=(r.payload->'data'->>'source_turn_index')::integer
        FROM runtime_records r WHERE r.project_id=e.project_id AND r.record_id=e.record_id
          AND r.payload->'data'->>'source_turn_index' IS NOT NULL""")
    op.execute("ALTER TABLE evaluation_invocation_events ENABLE TRIGGER immutable_history")
    op.execute("""CREATE INDEX evaluation_events_by_turn ON evaluation_invocation_events
        (project_id,invocation_id,source_turn_index,sequence) WHERE source_turn_index IS NOT NULL""")

    op.execute("""CREATE FUNCTION bind_evaluation_event_turn() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE derived integer;
    BEGIN
      SELECT (payload->'data'->>'source_turn_index')::integer INTO derived
        FROM runtime_records WHERE project_id=NEW.project_id AND record_id=NEW.record_id;
      IF NEW.source_turn_index IS NOT NULL AND NEW.source_turn_index IS DISTINCT FROM derived THEN
        RAISE EXCEPTION 'evaluation_event_turn_mismatch' USING ERRCODE='23514';
      END IF;
      NEW.source_turn_index := derived;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER bind_evaluation_event_turn BEFORE INSERT ON evaluation_invocation_events
        FOR EACH ROW EXECUTE FUNCTION bind_evaluation_event_turn()""")


def downgrade():
    op.execute("DROP TRIGGER bind_evaluation_event_turn ON evaluation_invocation_events")
    op.execute("DROP FUNCTION bind_evaluation_event_turn()")
    op.drop_index("evaluation_events_by_turn", table_name="evaluation_invocation_events")
    op.drop_column("evaluation_invocation_events", "source_turn_index")
