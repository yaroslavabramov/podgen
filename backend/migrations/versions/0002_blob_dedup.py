"""Enforce project-local blob deduplication for concurrent and S3 retries."""

from alembic import op

revision = "0002_blob_dedup"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("blobs_project_hash_media", "blobs", ["project_id", "sha256", "media_type"])


def downgrade():
    raise RuntimeError("Immutable history requires backup restore, not destructive downgrade.")
