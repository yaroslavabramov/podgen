"""Version V2 run corpus snapshots and scope run data.

Revision ID: c5e8a1d4f2b7
Revises: a8f2c9d4e6b1
Create Date: 2026-07-14 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c5e8a1d4f2b7"
down_revision: Union[str, None] = "a8f2c9d4e6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # f4 added is_current with a true backfill, so legacy groups with multiple
    # versions could have several current rows. Normalize them before new code
    # starts relying on a single current artifact.
    op.execute(
        """
        WITH ranked_artifacts AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY generation_run_id, artifact_type, artifact_key
                    ORDER BY version DESC, created_at DESC, id DESC
                ) AS version_rank,
                FIRST_VALUE(id) OVER (
                    PARTITION BY generation_run_id, artifact_type, artifact_key
                    ORDER BY version DESC, created_at DESC, id DESC
                ) AS latest_id
            FROM v2_artifacts
        )
        UPDATE v2_artifacts AS artifact
        SET
            is_current = (ranked.version_rank = 1),
            status = CASE
                WHEN ranked.version_rank = 1 THEN artifact.status
                ELSE 'superseded'
            END,
            superseded_by_id = CASE
                WHEN ranked.version_rank = 1 THEN NULL
                ELSE ranked.latest_id
            END
        FROM ranked_artifacts AS ranked
        WHERE artifact.id = ranked.id
        """
    )
    op.add_column("run_corpus_views", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("run_corpus_views", sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("run_corpus_views", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column(
        "run_corpus_views",
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_run_corpus_views_supersedes",
        "run_corpus_views",
        "run_corpus_views",
        ["supersedes_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("uq_run_corpus_view_generation_run", "run_corpus_views", type_="unique")
    op.create_unique_constraint(
        "uq_run_corpus_view_generation_run_revision_attempt",
        "run_corpus_views",
        ["generation_run_id", "revision", "attempt"],
    )
    op.create_index(
        "uq_run_corpus_views_current",
        "run_corpus_views",
        ["generation_run_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    for table in ("v2_artifacts", "v2_tool_traces", "evidence_items"):
        op.add_column(table, sa.Column("corpus_revision", sa.Integer(), nullable=False, server_default="1"))
    for table in ("v2_tool_traces", "evidence_items"):
        op.add_column(table, sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
    op.create_index(
        "ix_v2_tool_traces_run_scope",
        "v2_tool_traces",
        ["generation_run_id", "corpus_revision", "attempt", "phase"],
    )
    op.create_index(
        "ix_evidence_items_run_scope",
        "evidence_items",
        ["generation_run_id", "corpus_revision", "attempt"],
    )
    op.create_index(
        "ix_v2_artifacts_run_scope",
        "v2_artifacts",
        ["generation_run_id", "corpus_revision", "attempt"],
    )
    op.drop_index("ix_evidence_items_dedupe", table_name="evidence_items")
    op.create_index(
        "ix_evidence_items_dedupe",
        "evidence_items",
        [
            "generation_run_id",
            "corpus_revision",
            "attempt",
            "source_version_id",
            "snippet_hash",
            "retrieval_channel",
            "purpose",
        ],
    )

    op.alter_column("run_corpus_views", "revision", server_default=None)
    op.alter_column("run_corpus_views", "attempt", server_default=None)
    op.alter_column("run_corpus_views", "is_current", server_default=None)
    for table in ("v2_artifacts", "v2_tool_traces", "evidence_items"):
        op.alter_column(table, "corpus_revision", server_default=None)
    for table in ("v2_tool_traces", "evidence_items"):
        op.alter_column(table, "attempt", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_evidence_items_dedupe", table_name="evidence_items")
    op.create_index(
        "ix_evidence_items_dedupe",
        "evidence_items",
        ["generation_run_id", "source_version_id", "snippet_hash", "retrieval_channel", "purpose"],
    )
    op.drop_index("ix_v2_artifacts_run_scope", table_name="v2_artifacts")
    op.drop_index("ix_evidence_items_run_scope", table_name="evidence_items")
    op.drop_index("ix_v2_tool_traces_run_scope", table_name="v2_tool_traces")
    for table in ("evidence_items", "v2_tool_traces"):
        op.drop_column(table, "attempt")
    for table in ("evidence_items", "v2_tool_traces", "v2_artifacts"):
        op.drop_column(table, "corpus_revision")

    op.drop_index("uq_run_corpus_views_current", table_name="run_corpus_views")
    op.drop_constraint(
        "uq_run_corpus_view_generation_run_revision_attempt",
        "run_corpus_views",
        type_="unique",
    )
    op.execute(
        """
        DELETE FROM run_corpus_views
        WHERE id NOT IN (
            SELECT DISTINCT ON (generation_run_id) id
            FROM run_corpus_views
            ORDER BY generation_run_id, revision DESC, attempt DESC
        )
        """
    )
    op.create_unique_constraint(
        "uq_run_corpus_view_generation_run",
        "run_corpus_views",
        ["generation_run_id"],
    )
    op.drop_constraint("fk_run_corpus_views_supersedes", "run_corpus_views", type_="foreignkey")
    op.drop_column("run_corpus_views", "supersedes_id")
    op.drop_column("run_corpus_views", "is_current")
    op.drop_column("run_corpus_views", "attempt")
    op.drop_column("run_corpus_views", "revision")
