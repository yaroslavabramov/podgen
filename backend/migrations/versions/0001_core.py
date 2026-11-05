"""Immutable canonical store, scoped links, catalog and decision ledgers."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(text(Path(__file__).with_suffix(".sql").read_text()))


def downgrade():
    raise RuntimeError("Canonical history is irreversible; restore a verified backup instead.")
