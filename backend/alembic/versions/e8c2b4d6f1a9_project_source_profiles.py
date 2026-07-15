"""Add reusable project-level source profiles.

Revision ID: e8c2b4d6f1a9
Revises: d7a4e2c9b1f6
Create Date: 2026-07-15 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e8c2b4d6f1a9"
down_revision: Union[str, None] = "d7a4e2c9b1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_source_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["project_source_profiles.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_version_id",
            "schema_version",
            "version",
            name="uq_project_source_profile_version",
        ),
    )
    op.create_index(
        "ix_project_source_profiles_project_id",
        "project_source_profiles",
        ["project_id"],
    )
    op.create_index(
        "ix_project_source_profiles_source_id",
        "project_source_profiles",
        ["source_id"],
    )
    op.create_index(
        "ix_project_source_profiles_source_version_id",
        "project_source_profiles",
        ["source_version_id"],
    )
    op.create_index(
        "ix_project_source_profiles_project_source",
        "project_source_profiles",
        ["project_id", "source_id"],
    )
    op.create_index(
        "ix_project_source_profiles_fingerprint",
        "project_source_profiles",
        ["source_fingerprint"],
    )
    op.create_index(
        "uq_project_source_profiles_current",
        "project_source_profiles",
        ["source_version_id", "schema_version"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    op.drop_table("project_source_profiles")
