"""Authoritative private execution checkpoints, fixed run head and owned objects."""

from pathlib import Path

from alembic import op

revision = "0028_private_execution"
down_revision = "0027_execution_journal"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    raise RuntimeError("Private execution receipts and their provenance must be retained.")
