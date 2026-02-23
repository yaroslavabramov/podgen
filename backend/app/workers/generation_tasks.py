import uuid
from datetime import datetime

from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.generation_tasks.run_generation_pipeline", bind=True, max_retries=0)
def run_generation_pipeline(self, generation_run_id: str, resume: bool = False):
    """Launch the LangGraph pipeline for podcast generation."""
    import asyncio
    asyncio.run(_run_pipeline_async(generation_run_id, resume))


async def _run_pipeline_async(generation_run_id: str, resume: bool = False):
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.generation_run import GenerationRun, GenerationStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_completed, generation_failed

    async with async_session_factory() as db:
        result = await db.execute(
            select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id))
        )
        run = result.scalar_one_or_none()
        if run is None:
            return

        if run.status == GenerationStatus.CANCELLED:
            return

        run.status = GenerationStatus.RUNNING
        run.started_at = datetime.utcnow()
        await db.commit()

        try:
            from app.pipeline.graph import execute_pipeline
            episode_id = await execute_pipeline(run, db, resume=resume)

            run.status = GenerationStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            await db.commit()

            publish_event(generation_run_id, generation_completed(str(episode_id)))

        except Exception as e:
            run.status = GenerationStatus.FAILED
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            await db.commit()

            publish_event(generation_run_id, generation_failed(str(e)))
            raise
