"""Versioned acquisition reference contracts, preserving legacy projections."""

from pathlib import Path

from alembic import op

revision = "0035_acquisition_publication"
down_revision = "0034_elapsed_safe_reference_view"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    raise RuntimeError("Acquisition receipts and reference contracts must be retained")
