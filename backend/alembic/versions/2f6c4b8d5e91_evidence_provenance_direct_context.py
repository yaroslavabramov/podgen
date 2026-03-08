"""Add direct/contextual question relevance to evidence provenance.

Revision ID: 2f6c4b8d5e91
Revises: f4d2c1a7b9e3
Create Date: 2026-03-08 15:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "2f6c4b8d5e91"
down_revision: Union[str, None] = "f4d2c1a7b9e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evidence_provenance",
        sa.Column(
            "direct_or_contextual",
            sa.Enum(
                "DIRECT",
                "CONTEXTUAL",
                name="evidence_question_relevance",
                native_enum=False,
                length=20,
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("evidence_provenance", "direct_or_contextual")
