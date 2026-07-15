from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus_helpers import (
    canonical_provider_version,
    versions_by_source,
)
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, SourceVersion


@dataclass(frozen=True)
class ReconciliationContext:
    scope_source_ids: set[uuid.UUID]
    versions_by_id: dict[uuid.UUID, SourceVersion]
    canonical_by_source: dict[uuid.UUID, SourceVersion]
    rows_by_provider_file_id: dict[str, ProviderCorpusFile]
    rows_by_version_id: dict[uuid.UUID, ProviderCorpusFile]
    summary: dict[str, Any]


async def build_reconciliation_context(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
    *,
    source_ids: set[uuid.UUID] | None = None,
) -> ReconciliationContext:
    scope_source_ids = source_ids or {version.source_id for version in source_versions}
    provider_rows = await load_provider_rows(db, provider_index, scope_source_ids)
    return ReconciliationContext(
        scope_source_ids=scope_source_ids,
        versions_by_id={version.id: version for version in source_versions},
        canonical_by_source=canonical_versions_by_source(source_versions),
        rows_by_provider_file_id={row.provider_file_id: row for row in provider_rows if row.provider_file_id},
        rows_by_version_id={row.source_version_id: row for row in provider_rows},
        summary=initial_reconciliation_summary(),
    )


async def load_provider_rows(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
    scope_source_ids: set[uuid.UUID],
) -> list[ProviderCorpusFile]:
    file_row_conditions = [ProviderCorpusFile.provider_index_id == provider_index.id]
    if scope_source_ids:
        file_row_conditions.append(ProviderCorpusFile.source_id.in_(list(scope_source_ids)))
    file_rows_result = await db.execute(select(ProviderCorpusFile).where(*file_row_conditions))
    return list(file_rows_result.scalars())


def canonical_versions_by_source(source_versions: list[SourceVersion]) -> dict[uuid.UUID, SourceVersion]:
    return {
        source_id: canonical
        for source_id, versions in versions_by_source(
            [version for version in source_versions if version.is_active]
        ).items()
        if (canonical := canonical_provider_version(versions)) is not None
    }


def initial_reconciliation_summary() -> dict[str, Any]:
    return {
        "adopted_count": 0,
        "repaired_count": 0,
        "invalid_orphan_count": 0,
        "detached_count": 0,
        "retained_stale_db_file_count": 0,
        "openai_active_agent_facing_file_count": 0,
        "pending_indexing_provider_file_count": 0,
    }


__all__ = [
    "ReconciliationContext",
    "build_reconciliation_context",
    "canonical_versions_by_source",
    "initial_reconciliation_summary",
    "load_provider_rows",
]
