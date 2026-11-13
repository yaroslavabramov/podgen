"""Lookup reusable legacy computation by project-local content and pinned settings."""

from alembic import op

revision = "0026_legacy_source_lookup"
down_revision = "0025_runtime_references"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX source_search_legacy_content "
        "ON source_search_indexes(project_id,source_sha256,source_length,profile_sha256,language)"
    )


def downgrade():
    op.drop_index("source_search_legacy_content", table_name="source_search_indexes")
