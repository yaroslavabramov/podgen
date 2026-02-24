import uuid
from datetime import datetime

from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.generation_tasks.run_generation_pipeline", bind=True, max_retries=0)
def run_generation_pipeline(self, generation_run_id: str, resume: bool = False):
    """Launch the LangGraph pipeline for podcast generation."""
    import asyncio

    asyncio.run(_run_pipeline_async(generation_run_id, resume))


async def _run_pipeline_async(generation_run_id: str, resume: bool = False):
    from sqlalchemy import select
    from app.workers.worker_db import worker_session_factory
    from app.models.generation_run import GenerationRun, GenerationStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_completed, generation_failed

    async with worker_session_factory() as db:
        result = await db.execute(
            select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id))
        )
        run = result.scalar_one_or_none()
        if run is None:
            return

        if run.status == GenerationStatus.CANCELLED:
            return

        trace_ctx = {
            "component": "worker.generation",
            "generation_run_id": str(run.id),
            "project_id": str(run.project_id),
            "user_id": str(run.user_id),
            "language": (run.config or {}).get("language", "en"),
        }

        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["worker", "generation"]):
                with trace_span(
                    "worker.run_generation_pipeline",
                    run_type="chain",
                    inputs={"generation_run_id": generation_run_id, "resume": resume},
                    metadata={"langgraph_thread_id": run.langgraph_thread_id or str(run.id)},
                    tags=["celery-task"],
                ) as task_run:
                    if not run.langgraph_thread_id:
                        run.langgraph_thread_id = str(run.id)

                    run.status = GenerationStatus.RUNNING
                    run.error_message = None
                    run.started_at = datetime.utcnow()
                    await db.commit()

                    try:
                        from app.pipeline.graph import execute_pipeline

                        episode_id = await execute_pipeline(run, db, resume=resume)

                        run.status = GenerationStatus.COMPLETED
                        run.completed_at = datetime.utcnow()
                        await db.commit()

                        publish_event(generation_run_id, generation_completed(str(episode_id)))
                        safe_set_run(task_run, outputs={"status": "completed", "episode_id": str(episode_id)})

                    except Exception as e:
                        run.status = GenerationStatus.FAILED
                        run.error_message = str(e)
                        run.completed_at = datetime.utcnow()
                        await db.commit()

                        publish_event(generation_run_id, generation_failed(str(e)))
                        raise
