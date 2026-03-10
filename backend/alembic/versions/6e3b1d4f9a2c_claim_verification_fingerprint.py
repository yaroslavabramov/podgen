"""Add persistent verification fingerprint to claims.

Revision ID: 6e3b1d4f9a2c
Revises: 2f6c4b8d5e91
Create Date: 2026-03-10 15:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6e3b1d4f9a2c"
down_revision: Union[str, None] = "2f6c4b8d5e91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("claims", sa.Column("verification_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("claims", "verification_fingerprint")
