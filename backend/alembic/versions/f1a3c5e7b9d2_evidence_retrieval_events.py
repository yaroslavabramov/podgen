"""Separate canonical evidence from retrieval history.

Revision ID: f1a3c5e7b9d2
Revises: e8c2b4d6f1a9
Create Date: 2026-07-15 14:00:00.000000
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f1a3c5e7b9d2"
down_revision: Union[str, None] = "e8c2b4d6f1a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_retrieval_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("corpus_revision", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
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
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("search_result_index", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("locator_text", sa.Text(), nullable=True),
        sa.Column("locator_type", sa.String(length=50), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_evidence_retrieval_events_evidence", ["evidence_id"]),
        ("ix_evidence_retrieval_events_evidence_id", ["evidence_id"]),
        ("ix_evidence_retrieval_events_generation_run_id", ["generation_run_id"]),
        ("ix_evidence_retrieval_events_project_id", ["project_id"]),
        ("ix_evidence_retrieval_events_source_id", ["source_id"]),
        ("ix_evidence_retrieval_events_source_version_id", ["source_version_id"]),
        ("ix_evidence_retrieval_events_source_version", ["source_version_id"]),
        (
            "ix_evidence_retrieval_events_run_scope",
            ["generation_run_id", "corpus_revision", "attempt"],
        ),
    ):
        op.create_index(name, "evidence_retrieval_events", columns)

    evidence = sa.table(
        "evidence_items",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("generation_run_id", postgresql.UUID(as_uuid=True)),
        sa.column("corpus_revision", sa.Integer()),
        sa.column("attempt", sa.Integer()),
        sa.column("project_id", postgresql.UUID(as_uuid=True)),
        sa.column("source_id", postgresql.UUID(as_uuid=True)),
        sa.column("source_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("provider", sa.String()),
        sa.column("provider_vector_store_id", sa.String()),
        sa.column("provider_file_id", sa.String()),
        sa.column("provider_vector_store_file_id", sa.String()),
        sa.column("response_id", sa.String()),
        sa.column("tool_call_id", sa.String()),
        sa.column("retrieval_channel", sa.String()),
        sa.column("query_text", sa.Text()),
        sa.column("purpose", sa.String()),
        sa.column("search_result_index", sa.Integer()),
        sa.column("score", sa.Float()),
        sa.column("snippet_text", sa.Text()),
        sa.column("snippet_hash", sa.String()),
        sa.column("locator_text", sa.Text()),
        sa.column("locator_type", sa.String()),
        sa.column("evidence_strength", sa.String()),
        sa.column("metadata", postgresql.JSONB()),
        sa.column("created_at", sa.DateTime()),
    )
    events = sa.table(
        "evidence_retrieval_events",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("evidence_id", postgresql.UUID(as_uuid=True)),
        sa.column("generation_run_id", postgresql.UUID(as_uuid=True)),
        sa.column("corpus_revision", sa.Integer()),
        sa.column("attempt", sa.Integer()),
        sa.column("project_id", postgresql.UUID(as_uuid=True)),
        sa.column("source_id", postgresql.UUID(as_uuid=True)),
        sa.column("source_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("provider", sa.String()),
        sa.column("provider_vector_store_id", sa.String()),
        sa.column("provider_file_id", sa.String()),
        sa.column("provider_vector_store_file_id", sa.String()),
        sa.column("response_id", sa.String()),
        sa.column("tool_call_id", sa.String()),
        sa.column("retrieval_channel", sa.String()),
        sa.column("query_text", sa.Text()),
        sa.column("purpose", sa.Text()),
        sa.column("search_result_index", sa.Integer()),
        sa.column("score", sa.Float()),
        sa.column("locator_text", sa.Text()),
        sa.column("locator_type", sa.String()),
        sa.column("metadata", postgresql.JSONB()),
        sa.column("created_at", sa.DateTime()),
    )
    bind = op.get_bind()
    rows = list(bind.execute(sa.select(evidence).order_by(evidence.c.created_at, evidence.c.id)).mappings())

    survivor_by_key: dict[tuple, uuid.UUID] = {}
    for row in rows:
        canonical_text = " ".join(unicodedata.normalize("NFKC", str(row["snippet_text"] or "")).split())
        canonical_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        key = (
            row["generation_run_id"],
            int(row["corpus_revision"] or 1),
            int(row["attempt"] or 1),
            row["source_version_id"],
            canonical_hash,
        )
        survivor_id = survivor_by_key.get(key)
        metadata = dict(row["metadata"] or {})
        if survivor_id is None:
            survivor_by_key[key] = row["id"]
            new_hash = canonical_hash
        else:
            new_hash = hashlib.sha256(f"{canonical_hash}\x00legacy-duplicate:{row['id']}".encode("utf-8")).hexdigest()
            metadata.update(
                {
                    "legacy_duplicate": True,
                    "canonical_evidence_id": str(survivor_id),
                }
            )
        bind.execute(
            evidence.update().where(evidence.c.id == row["id"]).values(snippet_hash=new_hash, metadata=metadata)
        )
        bind.execute(
            events.insert().values(
                id=uuid.uuid4(),
                evidence_id=row["id"],
                generation_run_id=row["generation_run_id"],
                corpus_revision=int(row["corpus_revision"] or 1),
                attempt=int(row["attempt"] or 1),
                project_id=row["project_id"],
                source_id=row["source_id"],
                source_version_id=row["source_version_id"],
                provider=row["provider"],
                provider_vector_store_id=row["provider_vector_store_id"],
                provider_file_id=row["provider_file_id"],
                provider_vector_store_file_id=row["provider_vector_store_file_id"],
                response_id=row["response_id"],
                tool_call_id=row["tool_call_id"],
                retrieval_channel=row["retrieval_channel"],
                query_text=row["query_text"],
                purpose=row["purpose"],
                search_result_index=row["search_result_index"],
                score=row["score"],
                locator_text=row["locator_text"],
                locator_type=row["locator_type"] or "unknown",
                metadata={
                    "backfilled": True,
                    "evidence_strength": str(row["evidence_strength"] or "weak"),
                },
                created_at=row["created_at"],
            )
        )

    op.drop_index("ix_evidence_items_dedupe", table_name="evidence_items")
    op.create_unique_constraint(
        "uq_evidence_items_scope_source_snippet",
        "evidence_items",
        [
            "generation_run_id",
            "corpus_revision",
            "attempt",
            "source_version_id",
            "snippet_hash",
        ],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_evidence_items_scope_source_snippet",
        "evidence_items",
        type_="unique",
    )
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
    op.drop_table("evidence_retrieval_events")
