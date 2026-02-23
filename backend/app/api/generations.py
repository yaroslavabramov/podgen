import uuid

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
        select(func.count(Source.id)).where(Source.project_id == project_id, Source.is_temporary == False)
    )).scalar() or 0
    return estimate_cost(data.duration_target, source_count)


@router.post("/projects/{project_id}/generate", response_model=GenerationRunResponse, status_code=status.HTTP_201_CREATED)
async def start_generation(project_id: uuid.UUID, data: GenerationCreate, user: CurrentUser, db: DB):
    project = await _get_user_project(project_id, user.id, db)

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
                ShowBible.is_preset == False,
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
    else:
        result = await db.execute(
            select(ShowBible).where(
                ShowBible.project_id == project_id,
                ShowBible.is_preset == False,
                ShowBible.generation_run_id.is_(None),
            )
        )
        show_bible = result.scalar_one_or_none()
        if show_bible is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No ShowBible configured. Create one first or provide show_bible_override.")
        show_bible_id = show_bible.id

    # Estimate cost
    source_count = (await db.execute(
        select(func.count(Source.id)).where(Source.project_id == project_id, Source.is_temporary == False)
    )).scalar() or 0
    cost = estimate_cost(data.duration_target, source_count)

    run = GenerationRun(
        project_id=project_id,
        user_id=user.id,
        show_bible_id=show_bible_id,
        status=GenerationStatus.QUEUED,
        config={
            "duration_target": data.duration_target,
            "user_prompt": data.user_prompt,
            "language": project.language,
        },
        estimated_cost=cost.model_dump(),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)

    # Link run-scoped ShowBible to this run (if override was used)
    if data.show_bible_override:
        override_result = await db.execute(select(ShowBible).where(ShowBible.id == show_bible_id))
        override_bible = override_result.scalar_one_or_none()
        if override_bible:
            override_bible.generation_run_id = run.id
            await db.flush()

    # Trigger async generation
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

    run.status = GenerationStatus.QUEUED
    await db.flush()
    await db.refresh(run)

    from app.workers.generation_tasks import run_generation_pipeline
    run_generation_pipeline.delay(str(run.id), resume=True)

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
