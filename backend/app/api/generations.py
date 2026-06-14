import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import not_, select, func, update

from app.api.deps import CurrentUser, DB
from app.models.episode_manifest import EpisodeManifest
from app.models.project import Project
from app.models.source import Source
from app.models.show_bible import ShowBible
from app.models.generation_run import GenerationRun, GenerationStatus
from app.models.script_segment import ScriptSegment
from app.schemas.generation import (
    CostEstimateRequest,
    CostEstimateResponse,
    GenerationCreate,
    GenerationArtifactsResponse,
    GenerationRunResponse,
    V2ArtifactsResponse,
    V2EvidenceResponse,
    V2ToolTracesResponse,
    ProjectCorpusIndexResponse,
    EpisodePlanActArtifact,
    BeatSheetActArtifact,
    BeatSheetSceneArtifact,
    ScreenplayActArtifact,
    ScreenplayLineArtifact,
    ScreenplaySceneArtifact,
    StepRerunRequest,
)
from app.services.cost_estimator import estimate_cost

router = APIRouter(prefix="/api", tags=["generations"])

_STEP_NODE_SEQUENCE = [
    "setup_show",
    "decompose",
    "hunt_closed_book",
    "research_fast_web",
    "build_claims",
    "dedupe_claims",
    "verify_claims",
    "resolve_conflicts",
    "plan_episode",
    "plan_acts",
    "integrate_acts",
    "write_screenplay",
    "evaluate_and_revolt",
    "audit_citations",
    "generate_audio",
    "build_shownotes",
]

_STEP_NODE_INDEX = {node: idx for idx, node in enumerate(_STEP_NODE_SEQUENCE)}


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
    if data.generation_mode in {"v2", "compare"} and data.execution_mode != "full":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Generation V2 and compare mode currently support full execution only.",
        )

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
        "generation_mode": data.generation_mode,
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
        if data.generation_mode == "v2":
            from app.workers.generation_tasks import run_generation_v2

            run_generation_v2.delay(str(run.id), generate_audio=True)
        elif data.generation_mode == "compare":
            from app.workers.generation_tasks import run_generation_compare

            run_generation_compare.delay(str(run.id))
        else:
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


@router.get("/generations/{run_id}/artifacts", response_model=GenerationArtifactsResponse)
async def get_generation_artifacts(run_id: uuid.UUID, user: CurrentUser, db: DB):
    run = await _get_user_run(run_id, user.id, db)
    return await _build_generation_artifacts_response(run, db)


@router.get("/generations/{run_id}/artifacts/v2", response_model=V2ArtifactsResponse)
async def get_generation_v2_artifacts(
    run_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    include_versions: bool = Query(False),
):
    run = await _get_user_run(run_id, user.id, db)
    from app.generation_v2.artifacts import list_artifacts_grouped

    artifacts = await list_artifacts_grouped(db, run.id, include_versions=include_versions)
    config = run.config or {}
    v1_child_id = _uuid_from_config(config.get("compare_v1_run_id"))
    v2_child_id = _uuid_from_config(config.get("compare_v2_run_id"))
    compare_v1_artifacts = None
    compare_v2_artifacts = None

    if v1_child_id:
        v1_child = await _get_user_run(v1_child_id, user.id, db)
        compare_v1_artifacts = await _build_generation_artifacts_response(v1_child, db)
    if v2_child_id:
        v2_child = await _get_user_run(v2_child_id, user.id, db)
        compare_v2_artifacts = await list_artifacts_grouped(db, v2_child.id, include_versions=include_versions)

    return V2ArtifactsResponse(
        run_id=run.id,
        artifacts=artifacts,
        compare_v1_run_id=v1_child_id,
        compare_v2_run_id=v2_child_id,
        compare_v1_artifacts=compare_v1_artifacts,
        compare_v2_artifacts=compare_v2_artifacts,
    )


@router.get("/generations/{run_id}/tool-traces", response_model=V2ToolTracesResponse)
async def get_generation_v2_tool_traces(run_id: uuid.UUID, user: CurrentUser, db: DB):
    run = await _get_user_run(run_id, user.id, db)
    from app.generation_v2.artifacts import list_tool_traces

    return V2ToolTracesResponse(run_id=run.id, traces=await list_tool_traces(db, run.id))


@router.get("/generations/{run_id}/evidence", response_model=V2EvidenceResponse)
async def get_generation_v2_evidence(run_id: uuid.UUID, user: CurrentUser, db: DB):
    run = await _get_user_run(run_id, user.id, db)
    from app.generation_v2.source_corpus import list_evidence_dicts

    config = run.config or {}
    v2_child_id = _uuid_from_config(config.get("compare_v2_run_id"))
    compare_v2_evidence = None
    if v2_child_id:
        v2_child = await _get_user_run(v2_child_id, user.id, db)
        compare_v2_evidence = await list_evidence_dicts(db, v2_child.id)
    return V2EvidenceResponse(
        run_id=run.id,
        evidence=await list_evidence_dicts(db, run.id),
        compare_v2_run_id=v2_child_id,
        compare_v2_evidence=compare_v2_evidence,
    )


@router.get("/generations/{run_id}/evidence/{evidence_id}", response_model=dict)
async def get_generation_v2_evidence_item(run_id: uuid.UUID, evidence_id: uuid.UUID, user: CurrentUser, db: DB):
    run = await _get_user_run(run_id, user.id, db)
    from app.generation_v2.source_corpus import get_evidence_dict

    item = await get_evidence_dict(db, run.id, evidence_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence item not found")
    return item


@router.get("/projects/{project_id}/corpus-index", response_model=ProjectCorpusIndexResponse)
async def get_project_corpus_index(project_id: uuid.UUID, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    from app.generation_v2.source_corpus import project_corpus_index_summary

    return await project_corpus_index_summary(db, project_id)


@router.post("/projects/{project_id}/corpus-index/sync", response_model=ProjectCorpusIndexResponse)
async def sync_project_corpus_index(project_id: uuid.UUID, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    from app.generation_v2.source_corpus import sync_project_corpus_index_now, project_corpus_index_summary

    await sync_project_corpus_index_now(db, project_id)
    return await project_corpus_index_summary(db, project_id)


@router.post("/generations/{run_id}/promote-audio", response_model=GenerationRunResponse)
async def promote_generation_audio(run_id: uuid.UUID, user: CurrentUser, db: DB, variant: Literal["v1", "v2"] = "v2"):
    run = await _get_user_run(run_id, user.id, db)
    target_run = run
    if (run.config or {}).get("generation_mode") == "compare":
        target_id = _uuid_from_config((run.config or {}).get(f"compare_{variant}_run_id"))
        if not target_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Compare run has no {variant.upper()} child to promote yet.",
            )
        target_run = await _get_user_run(target_id, user.id, db)

    if target_run.status != GenerationStatus.COMPLETED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only completed draft runs can be promoted to audio.")
    segment_count = (
        await db.execute(select(func.count(ScriptSegment.id)).where(ScriptSegment.generation_run_id == target_run.id))
    ).scalar() or 0
    if segment_count <= 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Selected variant has no script segments for audio.")

    target_run.status = GenerationStatus.QUEUED
    target_run.error_message = None
    target_run.completed_at = None
    target_run.config = {**(target_run.config or {}), "audio_promotion_requested": True}
    await db.flush()
    await db.refresh(target_run)

    from app.workers.generation_tasks import promote_generation_variant_to_audio

    promote_generation_variant_to_audio.delay(str(target_run.id))
    return GenerationRunResponse.model_validate(target_run)


async def _build_generation_artifacts_response(run: GenerationRun, db: DB) -> GenerationArtifactsResponse:

    manifest_result = await db.execute(
        select(EpisodeManifest).where(EpisodeManifest.generation_run_id == run.id)
    )
    manifest = manifest_result.scalar_one_or_none()

    segment_result = await db.execute(
        select(ScriptSegment)
        .where(ScriptSegment.generation_run_id == run.id)
        .order_by(ScriptSegment.act_number.asc(), ScriptSegment.scene_number.asc())
    )
    segments = segment_result.scalars().all()

    acts_json = manifest.acts_json if manifest is not None and isinstance(manifest.acts_json, list) else []
    show_episode_plan = _artifact_available_for_run(run, required_node="plan_episode")
    show_beat_sheets = _artifact_available_for_run(run, required_node="plan_acts")
    show_screenplay = _artifact_available_for_run(run, required_node="write_screenplay")
    return GenerationArtifactsResponse(
        run_id=run.id,
        episode_plan=_build_episode_plan_artifacts(acts_json) if show_episode_plan else [],
        beat_sheets=_build_beat_sheet_artifacts(acts_json) if show_beat_sheets else [],
        screenplay=_build_screenplay_artifacts(segments) if show_screenplay else [],
    )


@router.get("/projects/{project_id}/generations/latest", response_model=GenerationRunResponse)
async def get_latest_project_generation(project_id: uuid.UUID, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    active_result = await db.execute(
        select(GenerationRun)
        .where(
            GenerationRun.project_id == project_id,
            _not_compare_child(),
            GenerationRun.status.in_([
                GenerationStatus.QUEUED,
                GenerationStatus.RUNNING,
            ]),
        )
        .order_by(GenerationRun.created_at.desc())
        .limit(1)
    )
    run = active_result.scalar_one_or_none()
    if run is None:
        failed_result = await db.execute(
            select(GenerationRun)
            .where(
                GenerationRun.project_id == project_id,
                _not_compare_child(),
                GenerationRun.status == GenerationStatus.FAILED,
            )
            .order_by(GenerationRun.created_at.desc())
            .limit(1)
        )
        run = failed_result.scalar_one_or_none()
    if run is None:
        completed_compare_result = await db.execute(
            select(GenerationRun)
            .where(
                GenerationRun.project_id == project_id,
                _is_compare_coordinator(),
                GenerationRun.status == GenerationStatus.COMPLETED,
            )
            .order_by(GenerationRun.created_at.desc())
            .limit(1)
        )
        run = completed_compare_result.scalar_one_or_none()
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

    if (run.config or {}).get("execution_mode") == "step":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use step controls for runs started in step mode.",
        )

    if run.status == GenerationStatus.RUNNING:
        if bool((run.config or {}).get("stop_requested")):
            return GenerationRunResponse.model_validate(run)
        run.error_message = None
        run.config = _mark_run_stop_requested(run.config)
        await db.flush()
        await db.refresh(run)
        return GenerationRunResponse.model_validate(run)

    cancelled_at = datetime.utcnow()
    queued_cancel = await db.execute(
        update(GenerationRun)
        .where(
            GenerationRun.id == run.id,
            GenerationRun.status == GenerationStatus.QUEUED,
        )
        .values(
            status=GenerationStatus.CANCELLED,
            error_message=None,
            completed_at=cancelled_at,
            config=_mark_run_stop_requested(run.config),
        )
    )
    if (queued_cancel.rowcount or 0) == 0:
        result = await db.execute(
            select(GenerationRun)
            .join(Project, GenerationRun.project_id == Project.id)
            .where(GenerationRun.id == run_id, Project.user_id == user.id)
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation run not found")
        if run.status == GenerationStatus.RUNNING:
            if not bool((run.config or {}).get("stop_requested")):
                run.error_message = None
                run.config = _mark_run_stop_requested(run.config)
                await db.flush()
                await db.refresh(run)
            return GenerationRunResponse.model_validate(run)
        if run.status == GenerationStatus.CANCELLED:
            return GenerationRunResponse.model_validate(run)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot cancel a completed run")

    await db.flush()
    await db.refresh(run)

    from app.realtime.publisher import publish_event
    from app.realtime.events import generation_cancelled

    publish_event(str(run.id), generation_cancelled("Generation cancelled before start."))
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
    generation_mode = (run.config or {}).get("generation_mode", "v1")
    if generation_mode == "v1" and not run.langgraph_thread_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resume unavailable for this run (missing checkpoint thread). Start a new generation run.",
        )

    run.status = GenerationStatus.QUEUED
    run.error_message = None
    run.completed_at = None
    await db.flush()
    await db.refresh(run)

    if generation_mode == "v2":
        from app.workers.generation_tasks import run_generation_v2

        run_generation_v2.delay(str(run.id), generate_audio=not bool((run.config or {}).get("compare_draft_only")))
    elif generation_mode == "compare":
        from app.workers.generation_tasks import run_generation_compare

        run_generation_compare.delay(str(run.id))
    else:
        from app.workers.generation_tasks import run_generation_pipeline

        run_generation_pipeline.delay(str(run.id), resume=True)

    return GenerationRunResponse.model_validate(run)


@router.post("/generations/{run_id}/steps/next", response_model=GenerationRunResponse)
async def run_next_generation_step(run_id: uuid.UUID, user: CurrentUser, db: DB):
    return await _enqueue_step_action(run_id, user.id, db, action="next")


@router.post("/generations/{run_id}/steps/rerun", response_model=GenerationRunResponse)
async def rerun_generation_step(
    run_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    data: StepRerunRequest | None = None,
):
    return await _enqueue_step_action(
        run_id,
        user.id,
        db,
        action="rerun",
        target_node=(data.target_node if data else None),
    )


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


async def _enqueue_step_action(
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    db: DB,
    action: str,
    target_node: str | None = None,
) -> GenerationRunResponse:
    run = await _get_user_run(run_id, user_id, db)
    step_control = dict(((run.config or {}).get("step_control") or {}))
    requested_target_node = target_node.strip() if isinstance(target_node, str) and target_node.strip() else None

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
        available_rerun_nodes = [
            node for node in (step_control.get("available_rerun_nodes") or [])
            if isinstance(node, str) and node
        ]
        if requested_target_node and available_rerun_nodes and requested_target_node not in available_rerun_nodes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected step is not available for rerun on this run.",
            )
    if run.status == GenerationStatus.COMPLETED and action != "rerun":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Generation run already completed. Use rerun to re-execute the last step.",
        )

    run.status = GenerationStatus.QUEUED
    run.error_message = None
    run.completed_at = None
    run.config = _mark_step_action_requested(run.config, action, requested_target_node)
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
        "available_rerun_nodes": [],
        "checkpoint_id": None,
        "is_complete": False,
        "can_rerun": False,
        "requested_action": None,
        "requested_target_node": None,
        "requested_at": None,
        "stop_requested": False,
        "stop_requested_at": None,
        "stop_acknowledged_at": None,
        "stop_reason": None,
        "updated_at": datetime.utcnow().isoformat(),
    }


def _uuid_from_config(value: object) -> uuid.UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _not_compare_child():
    return not_(GenerationRun.config.has_key("compare_parent_run_id"))  # noqa: W601


def _is_compare_coordinator():
    return _not_compare_child() & (GenerationRun.config["generation_mode"].as_string() == "compare")


def _mark_step_action_requested(config: dict | None, action: str, target_node: str | None = None) -> dict:
    merged = dict(config or {})
    merged["execution_mode"] = "step"
    step_control = dict(merged.get("step_control") or {})
    step_control["requested_action"] = action
    step_control["requested_target_node"] = target_node
    step_control["requested_at"] = datetime.utcnow().isoformat()
    step_control["stop_requested"] = False
    step_control["stop_requested_at"] = None
    step_control["stop_acknowledged_at"] = None
    step_control["stop_reason"] = None
    merged["step_control"] = step_control
    return merged


def _build_episode_plan_artifacts(acts_json: list[dict]) -> list[EpisodePlanActArtifact]:
    artifacts: list[EpisodePlanActArtifact] = []
    for idx, act in enumerate(acts_json, start=1):
        if not isinstance(act, dict):
            continue
        artifacts.append(
            EpisodePlanActArtifact(
                act_number=_safe_int(act.get("act_number"), idx),
                story_role=_clean_optional_str(act.get("story_role")),
                act_title=_clean_optional_str(act.get("act_title")),
                purpose=_clean_optional_str(act.get("purpose")),
                internal_arc=_clean_optional_str(act.get("internal_arc")),
                opening_question=_clean_optional_str(act.get("opening_question")),
                ending_turn=_clean_optional_str(act.get("ending_turn")),
            )
        )
    return artifacts


def _build_beat_sheet_artifacts(acts_json: list[dict]) -> list[BeatSheetActArtifact]:
    artifacts: list[BeatSheetActArtifact] = []
    for idx, act in enumerate(acts_json, start=1):
        if not isinstance(act, dict):
            continue
        beat_sheet = act.get("beat_sheet")
        if not isinstance(beat_sheet, list):
            continue
        scenes: list[BeatSheetSceneArtifact] = []
        for scene_idx, scene in enumerate(beat_sheet, start=1):
            if not isinstance(scene, dict):
                continue
            scenes.append(
                BeatSheetSceneArtifact(
                    scene_number=_safe_int(scene.get("scene"), scene_idx),
                    scene_type=_clean_optional_str(scene.get("scene_type")),
                    scene_job=_clean_optional_str(scene.get("scene_job")),
                    description=_clean_optional_str(scene.get("description")),
                    transition_note=_clean_optional_str(scene.get("transition_note")),
                )
            )
        artifacts.append(
            BeatSheetActArtifact(
                act_number=_safe_int(act.get("act_number"), idx),
                story_role=_clean_optional_str(act.get("story_role")),
                act_title=_clean_optional_str(act.get("act_title")),
                scenes=scenes,
            )
        )
    return artifacts


def _build_screenplay_artifacts(segments: list[ScriptSegment]) -> list[ScreenplayActArtifact]:
    acts: dict[int, list[ScreenplaySceneArtifact]] = {}
    for segment in segments:
        if not isinstance(segment.dialogue_json, list):
            continue
        dialogue = [
            ScreenplayLineArtifact(
                speaker=str(item.get("speaker") or "").strip(),
                text=str(item.get("text") or "").strip(),
            )
            for item in segment.dialogue_json
            if isinstance(item, dict)
            and str(item.get("speaker") or "").strip()
            and str(item.get("text") or "").strip()
        ]
        acts.setdefault(segment.act_number, []).append(
            ScreenplaySceneArtifact(scene_number=segment.scene_number, dialogue=dialogue)
        )

    return [
        ScreenplayActArtifact(
            act_number=act_number,
            scenes=sorted(scenes, key=lambda item: item.scene_number),
        )
        for act_number, scenes in sorted(acts.items())
    ]


def _clean_optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _artifact_available_for_run(run: GenerationRun, *, required_node: str) -> bool:
    config = getattr(run, "config", None) or {}
    if config.get("execution_mode") != "step":
        return True
    step_control = config.get("step_control") or {}
    last_node = step_control.get("last_node")
    if not isinstance(last_node, str) or last_node not in _STEP_NODE_INDEX:
        return False
    effective_completed_index = _STEP_NODE_INDEX[last_node]
    requested_action = step_control.get("requested_action")
    requested_target_node = step_control.get("requested_target_node")
    if requested_action == "rerun":
        rerun_target = (
            requested_target_node
            if isinstance(requested_target_node, str) and requested_target_node in _STEP_NODE_INDEX
            else last_node
        )
        effective_completed_index = min(
            effective_completed_index,
            _STEP_NODE_INDEX[rerun_target] - 1,
        )
    return effective_completed_index >= _STEP_NODE_INDEX[required_node]


def _mark_run_stop_requested(config: dict | None) -> dict:
    merged = dict(config or {})
    merged["stop_requested"] = True
    merged["stop_requested_at"] = datetime.utcnow().isoformat()
    merged["stop_acknowledged_at"] = None
    merged["stop_reason"] = "user_requested_stop"
    return merged


def _ack_run_stop_requested(config: dict | None, reason: str) -> dict:
    merged = dict(config or {})
    merged["stop_requested"] = False
    merged["stop_acknowledged_at"] = datetime.utcnow().isoformat()
    merged["stop_reason"] = reason
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
