from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.source_corpus_contracts import (
    CorpusProvider,
    OpenAIProviderFileStatus,
    PROVIDER_FAILED_TERMINAL_STATUSES,
    ProviderUploadPhase,
)
from app.generation_v2.source_corpus_helpers import (
    parse_uuid as _parse_uuid,
    provider_attributes as _provider_attributes,
    provider_db_attributes as _provider_db_attributes,
    provider_last_error as _provider_last_error,
    validate_reconciliation_candidate as _validate_reconciliation_candidate,
)
from app.generation_v2.source_corpus_provider_detach import detach_openai_vector_store_file, list_vector_store_files
from app.generation_v2.source_corpus_reconciliation_context import build_reconciliation_context
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, SourceVersion, V2RecordStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceCorpusReconciliationDeps:
    list_vector_store_files: Any
    detach_openai_vector_store_file: Any
    commit_if_supported: Any


def default_source_corpus_reconciliation_deps() -> SourceCorpusReconciliationDeps:
    return SourceCorpusReconciliationDeps(
        list_vector_store_files=list_vector_store_files,
        detach_openai_vector_store_file=detach_openai_vector_store_file,
        commit_if_supported=commit_if_supported,
    )


async def reconcile_provider_corpus_files(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
    *,
    source_ids: set[uuid.UUID] | None = None,
    deps: SourceCorpusReconciliationDeps | None = None,
) -> dict[str, Any]:
    deps = deps or default_source_corpus_reconciliation_deps()
    if not provider_index.provider_vector_store_id or not settings.openai_api_key:
        return {}
    context = await build_reconciliation_context(db, provider_index, source_versions, source_ids=source_ids)
    summary = context.summary
    vector_files = await deps.list_vector_store_files(provider_index)
    openai_file_ids = {str(getattr(vector_file, "id", "") or "") for vector_file in vector_files}
    for vector_file in vector_files:
        provider_file_id = str(getattr(vector_file, "id", "") or "")
        attributes = dict(getattr(vector_file, "attributes", None) or {})
        status = str(getattr(vector_file, "status", "") or "")
        validation = _validate_reconciliation_candidate(
            provider_index,
            vector_file,
            attributes,
            context.versions_by_id,
            context.canonical_by_source,
            context.scope_source_ids,
        )
        if validation.get("ignored"):
            continue
        version_id = _parse_uuid(attributes.get("source_version_id"))
        row = context.rows_by_provider_file_id.get(provider_file_id) or (
            context.rows_by_version_id.get(version_id) if version_id is not None else None
        )
        if not validation["valid"]:
            if row is not None and validation.get("reason") in {
                "source_version_not_active_ready",
                "noncanonical_source_version",
            }:
                summary["retained_stale_db_file_count"] += 1
                continue
            summary["invalid_orphan_count"] += 1
            try:
                await deps.detach_openai_vector_store_file(provider_index, provider_file_id)
                summary["detached_count"] += 1
            except Exception as exc:
                logger.info("Could not detach invalid OpenAI provider file %s: %s", provider_file_id, exc)
            continue
        version: SourceVersion = validation["version"]
        provider_attributes = _provider_attributes(version)
        row = row or context.rows_by_version_id.get(version.id)
        if (
            row
            and row.provider_file_id
            and row.provider_file_id != provider_file_id
            and row.status == V2RecordStatus.READY
            and row.provider_file_id in openai_file_ids
        ):
            summary["invalid_orphan_count"] += 1
            try:
                await deps.detach_openai_vector_store_file(provider_index, provider_file_id)
                summary["detached_count"] += 1
            except Exception as exc:
                logger.info("Could not detach duplicate OpenAI provider file %s: %s", provider_file_id, exc)
            continue
        is_new_row = row is None
        if row is None:
            row = ProviderCorpusFile(
                provider_index_id=provider_index.id,
                source_version_id=version.id,
                source_id=version.source_id,
                provider=CorpusProvider.OPENAI,
                status=V2RecordStatus.PENDING,
            )
            db.add(row)
        row.provider_file_id = provider_file_id
        row.provider_vector_store_file_id = provider_file_id
        row.attributes = _provider_db_attributes(
            row.attributes,
            ProviderUploadPhase.ADOPTED if status == OpenAIProviderFileStatus.COMPLETED else ProviderUploadPhase.INDEXING,
            provider_attributes,
            last_provider_status=status or "unknown",
            last_reconciled_at=datetime.utcnow().isoformat(),
            adoption_source="vector_store_reconciliation",
        )
        if status == OpenAIProviderFileStatus.COMPLETED:
            row.status = V2RecordStatus.READY
            row.error_message = None
            row.cleanup_status = None
            version.provider = CorpusProvider.OPENAI
            version.provider_file_id = provider_file_id
            version.provider_vector_store_file_id = provider_file_id
            version.upload_status = V2RecordStatus.READY
            summary["openai_active_agent_facing_file_count"] += 1
            summary["adopted_count" if is_new_row else "repaired_count"] += 1
        elif status in PROVIDER_FAILED_TERMINAL_STATUSES:
            row.status = V2RecordStatus.FAILED
            row.error_message = _provider_last_error(vector_file) or f"OpenAI vector store file status: {status}"
            row.attributes = _provider_db_attributes(
                row.attributes,
                ProviderUploadPhase.FAILED,
                provider_attributes,
                last_provider_status=status,
                last_openai_error=row.error_message,
            )
            version.upload_status = V2RecordStatus.FAILED
        else:
            row.status = V2RecordStatus.PENDING
            row.error_message = None
            version.provider = CorpusProvider.OPENAI
            version.provider_file_id = provider_file_id
            version.provider_vector_store_file_id = provider_file_id
            version.upload_status = V2RecordStatus.PENDING
            summary["pending_indexing_provider_file_count"] += 1
            summary["adopted_count" if is_new_row else "repaired_count"] += 1
        await db.flush()
    provider_index.config = {
        **(provider_index.config or {}),
        "last_reconciliation": {
            **summary,
            "last_reconciliation_at": datetime.utcnow().isoformat(),
        },
    }
    await db.flush()
    await deps.commit_if_supported(db)
    return summary


async def commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()
