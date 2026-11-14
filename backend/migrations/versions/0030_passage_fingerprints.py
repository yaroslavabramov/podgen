"""Original passage identities; old pinned rankings and vectors remain intact."""

from alembic import op

revision = "0030_passage_fingerprints"
down_revision = "0029_elapsed_authorizations"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("source_search_chunks", "source_search_document_chunks"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN passage_sha256 text CHECK(passage_sha256 ~ '^[0-9a-f]{{64}}$')"
        )


def downgrade():
    for table in ("source_search_document_chunks", "source_search_chunks"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN passage_sha256")
