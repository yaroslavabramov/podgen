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
    if source_id is None and source_version_id is None:
        return
    conditions = []
    if source_id is not None:
        conditions.append(ProviderCorpusFile.source_id == source_id)
    if source_version_id is not None:
        conditions.append(ProviderCorpusFile.source_version_id == source_version_id)
    result = await db.execute(select(ProviderCorpusFile).where(*conditions))
    for provider_file in result.scalars():
        provider_file.attributes = {**(provider_file.attributes or {}), "is_active": False}
        provider_file.cleanup_status = cleanup_status
        await db.flush()
        await _commit_if_supported(db)
        await deps.detach_provider_file_from_active_index(db, provider_file)
        await db.flush()
        await _commit_if_supported(db)
    await db.flush()


async def detach_provider_file_from_active_index(db: AsyncSession, provider_file: ProviderCorpusFile) -> None:
    if not settings.openai_api_key or not provider_file.provider_file_id:
        return
    provider_index = await db.get(ProviderCorpusIndex, provider_file.provider_index_id)
    if not provider_index or not provider_index.provider_vector_store_id:
        return
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
    try:
        await detach_openai_vector_store_file(provider_index, provider_file.provider_file_id)
        provider_file.cleanup_status = "removed_from_active_index_retained"
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "active_index_status": "removed",
        }
    except Exception as exc:
        logger.info("Could not detach provider corpus file %s from active index: %s", provider_file.id, exc)
        provider_file.attributes = {
            **(provider_file.attributes or {}),
            "is_active": False,
            "active_index_status": "detach_failed",
        }
    await db.flush()
    await _commit_if_supported(db)


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
]
