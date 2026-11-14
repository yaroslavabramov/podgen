"""Index elapsed grants without decoding unrelated raw JSON scalar values."""

from alembic import op

revision = "0034_elapsed_safe_reference_view"
down_revision = "0033_validated_media_descriptors"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP INDEX runtime_elapsed_authorizations_by_run")
    op.execute("""CREATE INDEX runtime_elapsed_authorizations_by_run
        ON runtime_records(project_id,(podgen_runtime_reference_document(payload)->>'run_id'))
        WHERE record_kind='tool_execution'
          AND podgen_runtime_reference_document(payload)->>'contract'='resume-elapsed-authorization/1'""")


def downgrade():
    raise RuntimeError("Raw runtime JSON must remain lossless, including NUL scalars")
