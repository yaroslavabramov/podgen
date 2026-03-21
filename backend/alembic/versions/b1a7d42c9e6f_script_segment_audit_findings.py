"""Add audit_findings to script segments.

Revision ID: b1a7d42c9e6f
Revises: 6e3b1d4f9a2c
Create Date: 2026-03-21 19:45:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1a7d42c9e6f"
down_revision: Union[str, None] = "6e3b1d4f9a2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("script_segments", sa.Column("audit_findings", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("script_segments", "audit_findings")
