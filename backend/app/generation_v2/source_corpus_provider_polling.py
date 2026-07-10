from __future__ import annotations

import asyncio
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
    provider_db_attributes as _provider_db_attributes,
    provider_last_error as _provider_last_error,
)
from app.generation_v2.source_corpus_vector_ops import retrieve_vector_store_file
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, SourceVersion, V2RecordStatus


async def poll_provider_file_until_terminal(
    db: AsyncSession,
    client: Any,
    provider_index: ProviderCorpusIndex,
    provider_file: ProviderCorpusFile,
    version: SourceVersion,
    attributes: dict[str, str | bool | float],
) -> None:
    deadline = asyncio.get_running_loop().time() + max(30, int(settings.generation_v2_provider_upload_timeout_seconds))
    while True:
        vector_file = await retrieve_vector_store_file(
            client,
            provider_index,
            provider_file.provider_file_id or "",
        )
        if vector_file is None:
            await _mark_indexing_unavailable(db, provider_file, version, attributes)
            return
        last_status = str(getattr(vector_file, "status", "") or "")
        provider_file.provider_vector_store_file_id = getattr(vector_file, "id", None) or provider_file.provider_file_id
        if last_status == OpenAIProviderFileStatus.COMPLETED:
            await _mark_ready(db, provider_file, version, attributes, last_status=last_status)
            return
        if last_status in PROVIDER_FAILED_TERMINAL_STATUSES:
            await _mark_failed(db, provider_file, version, attributes, vector_file=vector_file, last_status=last_status)
            return
        if asyncio.get_running_loop().time() >= deadline:
            await _mark_timeout(db, provider_file, version, attributes, last_status=last_status)
            return
        await _mark_indexing(db, provider_file, attributes, last_status=last_status)
        await asyncio.sleep(1)


async def _mark_indexing_unavailable(
    db: AsyncSession,
    provider_file: ProviderCorpusFile,
    version: SourceVersion,
    attributes: dict[str, str | bool | float],
) -> None:
    provider_file.status = V2RecordStatus.PENDING
    provider_file.error_message = None
    provider_file.attributes = _provider_db_attributes(
        provider_file.attributes,
        ProviderUploadPhase.INDEXING,
        attributes,
        last_provider_status=OpenAIProviderFileStatus.RETRIEVE_UNAVAILABLE,
        last_openai_error="provider_vector_store_file_retrieve_unavailable",
    )
    version.upload_status = V2RecordStatus.PENDING
    await _flush_commit(db)


async def _mark_ready(
    db: AsyncSession,
    provider_file: ProviderCorpusFile,
    version: SourceVersion,
    attributes: dict[str, str | bool | float],
    *,
    last_status: str,
) -> None:
    provider_file.status = V2RecordStatus.READY
    provider_file.error_message = None
    provider_file.cleanup_status = None
    provider_file.attributes = _provider_db_attributes(
        provider_file.attributes,
        ProviderUploadPhase.READY,
        attributes,
        last_provider_status=last_status,
    )
    version.provider = CorpusProvider.OPENAI
    version.provider_file_id = provider_file.provider_file_id
    version.provider_vector_store_file_id = provider_file.provider_vector_store_file_id
    version.upload_status = V2RecordStatus.READY
    await _flush_commit(db)


async def _mark_failed(
    db: AsyncSession,
    provider_file: ProviderCorpusFile,
    version: SourceVersion,
    attributes: dict[str, str | bool | float],
    *,
    vector_file: Any,
    last_status: str,
) -> None:
    last_error = _provider_last_error(vector_file) or f"OpenAI vector store file status: {last_status}"
    provider_file.status = V2RecordStatus.FAILED
    provider_file.error_message = last_error
    provider_file.attributes = _provider_db_attributes(
        provider_file.attributes,
        ProviderUploadPhase.FAILED,
        attributes,
        last_provider_status=last_status,
        last_openai_error=last_error,
    )
    version.upload_status = V2RecordStatus.FAILED
    await _flush_commit(db)


async def _mark_timeout(
    db: AsyncSession,
    provider_file: ProviderCorpusFile,
    version: SourceVersion,
    attributes: dict[str, str | bool | float],
    *,
    last_status: str,
) -> None:
    provider_file.status = V2RecordStatus.PENDING
    provider_file.error_message = None
    provider_file.attributes = _provider_db_attributes(
        provider_file.attributes,
        ProviderUploadPhase.INDEXING,
        attributes,
        last_provider_status=last_status or OpenAIProviderFileStatus.IN_PROGRESS,
        last_openai_error="provider_indexing_timeout",
    )
    version.provider = CorpusProvider.OPENAI
    version.provider_file_id = provider_file.provider_file_id
    version.provider_vector_store_file_id = provider_file.provider_vector_store_file_id
    version.upload_status = V2RecordStatus.PENDING
    await _flush_commit(db)


async def _mark_indexing(
    db: AsyncSession,
    provider_file: ProviderCorpusFile,
    attributes: dict[str, str | bool | float],
    *,
    last_status: str,
) -> None:
    provider_file.status = V2RecordStatus.PENDING
    provider_file.attributes = _provider_db_attributes(
        provider_file.attributes,
        ProviderUploadPhase.INDEXING,
        attributes,
        last_provider_status=last_status or OpenAIProviderFileStatus.IN_PROGRESS,
    )
    await _flush_commit(db)


async def _flush_commit(db: AsyncSession) -> None:
    await db.flush()
    await _commit_if_supported(db)


async def _commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()


__all__ = ["poll_provider_file_until_terminal"]
