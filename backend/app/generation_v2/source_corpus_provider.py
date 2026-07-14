from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.source_corpus_provider_detach import (
    SourceCorpusProviderDetachDeps,
    default_source_corpus_provider_detach_deps,
    detach_excluded_provider_files,
    detach_openai_vector_store_file,
    detach_provider_file_from_active_index,
    list_vector_store_files,
    mark_provider_files_inactive as _mark_provider_files_inactive,
    retry_pending_provider_detaches,
)
from app.generation_v2.source_corpus_provider_attribute_sync import (
    sync_provider_file_attributes,
)
from app.generation_v2.source_corpus_provider_index import ensure_project_provider_index
from app.generation_v2.source_corpus_provider_polling import poll_provider_file_until_terminal
from app.generation_v2.source_corpus_provider_upload import (
    SourceCorpusProviderUploadDeps,
    default_source_corpus_provider_upload_deps,
    ensure_provider_file as _ensure_provider_file_impl,
)
from app.generation_v2.source_corpus_reconciliation import reconcile_provider_corpus_files as reconcile_provider_corpus_files
from app.generation_v2.source_corpus_helpers import (
    agent_facing_provider_versions as _agent_facing_provider_versions,
    provider_file_search_ready as _provider_file_search_ready,
    version_uploadable as _version_uploadable,
)
from app.generation_v2.source_corpus_vector_ops import (
    ensure_vector_store_file_attached as _vector_ensure_vector_store_file_attached,
    provider_vector_operation_timeout_seconds as _provider_vector_operation_timeout_seconds,
    retrieve_vector_store_file as _vector_retrieve_vector_store_file,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    ProviderCorpusFile,
    ProviderCorpusIndex,
    SourceVersion,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceCorpusProviderSyncDeps:
    reconcile_provider_corpus_files: Any
    ensure_provider_file: Any
    detach_excluded_provider_files: Any
    retry_pending_provider_detaches: Any


def default_source_corpus_provider_sync_deps() -> SourceCorpusProviderSyncDeps:
    return SourceCorpusProviderSyncDeps(
        reconcile_provider_corpus_files=reconcile_provider_corpus_files,
        ensure_provider_file=_ensure_provider_file,
        detach_excluded_provider_files=_detach_excluded_provider_files,
        retry_pending_provider_detaches=retry_pending_provider_detaches,
    )


async def sync_provider_corpus_files(
    db: AsyncSession,
    run: GenerationRun | None,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
    *,
    source_ids: set[uuid.UUID] | None = None,
    deps: SourceCorpusProviderSyncDeps | None = None,
) -> list[ProviderCorpusFile]:
    deps = deps or default_source_corpus_provider_sync_deps()
    if (
        not provider_index.provider_vector_store_id
        or not settings.openai_api_key
        or not settings.generation_v2_enable_provider_uploads
    ):
        return []
    await _commit_if_supported(db)
    await deps.retry_pending_provider_detaches(db, provider_index)
    active_source_versions = [version for version in source_versions if version.is_active]
    agent_facing_versions = _agent_facing_provider_versions(active_source_versions)
    agent_facing_version_ids = {version.id for version in agent_facing_versions}
    await deps.reconcile_provider_corpus_files(db, provider_index, source_versions, source_ids=source_ids)
    synced: list[ProviderCorpusFile] = []
    for version in agent_facing_versions:
        if not _version_uploadable(version):
            continue
        synced.append(await deps.ensure_provider_file(db, run, provider_index, version))
    ready_agent_source_ids = {
        provider_file.source_id
        for provider_file in synced
        if provider_file.source_version_id in agent_facing_version_ids and _provider_file_search_ready(provider_file)
    }
    await deps.detach_excluded_provider_files(
        db,
        provider_index,
        source_versions,
        agent_facing_version_ids,
        detach_source_ids=ready_agent_source_ids,
    )
    await db.flush()
    return synced

async def mark_provider_files_inactive(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None = None,
    source_version_id: uuid.UUID | None = None,
    cleanup_status: str = "retained_for_historical_evidence",
) -> None:
    await _mark_provider_files_inactive(
        db,
        source_id=source_id,
        source_version_id=source_version_id,
        cleanup_status=cleanup_status,
    )


async def _detach_provider_file_from_active_index(db: AsyncSession, provider_file: ProviderCorpusFile) -> None:
    await detach_provider_file_from_active_index(db, provider_file)


async def _ensure_provider_file(
    db: AsyncSession,
    run: GenerationRun | None,
    provider_index: ProviderCorpusIndex,
    version: SourceVersion,
    **kwargs: Any,
) -> ProviderCorpusFile:
    return await _ensure_provider_file_impl(db, run, provider_index, version, **kwargs)


async def _ensure_vector_store_file_attached(
    client: Any,
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    attributes: dict[str, str | bool | float],
) -> Any | None:
    return await _vector_ensure_vector_store_file_attached(client, provider_index, provider_file, attributes)


async def _poll_provider_file_until_terminal(
    db: AsyncSession,
    client: Any,
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    version: SourceVersion,
    attributes: dict[str, str | bool | float],
) -> None:
    await poll_provider_file_until_terminal(db, client, provider_index, provider_file, version, attributes)


async def _retrieve_vector_store_file(client: Any, provider_index: ProviderCorpusIndex, provider_file_id: str) -> Any | None:
    return await _vector_retrieve_vector_store_file(client, provider_index, provider_file_id)


async def _sync_provider_file_attributes(
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    attributes: dict[str, str | bool | float],
) -> bool:
    return await sync_provider_file_attributes(provider_index, provider_file, attributes)


def _provider_uploads_required() -> bool:
    return bool(settings.openai_api_key and settings.generation_v2_enable_provider_uploads)


async def _detach_excluded_provider_files(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
    agent_facing_version_ids: set[uuid.UUID],
    *,
    detach_source_ids: set[uuid.UUID] | None = None,
) -> None:
    await detach_excluded_provider_files(
        db,
        provider_index,
        source_versions,
        agent_facing_version_ids,
        detach_source_ids=detach_source_ids,
    )


async def _detach_openai_vector_store_file(provider_index: ProviderCorpusIndex, provider_file_id: str) -> None:
    await detach_openai_vector_store_file(provider_index, provider_file_id)


async def _list_vector_store_files(provider_index: ProviderCorpusIndex) -> list[Any]:
    return await list_vector_store_files(provider_index)


async def _commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()

__all__ = [
    "ensure_project_provider_index",
    "sync_provider_file_attributes",
    "_provider_vector_operation_timeout_seconds",
    "SourceCorpusProviderUploadDeps",
    "default_source_corpus_provider_upload_deps",
    "SourceCorpusProviderDetachDeps",
    "default_source_corpus_provider_detach_deps",
]
