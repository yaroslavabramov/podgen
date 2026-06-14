"""Add generation V2 artifact metadata.

Revision ID: f4a1c8b9d2e3
Revises: e3b7a9c2d4f1
Create Date: 2026-06-02 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f4a1c8b9d2e3"
down_revision: Union[str, None] = "e3b7a9c2d4f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("v2_artifacts", sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("v2_artifacts", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("v2_artifacts", sa.Column("prompt_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "v2_artifacts",
        sa.Column("input_artifact_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
    )
    op.add_column("v2_artifacts", sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_v2_artifacts_superseded_by",
        "v2_artifacts",
        "v2_artifacts",
        ["superseded_by_id"],
        ["id"],
    )
    op.create_index(
        "ix_v2_artifacts_current",
        "v2_artifacts",
        ["generation_run_id", "artifact_type", "artifact_key", "is_current"],
    )
    op.alter_column("v2_artifacts", "attempt", server_default=None)
    op.alter_column("v2_artifacts", "is_current", server_default=None)
    op.alter_column("v2_artifacts", "input_artifact_ids", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_v2_artifacts_current", table_name="v2_artifacts")
    op.drop_constraint("fk_v2_artifacts_superseded_by", "v2_artifacts", type_="foreignkey")
    op.drop_column("v2_artifacts", "superseded_by_id")
    op.drop_column("v2_artifacts", "input_artifact_ids")
    op.drop_column("v2_artifacts", "prompt_hash")
    op.drop_column("v2_artifacts", "is_current")
    op.drop_column("v2_artifacts", "attempt")
