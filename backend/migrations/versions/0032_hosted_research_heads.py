"""Bounded hosted research state, anchored to its immutable journal manifest."""

from alembic import op

revision = "0032_hosted_research_heads"
down_revision = "0031_episode_production"
branch_labels = None
depends_on = None


def upgrade():
    # IF NOT EXISTS also accepts the coordinator's provisional 0031 projection.
    # Only a derived, unpublished projection is reset; no journal record is changed.
    op.execute("""CREATE TABLE IF NOT EXISTS hosted_research_heads(
      project_id uuid NOT NULL REFERENCES projects(project_id),run_id uuid NOT NULL,
      revision bigint NOT NULL,projection jsonb NOT NULL,PRIMARY KEY(project_id,run_id))""")
    op.execute("""DELETE FROM hosted_research_heads
      WHERE projection->>'contract' IS DISTINCT FROM 'hosted-research-head/1'""")
    op.execute("""ALTER TABLE hosted_research_heads
      ADD COLUMN manifest_record_id uuid NOT NULL,
      ADD FOREIGN KEY(project_id,manifest_record_id) REFERENCES runtime_records(project_id,record_id),
      ADD CHECK(projection ? 'revision' AND revision>=0 AND revision=(projection->>'revision')::bigint),
      ADD CHECK((projection->>'contract') IS NOT DISTINCT FROM 'hosted-research-head/1')""")
    op.execute("""CREATE TRIGGER immutable_hosted_research_head_owner BEFORE UPDATE
      ON hosted_research_heads FOR EACH ROW WHEN(
      OLD.project_id IS DISTINCT FROM NEW.project_id OR OLD.run_id IS DISTINCT FROM NEW.run_id OR
      OLD.manifest_record_id IS DISTINCT FROM NEW.manifest_record_id)
      EXECUTE FUNCTION reject_history_mutation()""")


def downgrade():
    op.execute("DROP TABLE hosted_research_heads")
