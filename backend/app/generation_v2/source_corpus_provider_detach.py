from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.source_corpus_contracts import ProviderUploadPhase
from app.generation_v2.source_corpus_helpers import version_uploadable as _version_uploadable
from app.generation_v2.source_corpus_helpers import provider_external_attributes as _provider_external_attributes
from app.generation_v2.source_corpus_provider_attribute_sync import sync_provider_file_attributes
from app.generation_v2.source_corpus_vector_ops import (
    detach_openai_vector_store_file as _vector_detach_openai_vector_store_file,
    list_vector_store_files as _vector_list_vector_store_files,
)
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, SourceVersion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceCorpusProviderDetachDeps:
    detach_provider_file_from_active_index: Any
    detach_openai_vector_store_file: Any


def default_source_corpus_provider_detach_deps() -> SourceCorpusProviderDetachDeps:
    return SourceCorpusProviderDetachDeps(
        detach_provider_file_from_active_index=detach_provider_file_from_active_index,
        detach_openai_vector_store_file=detach_openai_vector_store_file,
    )


def _get_openai_client() -> Any:
    from app.services.llm import get_openai_client

    return get_openai_client()


async def mark_provider_files_inactive(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None = None,
    source_version_id: uuid.UUID | None = None,
    cleanup_status: str = "retained_for_historical_evidence",
    deps: SourceCorpusProviderDetachDeps | None = None,
) -> None:
    deps = deps or default_source_corpus_provider_detach_deps()
    provider_files = await stage_provider_files_inactive(
        db,
        source_id=source_id,
        source_version_id=source_version_id,
        cleanup_reason=cleanup_status,
    )
    for provider_file in provider_files:
        await _commit_if_supported(db)
        await deps.detach_provider_file_from_active_index(db, provider_file)
        await db.flush()
        await _commit_if_supported(db)
    await db.flush()


async def stage_provider_files_inactive(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None = None,
    source_version_id: uuid.UUID | None = None,
    cleanup_reason: str = "retained_for_historical_evidence",
) -> list[ProviderCorpusFile]:
    """Persist a retryable local tombstone before any external detach call."""
    if source_id is None and source_version_id is None:
        return []
    conditions = []
    if source_id is not None:
        conditions.append(ProviderCorpusFile.source_id == source_id)
    if source_version_id is not None:
        conditions.append(ProviderCorpusFile.source_version_id == source_version_id)
    result = await db.execute(select(ProviderCorpusFile).where(*conditions))
    provider_files = list(result.scalars())
    for provider_file in provider_files:
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "agent_facing": False,
            "canonical_retrieval_surface": False,
            "retrieval_surface": "fallback",
            "active_index_status": "detach_retry_pending",
            "cleanup_reason": cleanup_reason,
        }
        provider_file.cleanup_status = "provider_detach_retry_pending"
    await db.flush()
    return provider_files


async def detach_provider_file_from_active_index(db: AsyncSession, provider_file: ProviderCorpusFile) -> bool:
    provider_file.attributes = {
        **(provider_file.attributes or {}),
        "is_active": False,
        "agent_facing": False,
        "canonical_retrieval_surface": False,
        "retrieval_surface": "fallback",
        "upload_phase": ProviderUploadPhase.DETACHED,
    }
    await db.flush()
    await _commit_if_supported(db)
    if not provider_file.provider_file_id:
        provider_file.cleanup_status = "removed_from_active_index_retained"
        provider_file.attributes = {**provider_file.attributes, "active_index_status": "removed"}
        await db.flush()
        await _commit_if_supported(db)
        return True
    if not settings.openai_api_key:
        provider_file.cleanup_status = "provider_detach_retry_pending"
        provider_file.attributes = {**provider_file.attributes, "active_index_status": "detach_retry_pending"}
        await db.flush()
        await _commit_if_supported(db)
        return False
    provider_index = await db.get(ProviderCorpusIndex, provider_file.provider_index_id)
    if not provider_index or not provider_index.provider_vector_store_id:
        provider_file.cleanup_status = "provider_detach_retry_pending"
        provider_file.attributes = {**provider_file.attributes, "active_index_status": "detach_retry_pending"}
        await db.flush()
        await _commit_if_supported(db)
        return False
    attributes_synced = await sync_provider_file_attributes(
        provider_index,
        provider_file,
        _provider_external_attributes(provider_file.attributes),
        force=True,
    )
    try:
        await detach_openai_vector_store_file(provider_index, provider_file.provider_file_id)
        provider_file.cleanup_status = "removed_from_active_index_retained"
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "active_index_status": "removed",
        }
        detached = True
    except Exception as exc:
        logger.info("Could not detach provider corpus file %s from active index: %s", provider_file.id, exc)
        provider_file.cleanup_status = "provider_detach_retry_pending"
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "active_index_status": (
                "inactive_detach_retry_pending" if attributes_synced else "detach_retry_pending"
            ),
        }
        detached = False
    await db.flush()
    await _commit_if_supported(db)
    return detached


async def retry_pending_provider_detaches(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
) -> int:
    result = await db.execute(
        select(ProviderCorpusFile).where(ProviderCorpusFile.provider_index_id == provider_index.id)
    )
    retried = 0
    for provider_file in result.scalars():
        active_index_status = str((provider_file.attributes or {}).get("active_index_status") or "")
        if provider_file.cleanup_status != "provider_detach_retry_pending" and not active_index_status.endswith(
            "detach_retry_pending"
        ):
            continue
        retried += 1
        await detach_provider_file_from_active_index(db, provider_file)
    return retried


async def detach_excluded_provider_files(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex,
    source_versions: list[SourceVersion],
    agent_facing_version_ids: set[uuid.UUID],
    *,
    detach_source_ids: set[uuid.UUID] | None = None,
    deps: SourceCorpusProviderDetachDeps | None = None,
) -> None:
    deps = deps or default_source_corpus_provider_detach_deps()
    if detach_source_ids is not None and not detach_source_ids:
        return
    candidate_ids = [
        version.id
        for version in source_versions
        if _version_uploadable(version) and (detach_source_ids is None or version.source_id in detach_source_ids)
    ]
    if not candidate_ids:
        return
    result = await db.execute(
        select(ProviderCorpusFile).where(
            ProviderCorpusFile.provider_index_id == provider_index.id,
            ProviderCorpusFile.source_version_id.in_(candidate_ids),
        )
    )
    for provider_file in result.scalars():
        if provider_file.source_version_id in agent_facing_version_ids:
            continue
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "agent_facing": False,
            "canonical_retrieval_surface": False,
            "retrieval_surface": "fallback",
            "excluded_reason": "noncanonical_retrieval_surface",
        }
        provider_file.cleanup_status = "excluded_from_agent_facing_index"
        await db.flush()
        await _commit_if_supported(db)
        await deps.detach_provider_file_from_active_index(db, provider_file)


async def detach_openai_vector_store_file(provider_index: ProviderCorpusIndex, provider_file_id: str) -> None:
    await _vector_detach_openai_vector_store_file(_get_openai_client(), provider_index, provider_file_id)


async def list_vector_store_files(provider_index: ProviderCorpusIndex) -> list[Any]:
    return await _vector_list_vector_store_files(_get_openai_client(), provider_index)


async def _commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()


__all__ = [
    "detach_excluded_provider_files",
    "detach_openai_vector_store_file",
    "detach_provider_file_from_active_index",
    "list_vector_store_files",
    "mark_provider_files_inactive",
    "retry_pending_provider_detaches",
    "stage_provider_files_inactive",
]
