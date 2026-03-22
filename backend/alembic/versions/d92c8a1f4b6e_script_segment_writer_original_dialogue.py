"""Add writer_original_dialogue_json to script segments.

Revision ID: d92c8a1f4b6e
Revises: b1a7d42c9e6f
Create Date: 2026-03-22 12:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d92c8a1f4b6e"
down_revision: Union[str, None] = "b1a7d42c9e6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "script_segments",
        sa.Column("writer_original_dialogue_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("script_segments", "writer_original_dialogue_json")
