import uuid
from datetime import datetime, timedelta

from celery.exceptions import MaxRetriesExceededError
from sqlalchemy.exc import SQLAlchemyError

from app.workers.celery_app import celery_app
from app.config import settings


@celery_app.task(name="app.workers.ingestion_tasks.ingest_source", bind=True, max_retries=5)
def ingest_source(self, source_id: str):
    """Main ingestion task: extracts text, chunks, embeds."""
    import asyncio

    try:
        found = asyncio.run(_ingest_source_async(source_id))
    except SQLAlchemyError as exc:
        try:
            raise self.retry(exc=exc, countdown=2)
        except MaxRetriesExceededError:
            asyncio.run(_mark_source_failed(source_id, f"Database error after retries: {exc}"))
            raise

    if not found:
        try:
            raise self.retry(countdown=1)
        except MaxRetriesExceededError:
            asyncio.run(_mark_source_failed(source_id, "Source not found after retries"))
            return


async def _ingest_source_async(source_id: str) -> bool:
    from sqlalchemy import select
    from app.workers.worker_db import worker_session_factory
    from app.models.source import Source, IngestionStatus

    async with worker_session_factory() as db:
        result = await db.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
        source = result.scalar_one_or_none()
        if source is None:
            return False

        source.ingestion_status = IngestionStatus.PROCESSING
        await db.commit()

        try:
            from app.ingestion.router import run_ingestion
            await run_ingestion(source, db)

            source.ingestion_status = IngestionStatus.COMPLETED
            source.fully_ingested = True
            if source.metadata_ and isinstance(source.metadata_, dict) and "error" in source.metadata_:
                source.metadata_ = {k: v for k, v in source.metadata_.items() if k != "error"}
            await db.commit()
        except Exception as e:
            source.ingestion_status = IngestionStatus.FAILED
            source.metadata_ = {**(source.metadata_ or {}), "error": str(e)}
            await db.commit()
            raise

    return True


async def _mark_source_failed(source_id: str, message: str) -> None:
    from sqlalchemy import select
    from app.workers.worker_db import worker_session_factory
    from app.models.source import Source, IngestionStatus

    async with worker_session_factory() as db:
        result = await db.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
        source = result.scalar_one_or_none()
        if source is None:
            return

        source.ingestion_status = IngestionStatus.FAILED
        source.metadata_ = {**(source.metadata_ or {}), "error": message}
        await db.commit()


@celery_app.task(name="app.workers.ingestion_tasks.deep_ingest_url")
def deep_ingest_url(source_id: str, generation_run_id: str):
    """Deep web ingestion: full download → parse → chunk → embed."""
    import asyncio

    asyncio.run(_deep_ingest_async(source_id, generation_run_id))


async def _deep_ingest_async(source_id: str, generation_run_id: str):
    from sqlalchemy import select
    from app.workers.worker_db import worker_session_factory
    from app.models.source import Source, IngestionStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import deep_ingestion_completed

    async with worker_session_factory() as db:
        # Atomic claim: SELECT FOR UPDATE SKIP LOCKED prevents two workers
        # from processing the same source concurrently.
        result = await db.execute(
            select(Source)
            .where(Source.id == uuid.UUID(source_id))
            .with_for_update(skip_locked=True)
        )
        source = result.scalar_one_or_none()
        if source is None:
            return  # locked by another worker or doesn't exist

        if source.ingestion_status in (IngestionStatus.PROCESSING, IngestionStatus.COMPLETED):
            return

        source.ingestion_status = IngestionStatus.PROCESSING
        await db.commit()

        try:
            from app.ingestion.router import run_ingestion
            await run_ingestion(source, db)

            source.ingestion_status = IngestionStatus.COMPLETED
            source.is_temporary = False
            source.fully_ingested = True
            if source.metadata_ and isinstance(source.metadata_, dict) and "error" in source.metadata_:
                source.metadata_ = {k: v for k, v in source.metadata_.items() if k != "error"}
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
    from app.workers.worker_db import worker_session_factory
    from app.models.source import Source, IngestionStatus
    from app.models.generation_run import GenerationRun, GenerationStatus

    cutoff = datetime.utcnow() - timedelta(days=settings.temp_source_ttl_days)
    stale_cutoff = datetime.utcnow() - timedelta(hours=1)

    async with worker_session_factory() as db:
        # 1. Reset stale PROCESSING/ENQUEUED sources (stuck > 1 hour — likely crashed worker or lost task)
        stale_result = await db.execute(
            select(Source).where(
                Source.ingestion_status.in_([IngestionStatus.PROCESSING, IngestionStatus.ENQUEUED]),
                Source.is_temporary == True,
                Source.status_changed_at < stale_cutoff,
            )
        )
        for source in stale_result.scalars().all():
            source.ingestion_status = IngestionStatus.PENDING

        # 2. Delete temp sources whose generation run is done and past TTL
        result = await db.execute(
            select(Source)
            .outerjoin(GenerationRun, Source.generation_run_id == GenerationRun.id)
            .where(
                Source.is_temporary == True,
                Source.created_at < cutoff,
                (GenerationRun.id == None) |
                (GenerationRun.status.in_([GenerationStatus.COMPLETED, GenerationStatus.FAILED, GenerationStatus.CANCELLED]))
            )
        )
        expired = result.scalars().all()
        for source in expired:
            await db.delete(source)

        await db.commit()
