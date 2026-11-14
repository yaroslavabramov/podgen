"""One durable product coordinator; provider execution remains in its existing journals."""

from alembic import op

revision = "0031_episode_production"
down_revision = "0030_passage_fingerprints"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE episode_production_runs(
      project_id uuid NOT NULL REFERENCES projects(project_id),run_id uuid NOT NULL,
      actor_id uuid NOT NULL,configuration jsonb NOT NULL,configuration_hash text NOT NULL,
      revision bigint NOT NULL DEFAULT 0,epoch bigint NOT NULL DEFAULT 0,
      desired_state text NOT NULL DEFAULT 'running' CHECK(desired_state IN ('running','paused')),
      status text NOT NULL DEFAULT 'prepared',phase text NOT NULL DEFAULT 'research',
      progress jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(project_id,run_id))""")
    op.execute("""CREATE TABLE episode_production_events(
      project_id uuid NOT NULL,run_id uuid NOT NULL,revision bigint NOT NULL,
      payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(project_id,run_id,revision),
      FOREIGN KEY(project_id,run_id) REFERENCES episode_production_runs(project_id,run_id))""")
    op.execute("""CREATE TRIGGER immutable_episode_production_event BEFORE UPDATE OR DELETE
      ON episode_production_events FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()""")
    op.execute("""CREATE TRIGGER immutable_episode_production_config BEFORE UPDATE
      ON episode_production_runs FOR EACH ROW WHEN(
      OLD.configuration IS DISTINCT FROM NEW.configuration OR
      OLD.configuration_hash IS DISTINCT FROM NEW.configuration_hash OR
      OLD.actor_id IS DISTINCT FROM NEW.actor_id OR OLD.project_id IS DISTINCT FROM NEW.project_id OR
      OLD.run_id IS DISTINCT FROM NEW.run_id) EXECUTE FUNCTION reject_history_mutation()""")


def downgrade():
    raise RuntimeError("Production execution history must be retained")
