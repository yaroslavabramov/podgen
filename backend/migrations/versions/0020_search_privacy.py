"""Retrieval projections follow blob withdrawals and cannot race their re-publication."""

from alembic import op

revision = "0020_search_privacy"
down_revision = "0019_source_search"
branch_labels = None
depends_on = None


def upgrade():
    # The preceding prototype projections are disposable; rebuilding registers their full blob closure.
    op.execute("DELETE FROM source_search_indexes")
    op.execute("""CREATE TABLE source_search_dependencies(
      project_id uuid NOT NULL,shared_input_record_id uuid NOT NULL,profile_sha256 text NOT NULL,source_id text NOT NULL,sha256 text NOT NULL,
      PRIMARY KEY(project_id,shared_input_record_id,profile_sha256,source_id,sha256),
      FOREIGN KEY(project_id,shared_input_record_id,profile_sha256,source_id)
        REFERENCES source_search_indexes(project_id,shared_input_record_id,profile_sha256,source_id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX source_search_private_hashes ON source_search_dependencies(project_id,sha256)")
    op.execute("""CREATE FUNCTION revoke_source_search_projection() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM project_id FROM projects WHERE project_id=NEW.project_id FOR UPDATE;
        DELETE FROM source_search_indexes i WHERE i.project_id=NEW.project_id AND
          (i.source_sha256=NEW.sha256 OR EXISTS(SELECT 1 FROM source_search_dependencies d
            WHERE d.project_id=i.project_id AND d.shared_input_record_id=i.shared_input_record_id
              AND d.profile_sha256=i.profile_sha256 AND d.source_id=i.source_id AND d.sha256=NEW.sha256));
        RETURN NEW;
      END $$""")
    op.execute(
        "CREATE TRIGGER source_search_withdrawal BEFORE INSERT ON blob_access_revocations FOR EACH ROW EXECUTE FUNCTION revoke_source_search_projection()"
    )
    op.execute("""CREATE FUNCTION reject_withdrawn_search_projection() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE fingerprint text;
      BEGIN
        PERFORM project_id FROM projects WHERE project_id=NEW.project_id FOR SHARE;
        IF TG_TABLE_NAME='source_search_indexes' THEN fingerprint:=NEW.source_sha256; ELSE fingerprint:=NEW.sha256; END IF;
        IF EXISTS(SELECT 1 FROM blob_access_revocations v WHERE v.project_id=NEW.project_id AND v.sha256=fingerprint) THEN
          RAISE EXCEPTION 'withdrawn_source_search_projection' USING ERRCODE='42501';
        END IF;
        RETURN NEW;
      END $$""")
    for table in ("source_search_indexes", "source_search_dependencies"):
        op.execute(
            f"CREATE TRIGGER source_search_access BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_withdrawn_search_projection()"
        )


def downgrade():
    raise RuntimeError("Privacy barriers cannot be removed while retrieval projections remain addressable.")
