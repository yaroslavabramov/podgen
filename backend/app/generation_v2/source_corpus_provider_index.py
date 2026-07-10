from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.source_corpus_contracts import CorpusProvider
from app.models.generation_v2 import ProviderCorpusIndex, ProviderCorpusScope, V2RecordStatus
from app.models.project import Project
from app.services.llm import get_openai_client


async def ensure_project_provider_index(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    run_id: uuid.UUID | None = None,
) -> ProviderCorpusIndex | None:
    # Serialize project-level provider index creation across concurrent ingest jobs.
    await db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
    result = await db.execute(
        select(ProviderCorpusIndex).where(
            ProviderCorpusIndex.project_id == project_id,
            ProviderCorpusIndex.provider == CorpusProvider.OPENAI,
            ProviderCorpusIndex.scope == ProviderCorpusScope.PROJECT,
        )
    )
    existing = result.scalar_one_or_none()
    if existing and existing.provider_vector_store_id and settings.openai_api_key and settings.generation_v2_enable_provider_uploads:
        if existing.status != V2RecordStatus.READY:
            existing.status = V2RecordStatus.READY
            existing.error_message = None
            existing.config = {
                **(existing.config or {}),
                "provider_uploads_enabled": True,
            }
            await db.flush()
        return existing
    if not settings.openai_api_key or not settings.generation_v2_enable_provider_uploads:
        error_message = (
            "OPENAI_API_KEY is not configured; provider index unavailable."
            if not settings.openai_api_key
            else "Provider corpus uploads are disabled; provider index unavailable."
        )
        if existing is None:
            existing = ProviderCorpusIndex(
                project_id=project_id,
                provider=CorpusProvider.OPENAI,
                scope=ProviderCorpusScope.PROJECT,
                status=V2RecordStatus.FAILED,
                config={"provider_uploads_enabled": bool(settings.generation_v2_enable_provider_uploads)},
                error_message=error_message,
            )
            db.add(existing)
        else:
            existing.status = V2RecordStatus.FAILED
            existing.error_message = error_message
            existing.config = {
                **(existing.config or {}),
                "provider_uploads_enabled": bool(settings.generation_v2_enable_provider_uploads),
            }
        await db.flush()
        return existing

    client = get_openai_client()
    vector_store = await client.vector_stores.create(
        name=f"podgen-v2-project-{project_id}",
        metadata={"project_id": str(project_id), "scope": "project"},
    )
    if existing is None:
        existing = ProviderCorpusIndex(
            project_id=project_id,
            provider=CorpusProvider.OPENAI,
            provider_vector_store_id=vector_store.id,
            scope=ProviderCorpusScope.PROJECT,
            status=V2RecordStatus.READY,
            config={"created_by_run_id": str(run_id) if run_id else None},
        )
        db.add(existing)
    else:
        existing.provider_vector_store_id = vector_store.id
        existing.status = V2RecordStatus.READY
        existing.error_message = None
        existing.config = {**(existing.config or {}), "created_by_run_id": str(run_id) if run_id else None}
    await db.flush()
    return existing
