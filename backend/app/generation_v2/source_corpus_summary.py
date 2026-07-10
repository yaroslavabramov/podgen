from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus_contracts import CorpusProvider, ProviderUploadPhase
from app.generation_v2.source_corpus_helpers import (
    canonical_provider_version as _canonical_provider_version,
    provider_file_search_ready as _provider_file_search_ready,
    versions_by_source as _versions_by_source,
)
from app.generation_v2.source_corpus_provider import _provider_uploads_required
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, ProviderCorpusScope, SourceVersion, V2RecordStatus
from app.models.source import Source


async def project_corpus_index_summary(db: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    index_result = await db.execute(
        select(ProviderCorpusIndex).where(
            ProviderCorpusIndex.project_id == project_id,
            ProviderCorpusIndex.provider == CorpusProvider.OPENAI,
            ProviderCorpusIndex.scope == ProviderCorpusScope.PROJECT,
        )
    )
    index = index_result.scalar_one_or_none()
    version_result = await db.execute(select(SourceVersion).where(SourceVersion.project_id == project_id))
    versions = list(version_result.scalars())
    source_result = await db.execute(
        select(Source).where(Source.project_id == project_id).order_by(Source.created_at.asc())
    )
    sources = list(source_result.scalars())
    files: list[ProviderCorpusFile] = []
    if index:
        file_result = await db.execute(select(ProviderCorpusFile).where(ProviderCorpusFile.provider_index_id == index.id))
        files = list(file_result.scalars())
    active_ready_versions = [
        version for version in versions if version.is_active and version.status == V2RecordStatus.READY
    ]
    ready_provider_files = [file for file in files if file.status == V2RecordStatus.READY]
    provider_by_version = {file.source_version_id: file for file in ready_provider_files if _provider_file_search_ready(file)}
    versions_by_source = _versions_by_source(active_ready_versions)
    source_summaries: list[dict[str, Any]] = []
    canonical_file_ids: set[str] = set()
    for source in sources:
        source_versions = versions_by_source.get(source.id, [])
        canonical = _canonical_provider_version(source_versions)
        provider_file = provider_by_version.get(canonical.id) if canonical else None
        skipped_reason: str | None = None
        if not source_versions:
            skipped_reason = "no_ready_source_versions"
        elif canonical is None:
            skipped_reason = "no_canonical_source_version"
        elif _provider_uploads_required() and (not index or not index.provider_vector_store_id or index.status != V2RecordStatus.READY):
            skipped_reason = "provider_index_not_ready"
        elif _provider_uploads_required() and provider_file is None:
            skipped_reason = "canonical_provider_file_not_ready"
        if provider_file and provider_file.provider_file_id:
            canonical_file_ids.add(provider_file.provider_file_id)
        source_summaries.append(
            {
                "source_id": str(source.id),
                "source_type": str(source.source_type),
                "canonical_version_id": str(canonical.id) if canonical else None,
                "canonical_kind": str(canonical.kind) if canonical else None,
                "provider_file_id": provider_file.provider_file_id if provider_file else None,
                "provider_status": provider_file.status if provider_file else None,
                "upload_phase": (provider_file.attributes or {}).get("upload_phase") if provider_file else None,
                "skipped_reason": skipped_reason,
            }
        )
    agent_facing_files = [
        file
        for file in ready_provider_files
        if (file.attributes or {}).get("is_active") is True and (file.attributes or {}).get("agent_facing") is True
    ]
    pending_indexing_files = [
        file
        for file in files
        if file.status == V2RecordStatus.PENDING and (file.attributes or {}).get("upload_phase") == ProviderUploadPhase.INDEXING
    ]
    failed_provider_files = [file for file in files if file.status == V2RecordStatus.FAILED]
    last_reconciliation = (index.config or {}).get("last_reconciliation", {}) if index else {}
    return {
        "project_id": str(project_id),
        "provider_index": {
            "id": str(index.id) if index else None,
            "provider": index.provider if index else CorpusProvider.OPENAI,
            "provider_vector_store_id": index.provider_vector_store_id if index else None,
            "status": index.status if index else "missing",
            "error_message": index.error_message if index else None,
        },
        "source_version_count": len(versions),
        "active_source_version_count": len([version for version in versions if version.is_active]),
        "provider_file_count": len(files),
        "ready_provider_file_count": len(ready_provider_files),
        "canonical_provider_file_count": len(canonical_file_ids),
        "active_agent_facing_provider_file_count": len(agent_facing_files),
        "noncanonical_provider_file_count": len(
            [file for file in agent_facing_files if file.provider_file_id not in canonical_file_ids]
        ),
        "db_canonical_provider_file_count": len(canonical_file_ids),
        "openai_active_agent_facing_file_count": int(
            last_reconciliation.get("openai_active_agent_facing_file_count", len(agent_facing_files)) or 0
        ),
        "pending_indexing_provider_file_count": len(pending_indexing_files),
        "failed_provider_file_count": len(failed_provider_files),
        "adoptable_orphan_count": int(last_reconciliation.get("adopted_count", 0) or 0),
        "invalid_orphan_count": int(last_reconciliation.get("invalid_orphan_count", 0) or 0),
        "last_reconciliation_at": last_reconciliation.get("last_reconciliation_at"),
        "sources": source_summaries,
    }
