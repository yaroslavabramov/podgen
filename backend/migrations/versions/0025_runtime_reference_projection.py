"""Typed runtime-reference indexes for bounded authorization and privacy traversal."""

from pathlib import Path

from alembic import op

revision = "0025_runtime_references"
down_revision = "0024_reusable_source_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    raise RuntimeError("Reference projections must not be removed while indexed writers are active.")
