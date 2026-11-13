"""Durable bounded source indexing and stable persisted result pages."""

from alembic import op

revision = "0022_streaming_search"
down_revision = "0021_technical_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE source_search_builds(
      project_id uuid NOT NULL,shared_input_record_id uuid NOT NULL,profile_sha256 text NOT NULL,
      source_id text NOT NULL,cursor_position integer NOT NULL DEFAULT 0 CHECK(cursor_position>=0),
      completed boolean NOT NULL DEFAULT false,
      PRIMARY KEY(project_id,shared_input_record_id,profile_sha256,source_id),
      FOREIGN KEY(project_id,shared_input_record_id,profile_sha256,source_id)
      REFERENCES source_search_indexes(project_id,shared_input_record_id,profile_sha256,source_id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE source_search_results(
      project_id uuid NOT NULL,shared_input_record_id uuid NOT NULL,profile_sha256 text NOT NULL,
      query_sha256 text NOT NULL,request jsonb NOT NULL,results jsonb NOT NULL,
      PRIMARY KEY(project_id,shared_input_record_id,profile_sha256,query_sha256),
      FOREIGN KEY(project_id,shared_input_record_id) REFERENCES runtime_records(project_id,record_id) ON DELETE CASCADE
    )""")


def downgrade():
    op.drop_table("source_search_results")
    op.drop_table("source_search_builds")
