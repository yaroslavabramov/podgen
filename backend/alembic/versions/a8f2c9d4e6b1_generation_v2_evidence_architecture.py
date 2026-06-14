"""Add generation V2 source versions and evidence architecture.

Revision ID: a8f2c9d4e6b1
Revises: f4a1c8b9d2e3
Create Date: 2026-06-15 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a8f2c9d4e6b1"
down_revision: Union[str, None] = "f4a1c8b9d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "source_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_file_id", sa.String(length=255), nullable=True),
        sa.Column("provider_vector_store_file_id", sa.String(length=255), nullable=True),
        sa.Column("upload_status", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("supersedes_source_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["supersedes_source_version_id"], ["source_versions.id"]),
    )
    op.create_index("ix_source_versions_source_id", "source_versions", ["source_id"])
    op.create_index("ix_source_versions_project_id", "source_versions", ["project_id"])
    op.create_index("ix_source_versions_content_hash", "source_versions", ["content_hash"])
    op.create_index("ix_source_versions_source_active", "source_versions", ["source_id", "is_active"])
    op.create_index("ix_source_versions_project_kind", "source_versions", ["project_id", "kind"])

    op.create_table(
        "provider_corpus_indexes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_vector_store_id", sa.String(length=255), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "provider", "scope", name="uq_provider_corpus_project_provider_scope"),
    )
    op.create_index("ix_provider_corpus_indexes_project_id", "provider_corpus_indexes", ["project_id"])
    op.create_index("ix_provider_corpus_indexes_project", "provider_corpus_indexes", ["project_id"])

    op.create_table(
        "provider_corpus_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider_index_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_file_id", sa.String(length=255), nullable=True),
        sa.Column("provider_vector_store_file_id", sa.String(length=255), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("cleanup_status", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["provider_index_id"], ["provider_corpus_indexes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"]),
        sa.UniqueConstraint("provider_index_id", "source_version_id", name="uq_provider_corpus_file_version"),
    )
    op.create_index("ix_provider_corpus_files_provider_index_id", "provider_corpus_files", ["provider_index_id"])
    op.create_index("ix_provider_corpus_files_source_id", "provider_corpus_files", ["source_id"])
    op.create_index("ix_provider_corpus_files_source_version_id", "provider_corpus_files", ["source_version_id"])
    op.create_index("ix_provider_corpus_files_index_status", "provider_corpus_files", ["provider_index_id", "status"])
    op.create_index("ix_provider_corpus_files_source_version", "provider_corpus_files", ["source_version_id"])

    op.create_table(
        "run_corpus_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_index_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("selected_source_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("selected_source_version_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active_source_filter", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("overlay_index_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("eligible_provider_file_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("readiness_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_index_id"], ["provider_corpus_indexes.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("generation_run_id", name="uq_run_corpus_view_generation_run"),
    )
    op.create_index("ix_run_corpus_views_generation_run_id", "run_corpus_views", ["generation_run_id"])
    op.create_index("ix_run_corpus_views_project_id", "run_corpus_views", ["project_id"])
    op.create_index("ix_run_corpus_views_project", "run_corpus_views", ["project_id"])
    op.create_index("ix_run_corpus_views_manifest_hash", "run_corpus_views", ["manifest_hash"])

    op.create_table(
        "evidence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_vector_store_id", sa.String(length=255), nullable=True),
        sa.Column("provider_file_id", sa.String(length=255), nullable=True),
        sa.Column("provider_vector_store_file_id", sa.String(length=255), nullable=True),
        sa.Column("response_id", sa.String(length=255), nullable=True),
        sa.Column("tool_call_id", sa.String(length=255), nullable=True),
        sa.Column("retrieval_channel", sa.String(length=50), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("purpose", sa.String(length=120), nullable=True),
        sa.Column("search_result_index", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("snippet_text", sa.Text(), nullable=False),
        sa.Column("normalized_quote_text", sa.Text(), nullable=True),
        sa.Column("locator_text", sa.Text(), nullable=True),
        sa.Column("locator_type", sa.String(length=50), nullable=False),
        sa.Column("evidence_strength", sa.String(length=50), nullable=False),
        sa.Column("snippet_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"]),
    )
    op.create_index("ix_evidence_items_generation_run_id", "evidence_items", ["generation_run_id"])
    op.create_index("ix_evidence_items_project_id", "evidence_items", ["project_id"])
    op.create_index("ix_evidence_items_source_id", "evidence_items", ["source_id"])
    op.create_index("ix_evidence_items_source_version_id", "evidence_items", ["source_version_id"])
    op.create_index("ix_evidence_items_run", "evidence_items", ["generation_run_id"])
    op.create_index("ix_evidence_items_project_source", "evidence_items", ["project_id", "source_id"])
    op.create_index("ix_evidence_items_source_version", "evidence_items", ["source_version_id"])
    op.create_index(
        "ix_evidence_items_dedupe",
        "evidence_items",
        ["generation_run_id", "source_version_id", "snippet_hash", "retrieval_channel", "purpose"],
    )

    _backfill_source_versions()
    _backfill_provider_corpus_indexes()


def downgrade() -> None:
    op.drop_index("ix_evidence_items_dedupe", table_name="evidence_items")
    op.drop_index("ix_evidence_items_source_version", table_name="evidence_items")
    op.drop_index("ix_evidence_items_project_source", table_name="evidence_items")
    op.drop_index("ix_evidence_items_run", table_name="evidence_items")
    op.drop_index("ix_evidence_items_source_version_id", table_name="evidence_items")
    op.drop_index("ix_evidence_items_source_id", table_name="evidence_items")
    op.drop_index("ix_evidence_items_project_id", table_name="evidence_items")
    op.drop_index("ix_evidence_items_generation_run_id", table_name="evidence_items")
    op.drop_table("evidence_items")

    op.drop_index("ix_run_corpus_views_manifest_hash", table_name="run_corpus_views")
    op.drop_index("ix_run_corpus_views_project", table_name="run_corpus_views")
    op.drop_index("ix_run_corpus_views_project_id", table_name="run_corpus_views")
    op.drop_index("ix_run_corpus_views_generation_run_id", table_name="run_corpus_views")
    op.drop_table("run_corpus_views")

    op.drop_index("ix_provider_corpus_files_source_version", table_name="provider_corpus_files")
    op.drop_index("ix_provider_corpus_files_index_status", table_name="provider_corpus_files")
    op.drop_index("ix_provider_corpus_files_source_version_id", table_name="provider_corpus_files")
    op.drop_index("ix_provider_corpus_files_source_id", table_name="provider_corpus_files")
    op.drop_index("ix_provider_corpus_files_provider_index_id", table_name="provider_corpus_files")
    op.drop_table("provider_corpus_files")

    op.drop_index("ix_provider_corpus_indexes_project", table_name="provider_corpus_indexes")
    op.drop_index("ix_provider_corpus_indexes_project_id", table_name="provider_corpus_indexes")
    op.drop_table("provider_corpus_indexes")

    op.drop_index("ix_source_versions_project_kind", table_name="source_versions")
    op.drop_index("ix_source_versions_source_active", table_name="source_versions")
    op.drop_index("ix_source_versions_content_hash", table_name="source_versions")
    op.drop_index("ix_source_versions_project_id", table_name="source_versions")
    op.drop_index("ix_source_versions_source_id", table_name="source_versions")
    op.drop_table("source_versions")


def _backfill_source_versions() -> None:
    op.execute(
        """
        INSERT INTO source_versions (
            id, source_id, project_id, kind, storage_path, content_hash, provider, provider_file_id,
            provider_vector_store_file_id, upload_status, status, metadata, supersedes_source_version_id,
            is_active, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            id,
            project_id,
            CASE
                WHEN file_path IS NOT NULL THEN 'original_file'
                WHEN original_url IS NOT NULL THEN 'original_url'
                WHEN source_type IN ('audio', 'video') THEN 'transcript'
                ELSE 'extracted_text'
            END,
            COALESCE(file_path, original_url, raw_text_path),
            encode(digest(COALESCE(file_path, original_url, raw_text_path, id::text), 'sha256'), 'hex'),
            NULL,
            NULL,
            NULL,
            'pending',
            CASE WHEN COALESCE(file_path, original_url, raw_text_path) IS NULL THEN 'failed' ELSE 'ready' END,
            jsonb_build_object('backfilled', true, 'source_type', source_type),
            NULL,
            true,
            COALESCE(created_at, now()),
            COALESCE(status_changed_at, now())
        FROM sources
        WHERE file_path IS NOT NULL OR original_url IS NOT NULL OR raw_text_path IS NOT NULL
        """
    )


def _backfill_provider_corpus_indexes() -> None:
    op.execute(
        """
        INSERT INTO provider_corpus_indexes (
            id, project_id, provider, provider_vector_store_id, scope, status, config,
            error_message, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            project_id,
            'openai',
            NULL,
            'project',
            'pending',
            jsonb_build_object('backfilled', true, 'requires_sync', true),
            NULL,
            now(),
            now()
        FROM source_versions
        GROUP BY project_id
        ON CONFLICT (project_id, provider, scope) DO NOTHING
        """
    )
