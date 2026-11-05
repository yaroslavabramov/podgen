"""Preserve all valid Unicode, including escaped U+0000 (unsupported by jsonb)."""

from alembic import op

revision = "0003_full_unicode_json"
down_revision = "0002_blob_dedup"
branch_labels = None
depends_on = None


def upgrade():
    for table in (
        "artifact_revisions",
        "runtime_records",
        "acceptance_records",
        "revocations",
        "integration_events",
        "dependency_revalidations",
        "operational_records",
        "outbox",
    ):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN payload TYPE json USING payload::json")


def downgrade():
    raise RuntimeError("jsonb cannot represent every preserved Unicode string; downgrade would lose meaning.")
