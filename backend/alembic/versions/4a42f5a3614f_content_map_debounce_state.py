"""Add project-level content map debounce state.

Revision ID: 4a42f5a3614f
Revises: 9d5a6d6c0f0a
Create Date: 2026-02-26 12:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "4a42f5a3614f"
down_revision: Union[str, None] = "9d5a6d6c0f0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("content_map_dirty", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("projects", sa.Column("content_map_last_requested_at", sa.DateTime(), nullable=True))
    op.add_column("projects", sa.Column("content_map_last_built_at", sa.DateTime(), nullable=True))
    op.alter_column("projects", "content_map_dirty", server_default=None)


def downgrade() -> None:
    op.drop_column("projects", "content_map_last_built_at")
    op.drop_column("projects", "content_map_last_requested_at")
    op.drop_column("projects", "content_map_dirty")
