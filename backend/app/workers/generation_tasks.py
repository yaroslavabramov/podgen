import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

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


@celery_app.task(name="app.workers.generation_tasks.run_generation_v2", bind=True, max_retries=0)
def run_generation_v2(self, generation_run_id: str, generate_audio: bool = True):
    """Launch the Generation V2 editorial pipeline."""
    import asyncio

    asyncio.run(_run_generation_v2_async(generation_run_id, generate_audio=generate_audio))


@celery_app.task(name="app.workers.generation_tasks.run_generation_compare", bind=True, max_retries=0)
def run_generation_compare(self, generation_run_id: str):
    """Launch a V1/V2 draft comparison run."""
    import asyncio

    asyncio.run(_run_generation_compare_async(generation_run_id))


@celery_app.task(name="app.workers.generation_tasks.promote_generation_variant_to_audio", bind=True, max_retries=0)
def promote_generation_variant_to_audio(self, generation_run_id: str):
    """Generate audio for a completed draft variant."""
    import asyncio

    asyncio.run(_promote_generation_variant_to_audio_async(generation_run_id))


def _merge_step_control(config: dict[str, Any] | None, action: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(config or {})
    merged["execution_mode"] = "step"

    step_control = dict(merged.get("step_control") or {})
    step_control["last_action"] = action
    step_control["updated_at"] = datetime.utcnow().isoformat()
    step_control["requested_action"] = None
    step_control["requested_target_node"] = None
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
                "available_rerun_nodes": result.get("available_rerun_nodes", []),
                "checkpoint_id": result.get("checkpoint_id"),
                "is_complete": bool(result.get("completed", False)),
                "can_rerun": bool(result.get("last_node")),
            }
        )
    merged["step_control"] = step_control
    return merged


def _merge_v2_step_control(config: dict[str, Any] | None, action: str, result: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_step_control(config, action, result)
    merged.pop("v2_stop_after_step", None)
    step_control = dict(merged.get("step_control") or {})
    # V2 does not yet have artifact checkpoint replay for phase reruns.
    step_control["can_rerun"] = False
    step_control["available_rerun_nodes"] = []
    step_control["checkpoint_id"] = None
    merged["step_control"] = step_control
    return merged


_NODE_PHASES = {
    "setup_show": "phase0_setup",
    "decompose": "phase2_decomposition",
    "hunt_closed_book": "phase3_research",
    "research_fast_web": "phase3_research",
    "build_claims": "phase4_claims",
    "dedupe_claims": "phase4_claims",
    "verify_claims": "phase4_claims",
    "resolve_conflicts": "phase4_claims",
    "plan_episode": "phase5_planning",
    "plan_acts": "phase5_planning",
    "integrate_acts": "phase5_planning",
    "write_screenplay": "phase6_writing",
    "evaluate_and_revolt": "phase6_writing",
    "audit_citations": "phase6_writing",
    "generate_audio": "phase7_audio",
    "build_shownotes": "phase7_audio",
}


def _node_from_step_control(step_control: dict[str, Any], key: str) -> str | None:
    candidate = step_control.get(key)
    return candidate if isinstance(candidate, str) and candidate else None


def _stopped_step_position(step_control: dict[str, Any], action: str, *, started: bool) -> dict[str, Any]:
    last_completed_node = _node_from_step_control(step_control, "last_node")
    requested_target_node = _node_from_step_control(step_control, "requested_target_node")
    interrupted_node = (
        requested_target_node or _node_from_step_control(step_control, "last_node")
        if action == "rerun"
        else _node_from_step_control(step_control, "next_node")
    )
    next_action_node = _node_from_step_control(step_control, "next_node")
    checkpoint_id = _node_from_step_control(step_control, "checkpoint_id")

    return {
        "last_node": last_completed_node,
        "executed_node": interrupted_node if started else last_completed_node,
        "next_node": next_action_node,
        "checkpoint_id": checkpoint_id,
        "is_complete": False,
        "can_rerun": bool(last_completed_node),
        "resume_node": interrupted_node,
        "resume_phase": _NODE_PHASES.get(next_action_node or interrupted_node or last_completed_node or "", ""),
    }


def _mark_step_stopped(
    config: dict[str, Any] | None,
    action: str,
    reason: str,
    *,
    started: bool,
) -> dict[str, Any]:
    merged = dict(config or {})
    merged["execution_mode"] = "step"

    step_control = dict(merged.get("step_control") or {})
    position = _stopped_step_position(step_control, action, started=started)
    step_control["last_action"] = action
    step_control["updated_at"] = datetime.utcnow().isoformat()
    step_control["requested_action"] = None
    step_control["requested_target_node"] = None
    step_control["requested_at"] = None
    step_control["stop_requested"] = False
    step_control["stop_requested_at"] = None
    step_control["stop_acknowledged_at"] = datetime.utcnow().isoformat()
    step_control["stop_reason"] = reason
    step_control["last_node"] = position["last_node"]
    step_control["executed_node"] = position["executed_node"]
    step_control["next_node"] = position["next_node"]
    step_control["checkpoint_id"] = position["checkpoint_id"]
    step_control["is_complete"] = position["is_complete"]
    step_control["can_rerun"] = position["can_rerun"]

    merged["step_control"] = step_control
    return merged


def _step_control(config: dict[str, Any] | None) -> dict[str, Any]:
    return dict((config or {}).get("step_control") or {})


async def _run_pipeline_async(generation_run_id: str, resume: bool = False):
    from app.workers.worker_db import worker_session_factory
    from app.models.generation_run import GenerationRun, GenerationStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_cancelled, generation_completed, generation_failed
    from app.pipeline.step_control import StepExecutionStopped, assert_step_not_stopped

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
            "execution_mode": (run.config or {}).get("execution_mode", "full"),
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
                    # Claim the queued run atomically so an API-side queued cancel wins
                    # even if this worker loaded a stale QUEUED row just beforehand.
                    thread_id = run.langgraph_thread_id or str(run.id)
                    start_at = datetime.utcnow()
                    claimed = await db.execute(
                        update(GenerationRun)
                        .where(
                            GenerationRun.id == run.id,
                            GenerationRun.status == GenerationStatus.QUEUED,
                        )
                        .values(
                            status=GenerationStatus.RUNNING,
                            error_message=None,
                            started_at=start_at,
                            langgraph_thread_id=thread_id,
                        )
                    )
                    if (claimed.rowcount or 0) == 0:
                        result = await db.execute(
                            select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id))
                        )
                        run = result.scalar_one_or_none()
                        if run is None or run.status == GenerationStatus.CANCELLED:
                            return
                        # Another worker or API transition already won this run state.
                        return

                    await db.commit()
                    result = await db.execute(
                        select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id))
                    )
                    run = result.scalar_one_or_none()
                    if run is None or run.status == GenerationStatus.CANCELLED:
                        return

                    try:
                        from app.pipeline.graph import execute_pipeline

                        await assert_step_not_stopped()
                        episode_id = await execute_pipeline(run, db, resume=resume)

                        run.status = GenerationStatus.COMPLETED
                        run.completed_at = datetime.utcnow()
                        await db.commit()

                        publish_event(generation_run_id, generation_completed(str(episode_id)))
                        safe_set_run(task_run, outputs={"status": "completed", "episode_id": str(episode_id)})

                    except StepExecutionStopped as stop_exc:
                        run.status = GenerationStatus.CANCELLED
                        run.error_message = None
                        run.completed_at = datetime.utcnow()
                        run.config = _ack_run_stop_requested(run.config, str(stop_exc))
                        await db.commit()

                        publish_event(generation_run_id, generation_cancelled(str(stop_exc)))
                        safe_set_run(task_run, outputs={"status": "cancelled", "reason": str(stop_exc)})

                    except Exception as e:
                        run.status = GenerationStatus.FAILED
                        run.error_message = str(e)
                        run.completed_at = datetime.utcnow()
                        await db.commit()

                        publish_event(generation_run_id, generation_failed(str(e)))
                        raise


def _ack_run_stop_requested(config: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    merged = dict(config or {})
    merged["stop_requested"] = False
    merged["stop_acknowledged_at"] = datetime.utcnow().isoformat()
    merged["stop_reason"] = reason
    return merged


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
        requested_target_node = step_control.get("requested_target_node")
        if not (isinstance(requested_action, str) and requested_action == action):
            # Stale queued task or action already cleared.
            return

        if step_control.get("stop_requested"):
            run.status = GenerationStatus.QUEUED
            run.error_message = None
            run.completed_at = None
            run.config = _mark_step_stopped(
                run.config,
                action,
                "stopped_before_start",
                started=False,
            )
            stopped_control = _step_control(run.config)
            run.current_step = stopped_control.get("next_node") or run.current_step
            resume_phase = _stopped_step_position(step_control, action, started=False).get("resume_phase")
            if isinstance(resume_phase, str) and resume_phase:
                run.current_phase = resume_phase
            await db.commit()

            publish_event(
                generation_run_id,
                generation_paused(
                    stopped_control.get("executed_node"),
                    stopped_control.get("next_node"),
                    stopped=True,
                    reason="stopped_before_start",
                ),
            )
            return

        if (run.config or {}).get("generation_mode") == "v2":
            await _run_v2_step_action(
                db,
                run,
                generation_run_id=generation_run_id,
                action=action,
                requested_target_node=(
                    requested_target_node
                    if isinstance(requested_target_node, str) and requested_target_node
                    else None
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

                        step_result = await execute_pipeline_step(
                            run,
                            db,
                            action=action,
                            target_node=(
                                requested_target_node
                                if isinstance(requested_target_node, str) and requested_target_node
                                else None
                            ),
                        )
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
                        run.config = _mark_step_stopped(
                            run.config,
                            action,
                            str(stop_exc),
                            started=True,
                        )
                        stopped_control = _step_control(run.config)
                        run.current_step = stopped_control.get("next_node") or run.current_step
                        resume_phase = _stopped_step_position(step_control, action, started=True).get("resume_phase")
                        if isinstance(resume_phase, str) and resume_phase:
                            run.current_phase = resume_phase
                        await db.commit()

                        publish_event(
                            generation_run_id,
                            generation_paused(
                                stopped_control.get("executed_node"),
                                stopped_control.get("next_node"),
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
                                "next_node": stopped_control.get("next_node"),
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


async def _run_v2_step_action(db, run, *, generation_run_id: str, action: str, requested_target_node: str | None) -> None:
    from app.generation_v2.orchestrator import V2StepExecutionStopped, execute_generation_v2
    from app.models.generation_run import GenerationStatus
    from app.realtime.events import generation_completed, generation_failed, generation_paused
    from app.realtime.publisher import publish_event

    step_control = _step_control(run.config)
    target_step = requested_target_node or step_control.get("next_node")
    if not isinstance(target_step, str) or not target_step:
        run.status = GenerationStatus.COMPLETED
        run.completed_at = datetime.utcnow()
        run.config = _merge_v2_step_control(
            run.config,
            action,
            {
                "last_node": step_control.get("last_node"),
                "executed_node": step_control.get("executed_node"),
                "next_node": None,
                "completed": True,
            },
        )
        await db.commit()
        publish_event(generation_run_id, generation_completed(""))
        return

    trace_ctx = _trace_context(run, component="worker.generation.v2.step")
    trace_ctx["step_action"] = action
    trace_ctx["v2_stop_after_step"] = target_step
    with push_trace_context(trace_ctx):
        with tracing_scope(metadata=trace_ctx, tags=["worker", "generation", "v2", "step"]):
            with trace_span(
                "worker.run_generation_v2_step",
                run_type="chain",
                inputs={"generation_run_id": generation_run_id, "action": action, "target_step": target_step},
                tags=["celery-task", "generation-v2", "step-mode"],
            ) as task_run:
                run.status = GenerationStatus.RUNNING
                run.error_message = None
                run.completed_at = None
                if run.started_at is None:
                    run.started_at = datetime.utcnow()
                run.config = {**(run.config or {}), "v2_stop_after_step": target_step}
                await db.commit()

                try:
                    episode_id = await execute_generation_v2(run, db, generate_audio=True)
                    run.status = GenerationStatus.COMPLETED
                    run.completed_at = datetime.utcnow()
                    run.config = _merge_v2_step_control(
                        run.config,
                        action,
                        {
                            "last_node": target_step,
                            "executed_node": target_step,
                            "next_node": None,
                            "completed": True,
                            "episode_id": str(episode_id or ""),
                        },
                    )
                    await db.commit()
                    publish_event(generation_run_id, generation_completed(str(episode_id or "")))
                    safe_set_run(task_run, outputs={"status": "completed", "target_step": target_step})
                except V2StepExecutionStopped as stop_exc:
                    completed = stop_exc.next_step is None
                    run.status = GenerationStatus.COMPLETED if completed else GenerationStatus.QUEUED
                    run.completed_at = datetime.utcnow() if completed else None
                    run.error_message = None
                    run.config = _merge_v2_step_control(
                        run.config,
                        action,
                        {
                            "last_node": stop_exc.executed_step,
                            "executed_node": stop_exc.executed_step,
                            "next_node": stop_exc.next_step,
                            "completed": completed,
                        },
                    )
                    await db.commit()
                    if completed:
                        publish_event(generation_run_id, generation_completed(""))
                    else:
                        publish_event(generation_run_id, generation_paused(stop_exc.executed_step, stop_exc.next_step))
                    safe_set_run(
                        task_run,
                        outputs={
                            "status": "completed" if completed else "paused",
                            "executed_node": stop_exc.executed_step,
                            "next_node": stop_exc.next_step,
                        },
                    )
                except Exception as exc:
                    run.status = GenerationStatus.FAILED
                    run.error_message = str(exc)
                    run.completed_at = datetime.utcnow()
                    run.config = _merge_v2_step_control(
                        run.config,
                        action,
                        {
                            "last_node": step_control.get("last_node"),
                            "executed_node": None,
                            "next_node": target_step,
                            "completed": False,
                        },
                    )
                    await db.commit()
                    publish_event(generation_run_id, generation_failed(str(exc)))
                    safe_set_run(task_run, outputs={"status": "failed", "error": str(exc), "target_step": target_step})
                    raise


async def _run_generation_v2_async(generation_run_id: str, *, generate_audio: bool = True):
    from app.workers.worker_db import worker_session_factory
    from app.models.generation_run import GenerationRun, GenerationStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_cancelled, generation_completed, generation_failed
    from app.pipeline.step_control import StepExecutionStopped, assert_step_not_stopped

    async with worker_session_factory() as db:
        result = await db.execute(select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id)))
        run = result.scalar_one_or_none()
        if run is None or run.status == GenerationStatus.CANCELLED:
            return

        trace_ctx = _trace_context(run, component="worker.generation.v2")
        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["worker", "generation", "v2"]):
                with trace_span(
                    "worker.run_generation_v2",
                    run_type="chain",
                    inputs={"generation_run_id": generation_run_id, "generate_audio": generate_audio},
                    tags=["celery-task", "generation-v2"],
                ) as task_run:
                    claimed = await _claim_run_for_worker(db, run)
                    if not claimed:
                        return
                    result = await db.execute(select(GenerationRun).where(GenerationRun.id == run.id))
                    run = result.scalar_one_or_none()
                    if run is None or run.status == GenerationStatus.CANCELLED:
                        return
                    try:
                        await assert_step_not_stopped()
                        from app.generation_v2.orchestrator import execute_generation_v2

                        episode_id = await execute_generation_v2(run, db, generate_audio=generate_audio)
                        from app.generation_v2.source_workspace import cleanup_provider_workspace

                        await cleanup_provider_workspace(db, run.id)
                        run.status = GenerationStatus.COMPLETED
                        run.completed_at = datetime.utcnow()
                        await db.commit()
                        publish_event(generation_run_id, generation_completed(str(episode_id or "")))
                        safe_set_run(task_run, outputs={"status": "completed", "episode_id": str(episode_id or "")})
                    except StepExecutionStopped as stop_exc:
                        run.status = GenerationStatus.CANCELLED
                        run.error_message = None
                        run.completed_at = datetime.utcnow()
                        run.config = _ack_run_stop_requested(run.config, str(stop_exc))
                        from app.generation_v2.source_workspace import cleanup_provider_workspace

                        await cleanup_provider_workspace(db, run.id)
                        await db.commit()
                        publish_event(generation_run_id, generation_cancelled(str(stop_exc)))
                    except Exception as exc:
                        run.status = GenerationStatus.FAILED
                        run.error_message = str(exc)
                        run.completed_at = datetime.utcnow()
                        from app.generation_v2.source_workspace import cleanup_provider_workspace

                        await cleanup_provider_workspace(db, run.id)
                        await db.commit()
                        publish_event(generation_run_id, generation_failed(str(exc)))
                        raise


async def _run_generation_compare_async(generation_run_id: str):
    from app.workers.worker_db import worker_session_factory
    from app.models.generation_run import GenerationRun, GenerationStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_completed, generation_failed, progress

    async with worker_session_factory() as db:
        result = await db.execute(select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id)))
        coordinator = result.scalar_one_or_none()
        if coordinator is None or coordinator.status == GenerationStatus.CANCELLED:
            return

        trace_ctx = _trace_context(coordinator, component="worker.generation.compare")
        with push_trace_context(trace_ctx):
            with tracing_scope(metadata=trace_ctx, tags=["worker", "generation", "compare"]):
                with trace_span(
                    "worker.run_generation_compare",
                    run_type="chain",
                    inputs={"generation_run_id": generation_run_id},
                    tags=["celery-task", "generation-compare"],
                ) as task_run:
                    claimed = await _claim_run_for_worker(db, coordinator)
                    if not claimed:
                        return
                    result = await db.execute(select(GenerationRun).where(GenerationRun.id == coordinator.id))
                    coordinator = result.scalar_one_or_none()
                    if coordinator is None or coordinator.status == GenerationStatus.CANCELLED:
                        return

                    v1_child = await _get_or_create_compare_child(db, coordinator, variant="v1")
                    v2_child = await _get_or_create_compare_child(db, coordinator, variant="v2")
                    coordinator.config = {
                        **(coordinator.config or {}),
                        "compare_v1_run_id": str(v1_child.id),
                        "compare_v2_run_id": str(v2_child.id),
                    }
                    await db.commit()
                    publish_event(generation_run_id, progress(5, "Compare child runs created"))

                    errors: dict[str, str] = {}
                    try:
                        await _execute_v1_compare_child(db, v1_child)
                    except Exception as exc:
                        errors["v1"] = str(exc)
                    publish_event(generation_run_id, progress(45, "V1 draft comparison branch finished"))

                    try:
                        await _execute_v2_compare_child(db, v2_child)
                    except Exception as exc:
                        errors["v2"] = str(exc)
                    publish_event(generation_run_id, progress(95, "V2 draft comparison branch finished"))

                    coordinator.config = {**(coordinator.config or {}), "compare_errors": errors}
                    coordinator.status = GenerationStatus.FAILED if len(errors) == 2 else GenerationStatus.COMPLETED
                    coordinator.completed_at = datetime.utcnow()
                    coordinator.progress_pct = 100.0
                    await db.commit()
                    if coordinator.status == GenerationStatus.COMPLETED:
                        publish_event(generation_run_id, generation_completed(""))
                        safe_set_run(task_run, outputs={"status": "completed", "errors": errors})
                    else:
                        message = "; ".join(f"{key}: {value}" for key, value in errors.items())
                        coordinator.error_message = message
                        await db.commit()
                        publish_event(generation_run_id, generation_failed(message))
                        safe_set_run(task_run, outputs={"status": "failed", "errors": errors})


async def _promote_generation_variant_to_audio_async(generation_run_id: str):
    from app.workers.worker_db import worker_session_factory
    from app.models.generation_run import GenerationRun, GenerationStatus
    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_completed, generation_failed

    async with worker_session_factory() as db:
        result = await db.execute(select(GenerationRun).where(GenerationRun.id == uuid.UUID(generation_run_id)))
        run = result.scalar_one_or_none()
        if run is None or run.status == GenerationStatus.CANCELLED:
            return
        claimed = await _claim_run_for_worker(db, run)
        if not claimed:
            return
        result = await db.execute(select(GenerationRun).where(GenerationRun.id == run.id))
        run = result.scalar_one_or_none()
        if run is None:
            return
        try:
            from app.generation_v2.finalize import generate_audio_from_segments

            episode_id = await generate_audio_from_segments(run)
            run.status = GenerationStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            run.progress_pct = 100.0
            run.config = {**(run.config or {}), "audio_promoted": True, "promoted_episode_id": episode_id}
            await db.commit()
            publish_event(generation_run_id, generation_completed(episode_id))
        except Exception as exc:
            run.status = GenerationStatus.FAILED
            run.error_message = str(exc)
            run.completed_at = datetime.utcnow()
            await db.commit()
            publish_event(generation_run_id, generation_failed(str(exc)))
            raise


async def _claim_run_for_worker(db, run) -> bool:
    from app.models.generation_run import GenerationRun, GenerationStatus

    start_at = datetime.utcnow()
    claimed = await db.execute(
        update(GenerationRun)
        .where(
            GenerationRun.id == run.id,
            GenerationRun.status == GenerationStatus.QUEUED,
        )
        .values(
            status=GenerationStatus.RUNNING,
            error_message=None,
            started_at=start_at if run.started_at is None else run.started_at,
            completed_at=None,
        )
    )
    await db.commit()
    return (claimed.rowcount or 0) > 0


def _trace_context(run, *, component: str) -> dict[str, Any]:
    return {
        "component": component,
        "generation_run_id": str(run.id),
        "project_id": str(run.project_id),
        "user_id": str(run.user_id),
        "language": (run.config or {}).get("language", "en"),
        "execution_mode": (run.config or {}).get("execution_mode", "full"),
        "generation_mode": (run.config or {}).get("generation_mode", "v1"),
    }


def _make_compare_child(coordinator, *, variant: str):
    from app.models.generation_run import GenerationRun, GenerationStatus

    config = dict(coordinator.config or {})
    config.update(
        {
            "generation_mode": variant,
            "execution_mode": "full",
            "compare_parent_run_id": str(coordinator.id),
            "compare_variant": variant,
            "compare_draft_only": True,
        }
    )
    if variant == "v1":
        config["stop_after_node"] = "audit_citations"
    return GenerationRun(
        project_id=coordinator.project_id,
        user_id=coordinator.user_id,
        show_bible_id=coordinator.show_bible_id,
        status=GenerationStatus.QUEUED,
        config=config,
        estimated_cost=coordinator.estimated_cost,
    )


async def _get_or_create_compare_child(db, coordinator, *, variant: str):
    from app.models.generation_run import GenerationRun

    config_key = f"compare_{variant}_run_id"
    existing_id = (coordinator.config or {}).get(config_key)
    if existing_id:
        try:
            result = await db.execute(select(GenerationRun).where(GenerationRun.id == uuid.UUID(str(existing_id))))
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing
        except ValueError:
            pass
    child = _make_compare_child(coordinator, variant=variant)
    db.add(child)
    await db.flush()
    return child


async def _execute_v1_compare_child(db, run) -> None:
    from app.models.generation_run import GenerationStatus
    from app.pipeline.graph import execute_pipeline_until

    if not await _claim_run_for_worker(db, run):
        if run.status == GenerationStatus.COMPLETED:
            return
        if run.status == GenerationStatus.FAILED:
            raise RuntimeError(run.error_message or "V1 compare child already failed")
        return
    result = await db.execute(select(type(run)).where(type(run).id == run.id))
    run = result.scalar_one_or_none()
    if run is None:
        return
    try:
        await execute_pipeline_until(run, db, stop_after_node="audit_citations")
        run.status = GenerationStatus.COMPLETED
        run.completed_at = datetime.utcnow()
        run.progress_pct = 100.0
        await db.commit()
    except Exception as exc:
        run.status = GenerationStatus.FAILED
        run.error_message = str(exc)
        run.completed_at = datetime.utcnow()
        await db.commit()
        raise


async def _execute_v2_compare_child(db, run) -> None:
    from app.models.generation_run import GenerationStatus
    from app.generation_v2.orchestrator import execute_generation_v2

    if not await _claim_run_for_worker(db, run):
        if run.status == GenerationStatus.COMPLETED:
            return
        if run.status == GenerationStatus.FAILED:
            raise RuntimeError(run.error_message or "V2 compare child already failed")
        return
    result = await db.execute(select(type(run)).where(type(run).id == run.id))
    run = result.scalar_one_or_none()
    if run is None:
        return
    try:
        await execute_generation_v2(run, db, generate_audio=False)
        from app.generation_v2.source_workspace import cleanup_provider_workspace

        await cleanup_provider_workspace(db, run.id)
        run.status = GenerationStatus.COMPLETED
        run.completed_at = datetime.utcnow()
        run.progress_pct = 100.0
        await db.commit()
    except Exception as exc:
        run.status = GenerationStatus.FAILED
        run.error_message = str(exc)
        run.completed_at = datetime.utcnow()
        from app.generation_v2.source_workspace import cleanup_provider_workspace

        await cleanup_provider_workspace(db, run.id)
        await db.commit()
        raise
