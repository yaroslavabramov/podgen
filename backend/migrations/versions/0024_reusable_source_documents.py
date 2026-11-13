"""Separate reusable document computation from authorized run-local source identities."""

from alembic import op

revision = "0024_reusable_source_documents"
down_revision = "0023_turn_event_selection"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE source_search_documents(
      project_id uuid NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
      document_key text NOT NULL,source_sha256 text NOT NULL,source_length integer NOT NULL CHECK(source_length>=0),
      profile_sha256 text NOT NULL,profile jsonb NOT NULL,language text NOT NULL,
      cursor_position integer NOT NULL DEFAULT 0 CHECK(cursor_position>=0 AND cursor_position<=source_length),
      completed boolean NOT NULL DEFAULT false,chunk_count integer NOT NULL DEFAULT 0 CHECK(chunk_count>=0),
      PRIMARY KEY(project_id,document_key),CHECK(NOT completed OR cursor_position=source_length)
    )""")
    op.execute(
        "CREATE INDEX source_search_document_hashes ON source_search_documents(project_id,source_sha256)"
    )
    op.execute("""CREATE TABLE source_search_document_chunks(
      project_id uuid NOT NULL,document_key text NOT NULL,
      start_offset integer NOT NULL CHECK(start_offset>=0),end_offset integer NOT NULL CHECK(end_offset>start_offset),
      language text NOT NULL,lexical tsvector NOT NULL,exact_terms text[] NOT NULL,phrase_text text NOT NULL,embedding vector(384) NOT NULL,
      PRIMARY KEY(project_id,document_key,start_offset,end_offset),
      FOREIGN KEY(project_id,document_key) REFERENCES source_search_documents(project_id,document_key) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX source_search_document_fts ON source_search_document_chunks USING gin(lexical)")
    op.execute(
        "CREATE INDEX source_search_document_entities ON source_search_document_chunks USING gin(exact_terms)"
    )
    op.execute("""CREATE TABLE source_search_memberships(
      project_id uuid NOT NULL,shared_input_record_id uuid NOT NULL,profile_sha256 text NOT NULL,
      source_id text NOT NULL,document_key text NOT NULL,
      PRIMARY KEY(project_id,shared_input_record_id,profile_sha256,source_id),
      FOREIGN KEY(project_id,shared_input_record_id) REFERENCES runtime_records(project_id,record_id) ON DELETE CASCADE,
      FOREIGN KEY(project_id,document_key) REFERENCES source_search_documents(project_id,document_key) ON DELETE CASCADE
    )""")
    op.execute(
        "CREATE INDEX source_search_member_documents ON source_search_memberships(project_id,document_key)"
    )
    op.execute("""CREATE TABLE source_search_scope_dependencies(
      project_id uuid NOT NULL,shared_input_record_id uuid NOT NULL,sha256 text NOT NULL,
      PRIMARY KEY(project_id,shared_input_record_id,sha256),
      FOREIGN KEY(project_id,shared_input_record_id) REFERENCES runtime_records(project_id,record_id) ON DELETE CASCADE
    )""")
    op.execute(
        "CREATE INDEX source_search_scope_hashes ON source_search_scope_dependencies(project_id,sha256)"
    )
    op.execute("""CREATE FUNCTION revoke_reusable_source_search() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM project_id FROM projects WHERE project_id=NEW.project_id FOR UPDATE;
        DELETE FROM source_search_documents d WHERE d.project_id=NEW.project_id AND
          (d.source_sha256=NEW.sha256 OR EXISTS(
            SELECT 1 FROM source_search_memberships m JOIN source_search_scope_dependencies s
              ON s.project_id=m.project_id AND s.shared_input_record_id=m.shared_input_record_id
            WHERE m.project_id=d.project_id AND m.document_key=d.document_key AND s.sha256=NEW.sha256));
        DELETE FROM source_search_results r WHERE r.project_id=NEW.project_id AND EXISTS(
          SELECT 1 FROM source_search_scope_dependencies s WHERE s.project_id=r.project_id
            AND s.shared_input_record_id=r.shared_input_record_id AND s.sha256=NEW.sha256);
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER reusable_source_search_withdrawal BEFORE INSERT ON blob_access_revocations
      FOR EACH ROW EXECUTE FUNCTION revoke_reusable_source_search()""")
    op.execute("""CREATE FUNCTION reject_withdrawn_reusable_search() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE fingerprint text;
      BEGIN
        PERFORM project_id FROM projects WHERE project_id=NEW.project_id FOR SHARE;
        IF TG_TABLE_NAME='source_search_documents' THEN fingerprint:=NEW.source_sha256; ELSE fingerprint:=NEW.sha256; END IF;
        IF EXISTS(SELECT 1 FROM blob_access_revocations v WHERE v.project_id=NEW.project_id AND v.sha256=fingerprint) THEN
          RAISE EXCEPTION 'withdrawn_source_search_projection' USING ERRCODE='42501';
        END IF;
        RETURN NEW;
      END $$""")
    for table in ("source_search_documents", "source_search_scope_dependencies"):
        op.execute(
            f"CREATE TRIGGER reusable_source_search_access BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_withdrawn_reusable_search()"
        )


def downgrade():
    raise RuntimeError("Reusable document projections require their withdrawal barriers.")
