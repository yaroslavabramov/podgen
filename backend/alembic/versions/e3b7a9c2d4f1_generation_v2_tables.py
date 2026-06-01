"""Add generation V2 source workspace and artifact tables.

Revision ID: e3b7a9c2d4f1
Revises: d92c8a1f4b6e
Create Date: 2026-06-01 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e3b7a9c2d4f1"
down_revision: Union[str, None] = "d92c8a1f4b6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_representations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_file_id", sa.String(length=255), nullable=True),
        sa.Column("provider_vector_store_id", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_source_representations_source_id", "source_representations", ["source_id"])
    op.create_index("ix_source_representations_content_hash", "source_representations", ["content_hash"])
    op.create_index(
        "ix_source_representations_source_kind",
        "source_representations",
        ["source_id", "kind"],
    )
    op.create_index(
        "ix_source_representations_provider_file",
        "source_representations",
        ["provider", "provider_file_id"],
    )

    op.create_table(
        "corpus_workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_vector_store_id", sa.String(length=255), nullable=True),
        sa.Column("source_set_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_corpus_workspaces_project_id", "corpus_workspaces", ["project_id"])
    op.create_index("ix_corpus_workspaces_generation_run_id", "corpus_workspaces", ["generation_run_id"])
    op.create_index("ix_corpus_workspaces_source_set_hash", "corpus_workspaces", ["source_set_hash"])
    op.create_index(
        "ix_corpus_workspaces_project_run",
        "corpus_workspaces",
        ["project_id", "generation_run_id"],
    )

    op.create_table(
        "v2_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("artifact_key", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_artifact_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "generation_run_id",
            "artifact_type",
            "artifact_key",
            "version",
            name="uq_v2_artifact_version",
        ),
    )
    op.create_index("ix_v2_artifacts_generation_run_id", "v2_artifacts", ["generation_run_id"])
    op.create_index("ix_v2_artifacts_run_type", "v2_artifacts", ["generation_run_id", "artifact_type"])

    op.create_table(
        "v2_tool_traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", sa.String(length=80), nullable=False),
        sa.Column("agent_name", sa.String(length=120), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("input_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_call_id", sa.String(length=255), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_v2_tool_traces_generation_run_id", "v2_tool_traces", ["generation_run_id"])
    op.create_index("ix_v2_tool_traces_run_phase", "v2_tool_traces", ["generation_run_id", "phase"])
    op.create_index("ix_v2_tool_traces_provider_call", "v2_tool_traces", ["provider_call_id"])


def downgrade() -> None:
    op.drop_index("ix_v2_tool_traces_provider_call", table_name="v2_tool_traces")
    op.drop_index("ix_v2_tool_traces_run_phase", table_name="v2_tool_traces")
    op.drop_index("ix_v2_tool_traces_generation_run_id", table_name="v2_tool_traces")
    op.drop_table("v2_tool_traces")

    op.drop_index("ix_v2_artifacts_run_type", table_name="v2_artifacts")
    op.drop_index("ix_v2_artifacts_generation_run_id", table_name="v2_artifacts")
    op.drop_table("v2_artifacts")

    op.drop_index("ix_corpus_workspaces_project_run", table_name="corpus_workspaces")
    op.drop_index("ix_corpus_workspaces_source_set_hash", table_name="corpus_workspaces")
    op.drop_index("ix_corpus_workspaces_generation_run_id", table_name="corpus_workspaces")
    op.drop_index("ix_corpus_workspaces_project_id", table_name="corpus_workspaces")
    op.drop_table("corpus_workspaces")

    op.drop_index("ix_source_representations_provider_file", table_name="source_representations")
    op.drop_index("ix_source_representations_source_kind", table_name="source_representations")
    op.drop_index("ix_source_representations_content_hash", table_name="source_representations")
    op.drop_index("ix_source_representations_source_id", table_name="source_representations")
    op.drop_table("source_representations")
