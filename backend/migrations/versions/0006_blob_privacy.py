"""Project-scoped privacy tombstones survive aliasing, deduplication and quality repair."""

from alembic import op

revision = "0006_blob_privacy"
down_revision = "0005_impact_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE blob_access_revocations(
      project_id uuid NOT NULL, sha256 text NOT NULL, event_id uuid NOT NULL, reason text NOT NULL,
      PRIMARY KEY(project_id,sha256,event_id),
      FOREIGN KEY(project_id,event_id) REFERENCES invalidations(project_id,event_id)
    )""")
    op.execute("""CREATE TABLE revalidation_privacy_links(
      project_id uuid NOT NULL, subject_id uuid NOT NULL, input_id uuid NOT NULL,
      PRIMARY KEY(project_id,subject_id,input_id),
      FOREIGN KEY(project_id,subject_id) REFERENCES artifact_revisions(project_id,revision_id),
      FOREIGN KEY(project_id,input_id) REFERENCES artifact_revisions(project_id,revision_id)
    )""")
    op.execute("""INSERT INTO revalidation_privacy_links
      SELECT DISTINCT d.project_id,d.subject_id,(r->>'revision_id')::uuid
      FROM dependency_revalidations d,
      LATERAL json_array_elements(d.payload->'dispositions') AS x,
      LATERAL json_array_elements(x->'current_input_refs') AS r ON CONFLICT DO NOTHING""")
    for table in ("blob_access_revocations", "revalidation_privacy_links"):
        op.execute(
            f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )


def downgrade():
    raise RuntimeError("Privacy tombstones must not be removed by schema downgrade.")
