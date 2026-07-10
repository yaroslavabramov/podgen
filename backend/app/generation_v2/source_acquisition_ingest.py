from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_corpus import sync_source_to_project_corpus_now
from app.ingestion.router import WebQualityIngestionError, _deactivate_native_ingest_for_web_failure, run_ingestion
from app.models.generation_v2 import SourceVersion
from app.models.source import IngestionStatus, Source


async def native_first_ingest_candidate(db: AsyncSession, source_id: uuid.UUID) -> None:
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError("Source not found")
    sync_summary = await sync_source_to_project_corpus_now(db, source)
    await db.commit()
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError("Source not found after V2 native sync")
    try:
        source.ingestion_status = IngestionStatus.PROCESSING
        await db.commit()
        await run_ingestion(source, db)
        source.ingestion_status = IngestionStatus.COMPLETED
        source.fully_ingested = True
        metadata = dict(source.metadata_ or {})
        metadata.pop("error", None)
        metadata["ingest_status"] = "completed"
        source.metadata_ = metadata
        await db.commit()
        await sync_source_to_project_corpus_now(db, source)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        source = await db.get(Source, source_id)
        if source is None:
            raise
        if isinstance(exc, WebQualityIngestionError) or getattr(exc, "invalidate_native_ingest", False):
            await _deactivate_native_ingest_for_web_failure(db, source, str(exc))
            metadata = dict(source.metadata_ or {})
            metadata.update(getattr(exc, "metadata_patch", lambda: {})())
            metadata["error"] = str(exc)
            metadata["deterministic_ingest_error"] = str(exc)
            metadata["ingest_status"] = "failed_invalid_web_source"
            source.metadata_ = metadata
            source.ingestion_status = IngestionStatus.FAILED
            source.fully_ingested = False
            await db.commit()
            raise
        if native_ready(source.metadata_, sync_summary):
            metadata = dict(source.metadata_ or {})
            metadata.pop("error", None)
            metadata["deterministic_ingest_error"] = str(exc)
            metadata["ingest_status"] = "completed_v2_native_parser_failed"
            source.metadata_ = metadata
            source.ingestion_status = IngestionStatus.COMPLETED
            source.fully_ingested = False
            await db.commit()
            return
        source.ingestion_status = IngestionStatus.FAILED
        source.fully_ingested = False
        source.metadata_ = {**(source.metadata_ or {}), "error": str(exc), "ingest_status": "failed"}
        await db.commit()
        raise


def native_ready(metadata: dict | None, sync_summary: dict[str, Any] | None = None) -> bool:
    native = (metadata or {}).get("v2_native_ingest") if isinstance(metadata, dict) else {}
    if not isinstance(native, dict):
        native = {}
    try:
        ready_provider_count = int(native.get("ready_provider_file_count") or (sync_summary or {}).get("ready_provider_file_count") or 0)
    except (TypeError, ValueError):
        ready_provider_count = 0
    return bool(ready_provider_count > 0 or native.get("openai_url_read_status") in {"readable", "partial"})


async def source_versions(db: AsyncSession, source_id: uuid.UUID) -> list[SourceVersion]:
    result = await db.execute(select(SourceVersion).where(SourceVersion.source_id == source_id).order_by(SourceVersion.created_at.asc()))
    return list(result.scalars())
