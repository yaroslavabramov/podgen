"""Project-local, rebuildable FTS/pgvector projections of frozen source selectors."""

from alembic import op

revision = "0019_source_search"
down_revision = "0018_total_input_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""CREATE TABLE source_search_indexes(
      project_id uuid NOT NULL,shared_input_record_id uuid NOT NULL,profile_sha256 text NOT NULL,
      source_id text NOT NULL,source_sha256 text NOT NULL,source_length integer NOT NULL,
      profile jsonb NOT NULL,spans jsonb NOT NULL,language text NOT NULL,chunk_count integer NOT NULL CHECK(chunk_count>=0),
      PRIMARY KEY(project_id,shared_input_record_id,profile_sha256,source_id),
      FOREIGN KEY(project_id,shared_input_record_id) REFERENCES runtime_records(project_id,record_id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE source_search_chunks(
      project_id uuid NOT NULL,shared_input_record_id uuid NOT NULL,profile_sha256 text NOT NULL,source_id text NOT NULL,
      start_offset integer NOT NULL CHECK(start_offset>=0),end_offset integer NOT NULL CHECK(end_offset>start_offset),
      language text NOT NULL,lexical tsvector NOT NULL,exact_terms text[] NOT NULL,phrase_text text NOT NULL,embedding vector(384) NOT NULL,
      PRIMARY KEY(project_id,shared_input_record_id,profile_sha256,source_id,start_offset,end_offset),
      FOREIGN KEY(project_id,shared_input_record_id,profile_sha256,source_id)
        REFERENCES source_search_indexes(project_id,shared_input_record_id,profile_sha256,source_id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX source_search_fts ON source_search_chunks USING gin(lexical)")
    op.execute("CREATE INDEX source_search_entities ON source_search_chunks USING gin(exact_terms)")


def downgrade():
    op.drop_table("source_search_chunks")
    op.drop_table("source_search_indexes")
