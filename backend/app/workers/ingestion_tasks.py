import logging
import uuid
from datetime import datetime, timedelta, timezone

from celery.exceptions import MaxRetriesExceededError
from sqlalchemy.exc import SQLAlchemyError

from app.workers.celery_app import celery_app
from app.config import settings
from app.ingestion.web import DNSResolutionError
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope

logger = logging.getLogger(__name__)


_CONTENT_MAP_REBUILD_MAX_REQUEUE_ATTEMPTS = 5


def _utcnow_naive() -> datetime:
    """UTC naive datetime for DB columns configured without timezone."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def _remaining_content_map_debounce_seconds(
    *,
    latest_requested_at: datetime | None,
    now: datetime | None = None,
) -> int:
    """Compute remaining debounce delay from latest request timestamp."""
    if latest_requested_at is None:
        return 0
    debounce = max(0, int(settings.content_map_rebuild_debounce_seconds))
    if debounce <= 0:
        return 0
    now_dt = now or _utcnow_naive()
    elapsed = (now_dt - latest_requested_at).total_seconds()
    if elapsed >= debounce:
        return 0
    return int(round(debounce - elapsed))


def _parse_requested_at_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_non_retryable_content_map_error(exc: Exception) -> bool:
    if isinstance(exc, ValueError) and "No chunks found for project" in str(exc):
        return True
    return False


async def request_project_content_map_rebuild(
    project_id: uuid.UUID | str,
    db,
    *,
    force_now: bool = False,
) -> str | None:
    """Mark project content map as dirty and enqueue debounce rebuild task."""
    from sqlalchemy import select
    from app.models.project import Project

    project_uuid = uuid.UUID(str(project_id))
    result = await db.execute(
        select(Project)
        .where(Project.id == project_uuid)
        .with_for_update()
    )
    project = result.scalar_one_or_none()
    if project is None:
        return None

    requested_at = _utcnow_naive()
    project.content_map_dirty = True
    project.content_map_last_requested_at = requested_at
    await db.commit()

    countdown = 0 if force_now else max(0, int(settings.content_map_rebuild_debounce_seconds))
    async_result = rebuild_content_map_for_project.apply_async(
        args=[str(project_uuid)],
        kwargs={
            "force_now": force_now,
            "requested_at_iso": requested_at.isoformat(),
            "attempt": 0,
        },
        countdown=countdown,
    )
    return str(async_result.id) if getattr(async_result, "id", None) else None


@celery_app.task(
    name="app.workers.ingestion_tasks.rebuild_content_map_for_project",
    bind=True,
    max_retries=5,
)
def rebuild_content_map_for_project(
    self,
    project_id: str,
    *,
    force_now: bool = False,
    requested_at_iso: str | None = None,
    attempt: int = 0,
) -> None:
    import asyncio

    asyncio.run(
        _rebuild_content_map_for_project_async(
            project_id,
            force_now=force_now,
            requested_at_iso=requested_at_iso,
            attempt=attempt,
        )
    )


async def _rebuild_content_map_for_project_async(
    project_id: str,
    *,
    force_now: bool = False,
    requested_at_iso: str | None = None,
    attempt: int = 0,
) -> None:
    from sqlalchemy import select
    from app.ingestion.scout import build_content_map
    from app.models.project import Project
    from app.workers.worker_db import worker_session_factory

    task_requested_at = _parse_requested_at_iso(requested_at_iso)
    project_uuid = uuid.UUID(project_id)

    async with worker_session_factory() as db:
        result = await db.execute(
            select(Project)
            .where(Project.id == project_uuid)
            .with_for_update(skip_locked=True)
        )
        project = result.scalar_one_or_none()
        if project is None:
            return

        latest_requested_at = project.content_map_last_requested_at
        if (
            task_requested_at is not None
            and latest_requested_at is not None
            and latest_requested_at > task_requested_at
            and not force_now
        ):
            # Newer request exists; let the newer scheduled task handle it.
            return

        if not force_now:
            wait_seconds = _remaining_content_map_debounce_seconds(
                latest_requested_at=latest_requested_at,
            )
            if wait_seconds > 0:
                rebuild_content_map_for_project.apply_async(
                    args=[project_id],
                    kwargs={
                        "force_now": False,
                        "requested_at_iso": latest_requested_at.isoformat() if latest_requested_at else None,
                        "attempt": 0,
                    },
                    countdown=wait_seconds,
                )
                await db.commit()
                return

        if not project.content_map_dirty and not force_now:
            await db.commit()
            return

        with trace_span(
            "worker.rebuild_content_map_for_project",
            run_type="chain",
            inputs={
                "project_id": project_id,
                "force_now": force_now,
                "requested_at_iso": requested_at_iso,
            },
            tags=["celery-task"],
        ) as rebuild_run:
            try:
                content_map_id = await build_content_map(project_id, db)
                project.content_map_dirty = False
                project.content_map_last_built_at = _utcnow_naive()
                await db.commit()
                safe_set_run(
                    rebuild_run,
                    outputs={
                        "status": "completed",
                        "content_map_id": content_map_id,
                    },
                )
            except Exception as exc:
                await db.rollback()
                async with worker_session_factory() as policy_db:
                    policy_result = await policy_db.execute(select(Project).where(Project.id == project_uuid))
                    policy_project = policy_result.scalar_one_or_none()
                    if policy_project is None:
                        safe_set_run(rebuild_run, outputs={"status": "failed_project_missing"})
                        return

                    if policy_project.content_map_last_requested_at is None:
                        policy_project.content_map_last_requested_at = _utcnow_naive()

                    if _is_non_retryable_content_map_error(exc):
                        policy_project.content_map_dirty = False
                        await policy_db.commit()
                        logger.warning(
                            "Content map rebuild failed for project %s with non-retryable error; stopping retries",
                            project_id,
                            exc_info=True,
                        )
                        safe_set_run(
                            rebuild_run,
                            outputs={
                                "status": "failed_non_retryable",
                                "attempt": attempt,
                            },
                        )
                        return

                    if attempt >= _CONTENT_MAP_REBUILD_MAX_REQUEUE_ATTEMPTS:
                        policy_project.content_map_dirty = False
                        await policy_db.commit()
                        logger.warning(
                            "Content map rebuild failed for project %s; max retry attempts reached (%d)",
                            project_id,
                            _CONTENT_MAP_REBUILD_MAX_REQUEUE_ATTEMPTS,
                            exc_info=True,
                        )
                        safe_set_run(
                            rebuild_run,
                            outputs={
                                "status": "failed_retry_exhausted",
                                "attempt": attempt,
                            },
                        )
                        return

                    policy_project.content_map_dirty = True
                    await policy_db.commit()

                logger.warning(
                    "Content map rebuild failed for project %s; scheduled retry attempt %d",
                    project_id,
                    attempt + 1,
                    exc_info=True,
                )
                rebuild_content_map_for_project.apply_async(
                    args=[project_id],
                    kwargs={
                        "force_now": False,
                        "requested_at_iso": requested_at_iso,
                        "attempt": attempt + 1,
                    },
                    countdown=max(30, int(settings.content_map_rebuild_debounce_seconds)),
                )
                safe_set_run(rebuild_run, outputs={"status": "failed_requeued", "attempt": attempt})


@celery_app.task(name="app.workers.ingestion_tasks.sweep_dirty_content_maps")
def sweep_dirty_content_maps() -> None:
    import asyncio

    asyncio.run(_sweep_dirty_content_maps_async())


async def _sweep_dirty_content_maps_async() -> None:
    from sqlalchemy import or_, select
    from app.models.project import Project
    from app.workers.worker_db import worker_session_factory

    now = _utcnow_naive()
    debounce_seconds = max(0, int(settings.content_map_rebuild_debounce_seconds))
    due_cutoff = now - timedelta(seconds=debounce_seconds)

    with trace_span(
        "worker.sweep_dirty_content_maps",
        run_type="chain",
        inputs={"due_cutoff": due_cutoff.isoformat(), "debounce_seconds": debounce_seconds},
        tags=["celery-task"],
    ) as sweep_run:
        async with worker_session_factory() as db:
            result = await db.execute(
                select(Project.id, Project.content_map_last_requested_at)
                .where(
                    Project.content_map_dirty.is_(True),
                    Project.content_map_last_requested_at.is_not(None),
                    Project.content_map_last_requested_at <= due_cutoff,
                    or_(
                        Project.content_map_last_built_at.is_(None),
                        Project.content_map_last_built_at < Project.content_map_last_requested_at,
                    ),
                )
                .order_by(Project.content_map_last_requested_at.asc())
                .limit(100)
            )
            due_projects = result.all()

        scheduled = 0
        failed = 0
        for project_id, requested_at in due_projects:
            try:
                rebuild_content_map_for_project.apply_async(
                    args=[str(project_id)],
                    kwargs={
                        "force_now": False,
                        "requested_at_iso": requested_at.isoformat() if requested_at else None,
                        "attempt": 0,
                    },
                    countdown=0,
                )
                scheduled += 1
            except Exception:
                failed += 1
                logger.warning(
                    "Failed to enqueue content-map rebuild from sweep for project %s",
                    project_id,
                    exc_info=True,
                )

        safe_set_run(
            sweep_run,
            outputs={
                "due_projects": len(due_projects),
                "scheduled": scheduled,
                "enqueue_failed": failed,
            },
        )


@celery_app.task(name="app.workers.ingestion_tasks.ingest_source", bind=True, max_retries=5)
def ingest_source(self, source_id: str, reset_existing: bool = False):
    """Main ingestion task: extracts text, chunks, embeds."""
    import asyncio

    try:
        found = asyncio.run(_ingest_source_async(source_id, reset_existing=reset_existing))
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


async def _ingest_source_async(source_id: str, *, reset_existing: bool = False) -> bool:
    from sqlalchemy import select
    from app.workers.worker_db import worker_session_factory
    from app.models.source import Source, IngestionStatus

    async with worker_session_factory() as db:
        source_uuid = uuid.UUID(source_id)

        # Existence check keeps "not found" retry behavior intact.
        exists_result = await db.execute(select(Source.id).where(Source.id == source_uuid))
        if exists_result.scalar_one_or_none() is None:
            return False

        # Lock source row to prevent duplicate concurrent ingestion for same source_id.
        result = await db.execute(
            select(Source)
            .where(Source.id == source_uuid)
            .with_for_update(skip_locked=True)
        )
        source = result.scalar_one_or_none()
        if source is None:
            # Another worker already owns the row lock for this source.
            return True

        trace_ctx = {
            "component": "worker.ingestion",
            "source_id": str(source.id),
            "project_id": str(source.project_id),
            "generation_run_id": str(source.generation_run_id) if source.generation_run_id else "",
            "source_type": source.source_type,
            "reset_existing": reset_existing,
        }
        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["worker", "ingestion"]):
                with trace_span(
                    "worker.ingest_source",
                    run_type="chain",
                    inputs={"source_id": source_id},
                    metadata={"source_type": source.source_type, "reset_existing": reset_existing},
                    tags=["celery-task"],
                ) as task_run:
                    # Duplicate-dispatch guard: if another task already started, this one exits.
                    if source.ingestion_status == IngestionStatus.PROCESSING and not reset_existing:
                        safe_set_run(task_run, outputs={"status": "noop_already_in_progress"})
                        return True

                    if source.ingestion_status == IngestionStatus.COMPLETED and not reset_existing:
                        safe_set_run(task_run, outputs={"status": "noop_already_completed"})
                        return True

                    source.ingestion_status = IngestionStatus.PROCESSING
                    await db.commit()

                    try:
                        if reset_existing:
                            await _reset_source_for_reingestion(source, db)

                        from app.ingestion.router import run_ingestion

                        await run_ingestion(source, db)

                        source.ingestion_status = IngestionStatus.COMPLETED
                        source.fully_ingested = True
                        if source.metadata_ and isinstance(source.metadata_, dict) and "error" in source.metadata_:
                            source.metadata_ = {k: v for k, v in source.metadata_.items() if k != "error"}
                        await db.commit()
                        await _sync_v2_corpus_after_ingestion(source, db)
                        content_map_task_id: str | None = None
                        try:
                            content_map_task_id = await request_project_content_map_rebuild(
                                source.project_id,
                                db,
                                force_now=False,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to enqueue debounced content map rebuild for project %s",
                                source.project_id,
                                exc_info=True,
                            )

                        safe_set_run(
                            task_run,
                            outputs={
                                "status": "completed",
                                "fully_ingested": source.fully_ingested,
                                "content_map_task_id": content_map_task_id or "",
                            },
                        )
                    except Exception as e:
                        await db.rollback()
                        refresh_result = await db.execute(select(Source).where(Source.id == source_uuid))
                        source = refresh_result.scalar_one_or_none()
                        if source is None:
                            raise
                        source.ingestion_status = IngestionStatus.FAILED
                        source.metadata_ = {**(source.metadata_ or {}), "error": str(e)}
                        await db.commit()
                        raise

    return True


async def _reset_source_for_reingestion(source, db) -> None:
    """Purge source index/chunks and reset source fields before full re-ingestion."""
    from sqlalchemy import delete, update
    from app.models.chunk import Chunk
    from app.models.evidence import EvidenceSpan
    from app.models.source import SourceType
    from app.services.embeddings import delete_points_by_source_id

    # Cleanup runs in worker (after enqueue succeeded) to avoid API-side data loss
    # when broker enqueue fails.
    delete_points_by_source_id(str(source.id))
    # Keep evidence rows but detach old chunk references to avoid FK violations
    # when purging source chunks.
    await db.execute(
        update(EvidenceSpan)
        .where(
            EvidenceSpan.source_id == source.id,
            EvidenceSpan.chunk_id.is_not(None),
        )
        .values(chunk_id=None)
    )
    await db.execute(delete(Chunk).where(Chunk.source_id == source.id))

    source.fully_ingested = False
    source.raw_text_path = None
    source.text_preview = None
    source.published_at = None
    if source.source_type == SourceType.WEB:
        source.title = None

    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
    metadata = dict(metadata)
    metadata.pop("error", None)
    metadata.pop("web_quality", None)
    source.metadata_ = metadata or None
    await db.flush()


async def _sync_v2_corpus_after_ingestion(source, db) -> None:
    try:
        from app.generation_v2.source_corpus import sync_project_corpus_index_now

        await sync_project_corpus_index_now(db, source.project_id)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning(
            "Failed to sync V2 project corpus index after ingestion for source %s",
            source.id,
            exc_info=True,
        )


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
                        await _sync_v2_corpus_after_ingestion(source, db)
                        content_map_task_id: str | None = None
                        try:
                            content_map_task_id = await request_project_content_map_rebuild(
                                source.project_id,
                                db,
                                force_now=False,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to enqueue debounced content map rebuild for project %s",
                                source.project_id,
                                exc_info=True,
                            )

                        # Upgrade claims referencing evidence from this source
                        try:
                            upgraded = await _upgrade_claims_for_source(
                                source.id,
                                db,
                                generation_run_id=generation_run_id,
                            )
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
                                "content_map_task_id": content_map_task_id or "",
                            },
                        )
                    except Exception as exc:
                        await db.rollback()
                        refresh_result = await db.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
                        source = refresh_result.scalar_one_or_none()
                        if source is not None:
                            source.ingestion_status = IngestionStatus.FAILED
                            source.metadata_ = {**(source.metadata_ or {}), "error": str(exc)}
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
    from app.models.generation_v2 import EvidenceItem, ProviderCorpusFile, SourceVersion

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
                    expired = []
                    retained_for_evidence = 0
                    for source in result.scalars().all():
                        if isinstance(source.metadata_, dict) and source.metadata_.get("deleted") is True:
                            continue
                        has_v2_refs = False
                        for model in (SourceVersion, ProviderCorpusFile, EvidenceItem):
                            ref_result = await db.execute(select(model.id).where(model.source_id == source.id).limit(1))
                            if ref_result.scalar_one_or_none() is not None:
                                has_v2_refs = True
                                break
                        if has_v2_refs:
                            retained_for_evidence += 1
                            source.metadata_ = {
                                **(source.metadata_ or {}),
                                "temp_cleanup_status": "retained_for_v2_evidence",
                            }
                            continue
                        expired.append(source)
                    for source in expired:
                        await db.delete(source)

                    await db.commit()

                safe_set_run(
                    cleanup_run,
                    outputs={
                        "stale_reset_count": len(stale_sources),
                        "deleted_count": len(expired),
                        "retained_for_v2_evidence_count": retained_for_evidence,
                    },
                )


async def _load_reverify_question_context_map(
    *,
    generation_run_id: str | uuid.UUID | None,
    db,
) -> dict[str, dict]:
    from sqlalchemy import select
    from app.models.episode_manifest import EpisodeManifest
    from app.pipeline.research_context import build_question_context_map

    if not generation_run_id:
        return {}

    run_uuid = generation_run_id if isinstance(generation_run_id, uuid.UUID) else uuid.UUID(str(generation_run_id))
    result = await db.execute(
        select(EpisodeManifest).where(EpisodeManifest.generation_run_id == run_uuid)
    )
    manifest = result.scalar_one_or_none()
    return build_question_context_map(manifest.research_tree_json if manifest else None)


async def _upgrade_claims_for_source(
    source_id: uuid.UUID,
    db,
    *,
    generation_run_id: str | uuid.UUID | None = None,
) -> int:
    """Re-verify claims that reference evidence from a newly deep-ingested source.

    Returns the number of claims whose verification_level changed.
    """
    from sqlalchemy import select
    from app.models.claim import Claim, ClaimEvidence
    from app.models.evidence import EvidenceSpan
    from app.pipeline.phase4_claims.verifier import verify_single_claim

    question_context_map = await _load_reverify_question_context_map(
        generation_run_id=generation_run_id,
        db=db,
    )

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
        new_level = await verify_single_claim(
            claim,
            db,
            generation_run_id=str(generation_run_id) if generation_run_id else None,
            question_context_map=question_context_map,
        )
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
