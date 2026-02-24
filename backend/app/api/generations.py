import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, func

from app.api.deps import CurrentUser, DB
from app.models.project import Project
from app.models.source import Source
from app.models.show_bible import ShowBible
from app.models.generation_run import GenerationRun, GenerationStatus
from app.schemas.generation import GenerationCreate, CostEstimateRequest, CostEstimateResponse, GenerationRunResponse
from app.services.cost_estimator import estimate_cost

router = APIRouter(prefix="/api", tags=["generations"])


@router.post("/projects/{project_id}/estimate", response_model=CostEstimateResponse)
async def estimate_generation_cost(project_id: uuid.UUID, data: CostEstimateRequest, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    source_count = (await db.execute(
        select(func.count(Source.id)).where(Source.project_id == project_id, Source.is_temporary.is_(False))
    )).scalar() or 0
    return estimate_cost(data.duration_target, source_count)


@router.post("/projects/{project_id}/generate", response_model=GenerationRunResponse, status_code=status.HTTP_201_CREATED)
async def start_generation(project_id: uuid.UUID, data: GenerationCreate, user: CurrentUser, db: DB):
    project = await _get_user_project(project_id, user.id, db)
    created_run_scoped_bible = False

    # Resolve ShowBible
    if data.show_bible_id:
        result = await db.execute(select(ShowBible).where(ShowBible.id == data.show_bible_id))
        show_bible = result.scalar_one_or_none()
        if show_bible is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ShowBible not found")
        # Verify ownership: must belong to this project or be a global preset
        if show_bible.project_id is not None and show_bible.project_id != project_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ShowBible does not belong to this project")
        show_bible_id = show_bible.id
    elif data.show_bible_override:
        # Create run-scoped ShowBible based on project's main ShowBible
        result = await db.execute(
            select(ShowBible).where(
                ShowBible.project_id == project_id,
                ShowBible.is_preset.is_(False),
                ShowBible.generation_run_id.is_(None),
            )
        )
        base = result.scalar_one_or_none()
        if base:
            new_bible = ShowBible(
                project_id=project_id,
                audience_profile=data.show_bible_override.get("audience_profile", base.audience_profile),
                tone_and_mood=data.show_bible_override.get("tone_and_mood", base.tone_and_mood),
                show_style=data.show_bible_override.get("show_style", base.show_style),
                hosts=base.hosts,
                voice_profiles=base.voice_profiles,
                forbidden_phrases=base.forbidden_phrases,
                rigor_policy=base.rigor_policy,
                adjacency_policy=base.adjacency_policy,
                explanation_policy=base.explanation_policy,
                format_rules=base.format_rules,
            )
        else:
            new_bible = ShowBible(project_id=project_id, **data.show_bible_override)
        db.add(new_bible)
        await db.flush()
        show_bible_id = new_bible.id
        created_run_scoped_bible = True
    else:
        result = await db.execute(
            select(ShowBible).where(
                ShowBible.project_id == project_id,
                ShowBible.is_preset.is_(False),
                ShowBible.generation_run_id.is_(None),
            )
        )
        show_bible = result.scalar_one_or_none()
        if show_bible is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No ShowBible configured. Create one first or provide show_bible_override.")
        show_bible_id = show_bible.id

    # Optional voice override for this generation run.
    if data.voice_a_id or data.voice_b_id:
        result = await db.execute(select(ShowBible).where(ShowBible.id == show_bible_id))
        selected_bible = result.scalar_one_or_none()
        if selected_bible is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ShowBible not found")

        updated_voice_profiles = _merge_voice_profiles(
            selected_bible.voice_profiles or [],
            data.voice_a_id,
            data.voice_b_id,
        )

        if created_run_scoped_bible:
            selected_bible.voice_profiles = updated_voice_profiles
            await db.flush()
        else:
            run_bible = _clone_show_bible_for_run(selected_bible, project_id, updated_voice_profiles)
            db.add(run_bible)
            await db.flush()
            show_bible_id = run_bible.id
            created_run_scoped_bible = True

    # Estimate cost
    source_count = (await db.execute(
        select(func.count(Source.id)).where(Source.project_id == project_id, Source.is_temporary.is_(False))
    )).scalar() or 0
    cost = estimate_cost(data.duration_target, source_count)

    run_config = {
        "duration_target": data.duration_target,
        "user_prompt": data.user_prompt,
        "language": project.language,
        "voice_a_id": data.voice_a_id,
        "voice_b_id": data.voice_b_id,
        "execution_mode": data.execution_mode,
    }
    if data.execution_mode == "step":
        run_config["step_control"] = _initial_step_control()

    run = GenerationRun(
        project_id=project_id,
        user_id=user.id,
        show_bible_id=show_bible_id,
        status=GenerationStatus.QUEUED,
        config=run_config,
        estimated_cost=cost.model_dump(),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)

    # Link run-scoped ShowBible to this run.
    if created_run_scoped_bible:
        override_result = await db.execute(select(ShowBible).where(ShowBible.id == show_bible_id))
        override_bible = override_result.scalar_one_or_none()
        if override_bible:
            override_bible.generation_run_id = run.id
            await db.flush()

    # Trigger async generation for full mode only.
    if data.execution_mode == "full":
        from app.workers.generation_tasks import run_generation_pipeline

        run_generation_pipeline.delay(str(run.id))

    return GenerationRunResponse.model_validate(run)


@router.get("/generations/{run_id}", response_model=GenerationRunResponse)
async def get_generation(run_id: uuid.UUID, user: CurrentUser, db: DB):
    result = await db.execute(
        select(GenerationRun)
        .join(Project, GenerationRun.project_id == Project.id)
        .where(GenerationRun.id == run_id, Project.user_id == user.id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation run not found")
    return GenerationRunResponse.model_validate(run)


@router.post("/generations/{run_id}/cancel", response_model=GenerationRunResponse)
async def cancel_generation(run_id: uuid.UUID, user: CurrentUser, db: DB):
    result = await db.execute(
        select(GenerationRun)
        .join(Project, GenerationRun.project_id == Project.id)
        .where(GenerationRun.id == run_id, Project.user_id == user.id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation run not found")
    if run.status not in (GenerationStatus.QUEUED, GenerationStatus.RUNNING):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot cancel a completed run")
    run.status = GenerationStatus.CANCELLED
    await db.flush()
    await db.refresh(run)
    return GenerationRunResponse.model_validate(run)


@router.post("/generations/{run_id}/resume", response_model=GenerationRunResponse)
async def resume_generation(run_id: uuid.UUID, user: CurrentUser, db: DB):
    result = await db.execute(
        select(GenerationRun)
        .join(Project, GenerationRun.project_id == Project.id)
        .where(GenerationRun.id == run_id, Project.user_id == user.id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation run not found")
    if run.status != GenerationStatus.FAILED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Can only resume failed runs")
    if (run.config or {}).get("execution_mode") == "step":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use step controls for runs started in step mode.",
        )
    if not run.langgraph_thread_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resume unavailable for this run (missing checkpoint thread). Start a new generation run.",
        )

    run.status = GenerationStatus.QUEUED
    run.error_message = None
    run.completed_at = None
    await db.flush()
    await db.refresh(run)

    from app.workers.generation_tasks import run_generation_pipeline
    run_generation_pipeline.delay(str(run.id), resume=True)

    return GenerationRunResponse.model_validate(run)


@router.post("/generations/{run_id}/steps/next", response_model=GenerationRunResponse)
async def run_next_generation_step(run_id: uuid.UUID, user: CurrentUser, db: DB):
    return await _enqueue_step_action(run_id, user.id, db, action="next")


@router.post("/generations/{run_id}/steps/rerun", response_model=GenerationRunResponse)
async def rerun_generation_step(run_id: uuid.UUID, user: CurrentUser, db: DB):
    return await _enqueue_step_action(run_id, user.id, db, action="rerun")


@router.post("/generations/{run_id}/steps/stop", response_model=GenerationRunResponse)
async def stop_generation_step(run_id: uuid.UUID, user: CurrentUser, db: DB):
    run = await _get_user_run(run_id, user.id, db)
    step_control = dict(((run.config or {}).get("step_control") or {}))

    if (run.config or {}).get("execution_mode") != "step":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Step stop is available only for runs started in step mode.",
        )

    queued_action = step_control.get("requested_action")
    has_queued_action = isinstance(queued_action, str) and bool(queued_action)
    is_running = run.status == GenerationStatus.RUNNING
    is_queued_action = run.status == GenerationStatus.QUEUED and has_queued_action
    if not is_running and not is_queued_action:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No running or queued step action to stop.",
        )

    if step_control.get("stop_requested"):
        return GenerationRunResponse.model_validate(run)

    run.error_message = None
    run.config = _mark_step_stop_requested(run.config)
    await db.flush()
    await db.refresh(run)
    return GenerationRunResponse.model_validate(run)


async def _verify_project_ownership(project_id, user_id, db):
    result = await db.execute(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


async def _get_user_project(project_id, user_id, db) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _enqueue_step_action(run_id: uuid.UUID, user_id: uuid.UUID, db: DB, action: str) -> GenerationRunResponse:
    run = await _get_user_run(run_id, user_id, db)
    step_control = dict(((run.config or {}).get("step_control") or {}))

    if (run.config or {}).get("execution_mode") != "step":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Step actions are available only for runs started in step mode.",
        )
    if step_control.get("requested_action"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another step action is already queued. Wait for it to finish.",
        )
    if step_control.get("stop_requested"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stop requested for current step action. Wait until run is paused.",
        )
    if run.status == GenerationStatus.RUNNING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Generation run is already running")
    if run.status == GenerationStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot execute step for a cancelled run",
        )
    if action == "rerun":
        has_last_node = isinstance(step_control.get("last_node"), str) and bool(step_control.get("last_node"))
        has_checkpoint = isinstance(step_control.get("checkpoint_id"), str) and bool(step_control.get("checkpoint_id"))
        if not bool(step_control.get("can_rerun")) or not has_last_node or not has_checkpoint:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot rerun before at least one completed step. Run next step first.",
            )
    if run.status == GenerationStatus.COMPLETED and action != "rerun":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Generation run already completed. Use rerun to re-execute the last step.",
        )

    run.status = GenerationStatus.QUEUED
    run.error_message = None
    run.completed_at = None
    run.config = _mark_step_action_requested(run.config, action)
    await db.flush()
    await db.refresh(run)

    from app.workers.generation_tasks import run_generation_step

    run_generation_step.delay(str(run.id), action=action)
    return GenerationRunResponse.model_validate(run)


async def _get_user_run(run_id: uuid.UUID, user_id: uuid.UUID, db: DB) -> GenerationRun:
    result = await db.execute(
        select(GenerationRun)
        .join(Project, GenerationRun.project_id == Project.id)
        .where(GenerationRun.id == run_id, Project.user_id == user_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation run not found")
    return run


def _initial_step_control() -> dict:
    return {
        "last_action": None,
        "last_node": None,
        "executed_node": None,
        "next_node": "setup_show",
        "checkpoint_id": None,
        "is_complete": False,
        "can_rerun": False,
        "requested_action": None,
        "requested_at": None,
        "stop_requested": False,
        "stop_requested_at": None,
        "stop_acknowledged_at": None,
        "stop_reason": None,
        "updated_at": datetime.utcnow().isoformat(),
    }


def _mark_step_action_requested(config: dict | None, action: str) -> dict:
    merged = dict(config or {})
    merged["execution_mode"] = "step"
    step_control = dict(merged.get("step_control") or {})
    step_control["requested_action"] = action
    step_control["requested_at"] = datetime.utcnow().isoformat()
    step_control["stop_requested"] = False
    step_control["stop_requested_at"] = None
    step_control["stop_acknowledged_at"] = None
    step_control["stop_reason"] = None
    merged["step_control"] = step_control
    return merged


def _mark_step_stop_requested(config: dict | None) -> dict:
    merged = dict(config or {})
    merged["execution_mode"] = "step"
    step_control = dict(merged.get("step_control") or {})
    step_control["stop_requested"] = True
    step_control["stop_requested_at"] = datetime.utcnow().isoformat()
    step_control["stop_reason"] = "user_requested_stop"
    merged["step_control"] = step_control
    return merged


def _merge_voice_profiles(current_profiles: list, voice_a_id: str | None, voice_b_id: str | None) -> list:
    profiles = [dict(v) if isinstance(v, dict) else {} for v in current_profiles[:2]]
    while len(profiles) < 2:
        profiles.append({})

    if voice_a_id:
        profiles[0]["voice_id"] = voice_a_id
    if voice_b_id:
        profiles[1]["voice_id"] = voice_b_id

    return profiles


def _clone_show_bible_for_run(base: ShowBible, project_id: uuid.UUID, voice_profiles: list) -> ShowBible:
    return ShowBible(
        project_id=project_id,
        audience_profile=base.audience_profile,
        tone_and_mood=base.tone_and_mood,
        show_style=base.show_style,
        hosts=base.hosts,
        voice_profiles=voice_profiles,
        forbidden_phrases=base.forbidden_phrases,
        rigor_policy=base.rigor_policy,
        adjacency_policy=base.adjacency_policy,
        explanation_policy=base.explanation_policy,
        format_rules=base.format_rules,
    )
