from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.source_corpus_contracts import (
    CorpusProvider,
    OpenAIProviderFileStatus,
    ProviderUploadPhase,
)
from app.generation_v2.source_corpus_helpers import (
    provider_attributes as _provider_attributes,
    provider_db_attributes as _provider_db_attributes,
)
from app.generation_v2.source_corpus_provider_attribute_sync import sync_provider_file_attributes
from app.generation_v2.source_corpus_provider_polling import poll_provider_file_until_terminal
from app.generation_v2.source_corpus_upload_payload import version_upload_payload
from app.generation_v2.source_corpus_vector_ops import ensure_vector_store_file_attached
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, SourceVersion, V2RecordStatus
from app.services.llm import get_openai_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceCorpusProviderUploadDeps:
    get_openai_client: Any
    version_upload_payload: Any
    sync_provider_file_attributes: Any
    ensure_vector_store_file_attached: Any
    poll_provider_file_until_terminal: Any


def default_source_corpus_provider_upload_deps() -> SourceCorpusProviderUploadDeps:
    return SourceCorpusProviderUploadDeps(
        get_openai_client=get_openai_client,
        version_upload_payload=version_upload_payload,
        sync_provider_file_attributes=sync_provider_file_attributes,
        ensure_vector_store_file_attached=ensure_vector_store_file_attached,
        poll_provider_file_until_terminal=poll_provider_file_until_terminal,
    )


async def ensure_provider_file(
    db: AsyncSession,
    run: GenerationRun | None,
    provider_index: ProviderCorpusIndex,
    version: SourceVersion,
    *,
    deps: SourceCorpusProviderUploadDeps | None = None,
) -> ProviderCorpusFile:
    deps = deps or default_source_corpus_provider_upload_deps()
    provider_attributes = _provider_attributes(version)
    existing_result = await db.execute(
        select(ProviderCorpusFile).where(
            ProviderCorpusFile.provider_index_id == provider_index.id,
            ProviderCorpusFile.source_version_id == version.id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing and existing.status == V2RecordStatus.READY and existing.provider_file_id:
        if await deps.sync_provider_file_attributes(provider_index, existing, provider_attributes):
            existing.attributes = _provider_db_attributes(existing.attributes, ProviderUploadPhase.READY, provider_attributes)
            existing.cleanup_status = None
            version.provider = CorpusProvider.OPENAI
            version.provider_file_id = existing.provider_file_id
            version.provider_vector_store_file_id = existing.provider_vector_store_file_id
            version.upload_status = V2RecordStatus.READY
            await db.flush()
            await _commit_if_supported(db)
            return existing
        existing.status = V2RecordStatus.PENDING
        existing.error_message = "Provider file attributes were stale and could not be updated; re-uploading"
    if existing is None:
        existing = ProviderCorpusFile(
            provider_index_id=provider_index.id,
            source_version_id=version.id,
            source_id=version.source_id,
            provider=CorpusProvider.OPENAI,
            attributes=_provider_db_attributes({}, ProviderUploadPhase.DB_PREPARED, provider_attributes),
            status=V2RecordStatus.PENDING,
        )
        db.add(existing)
        await db.flush()
        await _commit_if_supported(db)
    try:
        client = deps.get_openai_client()
        filename: str | None = None
        if not existing.provider_file_id:
            filename, content = deps.version_upload_payload(version)
            if not content:
                raise RuntimeError("Source version content is empty")
            file_obj = await asyncio.wait_for(
                client.files.create(file=(filename, io.BytesIO(content)), purpose="assistants"),
                timeout=max(30, int(settings.generation_v2_provider_upload_timeout_seconds)),
            )
            existing.provider_file_id = file_obj.id
            existing.status = V2RecordStatus.PENDING
            existing.error_message = None
            existing.attributes = _provider_db_attributes(
                existing.attributes,
                ProviderUploadPhase.FILE_UPLOADED,
                provider_attributes,
                last_provider_status=OpenAIProviderFileStatus.FILE_UPLOADED,
            )
            version.provider = CorpusProvider.OPENAI
            version.provider_file_id = file_obj.id
            version.upload_status = V2RecordStatus.PENDING
            await db.flush()
            await _commit_if_supported(db)
        vector_file = await deps.ensure_vector_store_file_attached(client, provider_index, existing, provider_attributes)
        if vector_file is not None:
            existing.provider_vector_store_file_id = getattr(vector_file, "id", None) or existing.provider_file_id
            existing.attributes = _provider_db_attributes(
                existing.attributes,
                ProviderUploadPhase.ATTACHED,
                provider_attributes,
                last_provider_status=str(getattr(vector_file, "status", "") or OpenAIProviderFileStatus.ATTACHED),
            )
            version.provider_vector_store_file_id = existing.provider_vector_store_file_id
            await db.flush()
            await _commit_if_supported(db)
        await deps.poll_provider_file_until_terminal(db, client, provider_index, existing, version, provider_attributes)
        if run is not None:
            await save_tool_trace(
                db,
                generation_run_id=run.id,
                phase=V2Phase.CORPUS_PREPARE,
                agent_name=V2AgentName.SOURCE_CORPUS,
                tool_name=V2ToolName.OPENAI_FILES_CREATE_VECTOR_STORE_FILE_RESUME,
                input_json={"source_version_id": str(version.id), "filename": filename, "attributes": provider_attributes},
                output_json={
                    "provider_file_id": existing.provider_file_id,
                    "provider_vector_store_file_id": existing.provider_vector_store_file_id,
                    "status": existing.status,
                    "upload_phase": (existing.attributes or {}).get("upload_phase"),
                },
                provider_call_id=existing.provider_file_id,
            )
    except Exception as exc:
        logger.info("Provider corpus file sync failed for %s: %s", version.id, exc, exc_info=True)
        existing.status = V2RecordStatus.FAILED
        existing.error_message = str(exc)
        existing.attributes = _provider_db_attributes(
            existing.attributes,
            ProviderUploadPhase.FAILED,
            provider_attributes,
            last_openai_error=str(exc),
        )
        version.upload_status = V2RecordStatus.FAILED
    await db.flush()
    await _commit_if_supported(db)
    return existing


async def _commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()


__all__ = ["ensure_provider_file"]
