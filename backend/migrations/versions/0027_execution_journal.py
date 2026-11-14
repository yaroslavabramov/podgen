"""Run-bound execution journal and replaceable flat input metadata."""

from pathlib import Path

from alembic import op

revision = "0027_execution_journal"
down_revision = "0026_legacy_source_lookup"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    raise RuntimeError("Bound execution records and their privacy bindings cannot be discarded.")
