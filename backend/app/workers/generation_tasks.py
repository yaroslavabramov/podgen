import uuid
from datetime import datetime
from typing import Any

from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.generation_tasks.run_generation_pipeline", bind=True, max_retries=0)
def run_generation_pipeline(self, generation_run_id: str, resume: bool = False):
    """Launch the LangGraph pipeline for podcast generation."""
    import asyncio

    asyncio.run(_run_pipeline_async(generation_run_id, resume))


@celery_app.task(name="app.workers.generation_tasks.run_generation_step", bind=True, max_retries=0)
def run_generation_step(self, generation_run_id: str, action: str = "next"):
    """Launch a single pipeline step (debug mode)."""
    import asyncio

    asyncio.run(_run_step_async(generation_run_id, action))


def _merge_step_control(config: dict[str, Any] | None, action: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(config or {})
    merged["execution_mode"] = "step"

    step_control = dict(merged.get("step_control") or {})
    step_control["last_action"] = action
    step_control["updated_at"] = datetime.utcnow().isoformat()
    step_control["requested_action"] = None
    step_control["requested_at"] = None
    step_control["stop_requested"] = False
    step_control["stop_requested_at"] = None
    step_control["stop_acknowledged_at"] = None
    step_control["stop_reason"] = None

    if result is not None:
        step_control.update(
            {
                "last_node": result.get("last_node"),
                "executed_node": result.get("executed_node"),
                "next_node": result.get("next_node"),
                "checkpoint_id": result.get("checkpoint_id"),
                "is_complete": bool(result.get("completed", False)),
                "can_rerun": bool(result.get("last_node")),
            }
        )
    merged["step_control"] = step_control
    return merged


def _mark_step_stopped(config: dict[str, Any] | None, action: str, reason: str) -> dict[str, Any]:
    merged = dict(config or {})
    merged["execution_mode"] = "step"

    step_control = dict(merged.get("step_control") or {})
    step_control["last_action"] = action
    step_control["updated_at"] = datetime.utcnow().isoformat()
    step_control["requested_action"] = None
    step_control["requested_at"] = None
    step_control["stop_requested"] = False
    step_control["stop_acknowledged_at"] = datetime.utcnow().isoformat()
    step_control["stop_reason"] = reason

    merged["step_control"] = step_control
    return merged


def _step_control(config: dict[str, Any] | None) -> dict[str, Any]:
    return dict((config or {}).get("step_control") or {})


def _next_node_for_stop(step_control: dict[str, Any], action: str) -> str | None:
    if action == "rerun":
        candidate = step_control.get("last_node")
    else:
        candidate = step_control.get("next_node")
    return candidate if isinstance(candidate, str) and candidate else None


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


async def _run_step_async(generation_run_id: str, action: str = "next"):
    from sqlalchemy import select
    from app.workers.worker_db import worker_session_factory
    from app.models.generation_run import GenerationRun, GenerationStatus
    from app.pipeline.step_control import StepExecutionStopped
    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_completed, generation_failed, generation_paused

    async with worker_session_factory() as db:
        result = await db.execute(
            select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id))
        )
        run = result.scalar_one_or_none()
        if run is None:
            return

        if run.status == GenerationStatus.CANCELLED:
            return
        if (run.config or {}).get("execution_mode") != "step":
            return

        step_control = _step_control(run.config)
        requested_action = step_control.get("requested_action")
        if not (isinstance(requested_action, str) and requested_action == action):
            # Stale queued task or action already cleared.
            return

        if step_control.get("stop_requested"):
            run.status = GenerationStatus.QUEUED
            run.error_message = None
            run.completed_at = None
            run.config = _mark_step_stopped(run.config, action, "stopped_before_start")
            await db.commit()

            publish_event(
                generation_run_id,
                generation_paused(
                    step_control.get("executed_node"),
                    _next_node_for_stop(step_control, action),
                    stopped=True,
                    reason="stopped_before_start",
                ),
            )
            return

        trace_ctx = {
            "component": "worker.generation.step",
            "generation_run_id": str(run.id),
            "project_id": str(run.project_id),
            "user_id": str(run.user_id),
            "step_action": action,
            "execution_mode": "step",
            "language": (run.config or {}).get("language", "en"),
        }

        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["worker", "generation", "step"]):
                with trace_span(
                    "worker.run_generation_step",
                    run_type="chain",
                    inputs={"generation_run_id": generation_run_id, "action": action},
                    metadata={"langgraph_thread_id": run.langgraph_thread_id or str(run.id)},
                    tags=["celery-task", "step-mode"],
                ) as task_run:
                    if not run.langgraph_thread_id:
                        run.langgraph_thread_id = str(run.id)

                    run.status = GenerationStatus.RUNNING
                    run.error_message = None
                    if run.started_at is None:
                        run.started_at = datetime.utcnow()
                    await db.commit()

                    try:
                        from app.pipeline.graph import execute_pipeline_step

                        step_result = await execute_pipeline_step(run, db, action=action)
                        run.config = _merge_step_control(run.config, action, step_result)

                        if step_result.get("completed"):
                            episode_id = str(step_result.get("episode_id") or "")
                            run.status = GenerationStatus.COMPLETED
                            run.completed_at = datetime.utcnow()
                            await db.commit()

                            publish_event(generation_run_id, generation_completed(episode_id))
                            safe_set_run(
                                task_run,
                                outputs={
                                    "status": "completed",
                                    "action": action,
                                    "executed_node": step_result.get("executed_node"),
                                    "episode_id": episode_id,
                                },
                            )
                        else:
                            run.status = GenerationStatus.QUEUED
                            run.completed_at = None
                            await db.commit()

                            publish_event(
                                generation_run_id,
                                generation_paused(
                                    step_result.get("executed_node"),
                                    step_result.get("next_node"),
                                ),
                            )
                            safe_set_run(
                                task_run,
                                outputs={
                                    "status": "paused",
                                    "action": action,
                                    "executed_node": step_result.get("executed_node"),
                                    "next_node": step_result.get("next_node"),
                                },
                            )

                    except StepExecutionStopped as stop_exc:
                        run.status = GenerationStatus.QUEUED
                        run.error_message = None
                        run.completed_at = None
                        run.config = _mark_step_stopped(run.config, action, str(stop_exc))
                        await db.commit()

                        stopped_control = _step_control(run.config)
                        publish_event(
                            generation_run_id,
                            generation_paused(
                                stopped_control.get("executed_node"),
                                _next_node_for_stop(stopped_control, action),
                                stopped=True,
                                reason=str(stop_exc),
                            ),
                        )
                        safe_set_run(
                            task_run,
                            outputs={
                                "status": "stopped",
                                "action": action,
                                "reason": str(stop_exc),
                                "executed_node": stopped_control.get("executed_node"),
                                "next_node": _next_node_for_stop(stopped_control, action),
                            },
                        )

                    except Exception as e:
                        run.status = GenerationStatus.FAILED
                        run.error_message = str(e)
                        run.completed_at = datetime.utcnow()
                        run.config = _merge_step_control(run.config, action)
                        await db.commit()

                        publish_event(generation_run_id, generation_failed(str(e)))
                        raise
