import uuid
from datetime import datetime, timedelta

from app.workers.celery_app import celery_app
from app.config import settings


@celery_app.task(name="app.workers.ingestion_tasks.ingest_source", bind=True, max_retries=3)
def ingest_source(self, source_id: str):
    """Main ingestion task: extracts text, chunks, embeds."""
    import asyncio
    asyncio.run(_ingest_source_async(source_id))


async def _ingest_source_async(source_id: str):
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.source import Source, IngestionStatus

    async with async_session_factory() as db:
        result = await db.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
        source = result.scalar_one_or_none()
        if source is None:
            return

        source.ingestion_status = IngestionStatus.PROCESSING
        await db.commit()

        try:
            from app.ingestion.router import run_ingestion
            await run_ingestion(source, db)

            source.ingestion_status = IngestionStatus.COMPLETED
            source.fully_ingested = True
            await db.commit()
        except Exception as e:
            source.ingestion_status = IngestionStatus.FAILED
            source.metadata_ = {**(source.metadata_ or {}), "error": str(e)}
            await db.commit()
            raise


@celery_app.task(name="app.workers.ingestion_tasks.deep_ingest_url")
def deep_ingest_url(source_id: str, generation_run_id: str):
    """Deep web ingestion: full download → parse → chunk → embed."""
    import asyncio
    asyncio.run(_deep_ingest_async(source_id, generation_run_id))


async def _deep_ingest_async(source_id: str, generation_run_id: str):
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.source import Source, IngestionStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import deep_ingestion_completed

    async with async_session_factory() as db:
        result = await db.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
        source = result.scalar_one_or_none()
        if source is None:
            return

        # Guard: skip if already processing or completed (idempotency)
        if source.ingestion_status in (IngestionStatus.PROCESSING, IngestionStatus.COMPLETED):
            return

        # Mark PROCESSING immediately to prevent duplicate tasks
        source.ingestion_status = IngestionStatus.PROCESSING
        await db.commit()

        try:
            from app.ingestion.router import run_ingestion
            await run_ingestion(source, db)

            source.ingestion_status = IngestionStatus.COMPLETED
            source.is_temporary = False
            source.fully_ingested = True
            await db.commit()

            publish_event(generation_run_id, deep_ingestion_completed(source.title or "Unknown"))
        except Exception as e:
            source.ingestion_status = IngestionStatus.FAILED
            await db.commit()


@celery_app.task(name="app.workers.ingestion_tasks.cleanup_expired_temp_sources")
def cleanup_expired_temp_sources():
    """Clean up temporary sources past their TTL."""
    import asyncio
    asyncio.run(_cleanup_async())


async def _cleanup_async():
    from sqlalchemy import select, delete
    from app.database import async_session_factory
    from app.models.source import Source
    from app.models.generation_run import GenerationRun, GenerationStatus

    cutoff = datetime.utcnow() - timedelta(days=settings.temp_source_ttl_days)

    async with async_session_factory() as db:
        # Find temp sources whose generation run is completed/failed and past TTL
        result = await db.execute(
            select(Source)
            .outerjoin(GenerationRun, Source.generation_run_id == GenerationRun.id)
            .where(
                Source.is_temporary == True,
                Source.created_at < cutoff,
                # Either run is done or source has no run link (orphan)
                (GenerationRun.id == None) |
                (GenerationRun.status.in_([GenerationStatus.COMPLETED, GenerationStatus.FAILED, GenerationStatus.CANCELLED]))
            )
        )
        expired = result.scalars().all()
        for source in expired:
            await db.delete(source)
        await db.commit()
