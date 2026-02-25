import logging
import uuid
from datetime import datetime, timedelta

from celery.exceptions import MaxRetriesExceededError
from sqlalchemy.exc import SQLAlchemyError

from app.workers.celery_app import celery_app
from app.config import settings
from app.ingestion.web import DNSResolutionError
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope

logger = logging.getLogger(__name__)


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
    except DNSResolutionError as exc:
        try:
            raise self.retry(exc=exc, countdown=3)
        except MaxRetriesExceededError:
            asyncio.run(_mark_source_failed(source_id, f"DNS resolution error after retries: {exc}"))
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

        trace_ctx = {
            "component": "worker.ingestion",
            "source_id": str(source.id),
            "project_id": str(source.project_id),
            "generation_run_id": str(source.generation_run_id) if source.generation_run_id else "",
            "source_type": source.source_type,
        }
        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["worker", "ingestion"]):
                with trace_span(
                    "worker.ingest_source",
                    run_type="chain",
                    inputs={"source_id": source_id},
                    metadata={"source_type": source.source_type},
                    tags=["celery-task"],
                ) as task_run:
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

                        safe_set_run(
                            task_run,
                            outputs={
                                "status": "completed",
                                "fully_ingested": source.fully_ingested,
                            },
                        )
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

        trace_ctx = {
            "component": "worker.deep_ingestion",
            "source_id": str(source.id),
            "project_id": str(source.project_id),
            "generation_run_id": generation_run_id,
            "source_type": source.source_type,
        }
        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["worker", "deep-ingestion"]):
                with trace_span(
                    "worker.deep_ingest_url",
                    run_type="chain",
                    inputs={"source_id": source_id, "generation_run_id": generation_run_id},
                    metadata={"source_type": source.source_type},
                    tags=["celery-task"],
                ) as task_run:
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

                        # Upgrade claims referencing evidence from this source
                        try:
                            upgraded = await _upgrade_claims_for_source(source.id, db)
                            if upgraded:
                                logger.info(
                                    "Deep ingestion: upgraded %d claims for source %s",
                                    upgraded, source_id,
                                )
                        except Exception:
                            logger.warning(
                                "Claim upgrade failed for source %s, continuing",
                                source_id, exc_info=True,
                            )

                        publish_event(generation_run_id, deep_ingestion_completed(source.title or "Unknown"))
                        safe_set_run(
                            task_run,
                            outputs={
                                "status": "completed",
                                "source_became_permanent": not source.is_temporary,
                            },
                        )
                    except Exception:
                        source.ingestion_status = IngestionStatus.FAILED
                        await db.commit()
                        raise


@celery_app.task(name="app.workers.ingestion_tasks.cleanup_expired_temp_sources")
def cleanup_expired_temp_sources():
    """Clean up temporary sources past their TTL."""
    import asyncio

    asyncio.run(_cleanup_async())


async def _cleanup_async():
    from sqlalchemy import select
    from app.workers.worker_db import worker_session_factory
    from app.models.source import Source, IngestionStatus
    from app.models.generation_run import GenerationRun, GenerationStatus

    cutoff = datetime.utcnow() - timedelta(days=settings.temp_source_ttl_days)
    stale_cutoff = datetime.utcnow() - timedelta(hours=1)

    trace_ctx = {"component": "worker.cleanup_temp_sources"}
    with push_trace_context(trace_ctx):
        with tracing_scope(metadata=trace_ctx, tags=["worker", "cleanup"]):
            with trace_span(
                "worker.cleanup_expired_temp_sources",
                run_type="chain",
                inputs={
                    "temp_source_ttl_days": settings.temp_source_ttl_days,
                    "stale_cutoff_hours": 1,
                },
                tags=["celery-task"],
            ) as cleanup_run:
                async with worker_session_factory() as db:
                    # 1. Reset stale PROCESSING/ENQUEUED sources.
                    stale_result = await db.execute(
                        select(Source).where(
                            Source.ingestion_status.in_([IngestionStatus.PROCESSING, IngestionStatus.ENQUEUED]),
                            Source.is_temporary,
                            Source.status_changed_at < stale_cutoff,
                        )
                    )
                    stale_sources = stale_result.scalars().all()
                    for source in stale_sources:
                        source.ingestion_status = IngestionStatus.PENDING

                    # 2. Delete temp sources whose generation run is done and past TTL.
                    result = await db.execute(
                        select(Source)
                        .outerjoin(GenerationRun, Source.generation_run_id == GenerationRun.id)
                        .where(
                            Source.is_temporary,
                            Source.created_at < cutoff,
                            (GenerationRun.id.is_(None))
                            | (
                                GenerationRun.status.in_(
                                    [
                                        GenerationStatus.COMPLETED,
                                        GenerationStatus.FAILED,
                                        GenerationStatus.CANCELLED,
                                    ]
                                )
                            ),
                        )
                    )
                    expired = result.scalars().all()
                    for source in expired:
                        await db.delete(source)

                    await db.commit()

                safe_set_run(
                    cleanup_run,
                    outputs={"stale_reset_count": len(stale_sources), "deleted_count": len(expired)},
                )


async def _upgrade_claims_for_source(source_id: uuid.UUID, db) -> int:
    """Re-verify claims that reference evidence from a newly deep-ingested source.

    Returns the number of claims whose verification_level changed.
    """
    from sqlalchemy import select
    from app.models.claim import Claim, ClaimEvidence
    from app.models.evidence import EvidenceSpan
    from app.pipeline.phase4_claims.verifier import verify_single_claim

    # Find all evidence spans for this source
    ev_result = await db.execute(
        select(EvidenceSpan.id).where(EvidenceSpan.source_id == source_id)
    )
    evidence_ids = [row[0] for row in ev_result.all()]
    if not evidence_ids:
        return 0

    # Find all claims linked to these evidence spans
    ce_result = await db.execute(
        select(ClaimEvidence.claim_id)
        .where(ClaimEvidence.evidence_id.in_(evidence_ids))
        .distinct()
    )
    claim_ids = [row[0] for row in ce_result.all()]
    if not claim_ids:
        return 0

    upgraded = 0
    for claim_id in claim_ids:
        result = await db.execute(select(Claim).where(Claim.id == claim_id))
        claim = result.scalar_one_or_none()
        if not claim:
            continue

        old_level = claim.verification_level
        new_level = await verify_single_claim(claim, db)
        if new_level != old_level:
            claim.verification_level = new_level
            upgraded += 1
            logger.info(
                "Claim %s upgraded: %s → %s (after deep ingestion of source %s)",
                claim_id, old_level, new_level, source_id,
            )

    if upgraded:
        await db.commit()

    return upgraded
