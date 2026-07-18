from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.contracts.pipeline import V2PipelineStep
from app.generation_v2.corpus_brief_actions import (
    triage_state_acquisition_requests,
    triage_state_ready_for_research,
)
from app.generation_v2.pipeline import orchestrator_compat as _orchestrator_compat
from app.generation_v2.pipeline.source_triage_flow import (
    _run_source_acquisition_until_triage_ready,
    _triage_prepared_corpus,
)
from app.generation_v2.pipeline.source_acquisition_flow import persisted_source_acquisition_rounds_used
from app.generation_v2.pipeline.triage_state_loader import load_passed_triage_state
from app.generation_v2.pipeline.status import (
    V2StepExecutionStopped,
    commit_step,
)
from app.generation_v2.pipeline.drafting_flow import draft_treatment_acts
from app.generation_v2.pipeline.editorial_research_flow import run_editorial_research_until_treatment
from app.generation_v2.pipeline.post_drafting_flow import run_post_drafting_until_final
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ShowBibleContext,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import RunCorpusView
from app.models.show_bible import ShowBible
from app.generation_v2.source_corpus import (
    CorpusPreparationResult,
    load_current_v2_corpus,
    prepare_v2_corpus,
)
from app.generation_v2.source_corpus_run_view import (
    current_attempt_has_triage_history,
    load_current_run_corpus_view,
)
from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_state import WORKSPACE_SCHEMA_VERSION


__all__ = ["V2StepExecutionStopped", "execute_generation_v2"]


def __getattr__(name: str):
    if name in _orchestrator_compat.__all__:
        return getattr(_orchestrator_compat, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def execute_generation_v2(
    run: GenerationRun,
    db: AsyncSession,
    *,
    generate_audio: bool = True,
) -> str | None:
    runtime = V2Runtime()
    show_bible = await _load_show_bible_context(db, run)
    acquisition_rounds_used = await persisted_source_acquisition_rounds_used(db, run.id)

    config = run.config or {}
    step_mode = config.get("execution_mode") == "step"
    requested_step = str(config.get("v2_stop_after_step") or "") if step_mode else ""

    if not step_mode:
        corpus = await _load_or_prepare_triage_corpus(db, run)
        await commit_step(
            run,
            db,
            V2PipelineStep.CORPUS_PREPARE,
            6,
            next_step=V2PipelineStep.SOURCE_TRIAGE,
        )
    elif requested_step == V2PipelineStep.CORPUS_PREPARE:
        corpus = await prepare_v2_corpus(db, run)
        await commit_step(
            run,
            db,
            V2PipelineStep.CORPUS_PREPARE,
            6,
            next_step=V2PipelineStep.SOURCE_TRIAGE,
        )
    else:
        current_view = (
            await load_current_run_corpus_view(db, run.id)
            if requested_step == V2PipelineStep.SOURCE_TRIAGE
            else None
        )
        restart_triage = bool(
            current_view is not None
            and (
                _explicit_source_selection_changed(run, current_view)
                or await _current_triage_scope_requires_new_attempt(db, run, current_view)
            )
        )
        if restart_triage:
            # Inspect the lightweight view before hydrating its pinned versions.
            # A retry can legitimately replace versions that made the old view
            # unreadable; that history must not prevent creation of a clean scope.
            corpus = await prepare_v2_corpus(db, run)
        else:
            corpus = await load_current_v2_corpus(db, run)

    if not step_mode or requested_step == V2PipelineStep.SOURCE_TRIAGE:
        triage_state = await _triage_prepared_corpus(run, db, runtime, corpus=corpus)
        if not triage_state_ready_for_research(triage_state) and (
            triage_state_acquisition_requests(triage_state)
            or getattr(triage_state, "corpus_brief", None) is None
        ):
            triage_state, acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
                run,
                db,
                runtime,
                triage_state=triage_state,
                trigger_phase="source_triage",
                acquisition_rounds_used=acquisition_rounds_used,
            )
        if not triage_state_ready_for_research(triage_state):
            await db.commit()
            raise RuntimeError(
                f"V2 source triage gate failed: {triage_state.triage_completion.summary} "
                f"{triage_state.triage_gate.summary}"
            )
        await commit_step(
            run,
            db,
            V2PipelineStep.SOURCE_TRIAGE,
            12,
            next_step=V2PipelineStep.EDITORIAL_RESEARCH,
        )
    else:
        triage_state = await load_passed_triage_state(db, run)
        if triage_state_acquisition_requests(triage_state):
            triage_state, acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
                run,
                db,
                runtime,
                triage_state=triage_state,
                trigger_phase="source_triage",
                acquisition_rounds_used=acquisition_rounds_used,
            )
        if not triage_state_ready_for_research(triage_state):
            raise RuntimeError("V2 source triage is not ready for editorial research")

    editorial_result = await run_editorial_research_until_treatment(
        db,
        run,
        runtime,
        triage_state=triage_state,
        acquisition_rounds_used=acquisition_rounds_used,
        show_bible=show_bible,
    )
    triage_state = editorial_result.triage_state
    corpus = editorial_result.corpus
    evidence_toolset = editorial_result.evidence_toolset
    evidence_tools = editorial_result.evidence_tools
    quote_cards = editorial_result.quote_cards
    source_triage = editorial_result.source_triage
    source_artifact_ids = editorial_result.source_artifact_ids
    quote_artifact_ids = editorial_result.quote_artifact_ids
    run_source_cards = editorial_result.run_source_cards
    treatment = editorial_result.treatment
    treatment_artifact_id = editorial_result.treatment_artifact_id

    drafting_result = await draft_treatment_acts(
        db,
        run,
        runtime,
        treatment=treatment,
        treatment_artifact_id=treatment_artifact_id,
        triage_artifact_id=triage_state.triage_artifact_id,
        source_artifact_ids=source_artifact_ids,
        quote_artifact_ids=quote_artifact_ids,
        source_cards=run_source_cards,
        quote_cards=quote_cards,
        source_triage=source_triage,
        evidence_tools=evidence_tools,
        vector_store_id=corpus.vector_store_id,
        show_bible=show_bible,
    )
    drafts = drafting_result.drafts
    dossiers = drafting_result.dossiers
    dossier_artifact_ids_by_act = drafting_result.dossier_artifact_ids_by_act
    draft_artifact_ids_by_act = drafting_result.draft_artifact_ids_by_act

    return await run_post_drafting_until_final(
        db,
        run,
        runtime,
        treatment=treatment,
        drafts=drafts,
        dossiers=dossiers,
        dossier_artifact_ids_by_act=dossier_artifact_ids_by_act,
        draft_artifact_ids_by_act=draft_artifact_ids_by_act,
        treatment_artifact_id=treatment_artifact_id,
        source_artifact_ids=source_artifact_ids,
        quote_artifact_ids=quote_artifact_ids,
        source_cards=run_source_cards,
        quote_cards=quote_cards,
        evidence_toolset=evidence_toolset,
        evidence_tools=evidence_tools,
        vector_store_id=corpus.vector_store_id,
        run_view=corpus.run_view,
        show_bible=show_bible,
        generate_audio=generate_audio,
    )


def _explicit_source_selection_changed(run: GenerationRun, run_view: RunCorpusView) -> bool:
    configured_ids = {
        str(value)
        for value in (run.config or {}).get("source_ids", [])
        if str(value).strip()
    }
    if not configured_ids:
        return False
    pinned_ids = {str(value) for value in (run_view.selected_source_ids or []) if str(value).strip()}
    return configured_ids != pinned_ids


async def _load_or_prepare_triage_corpus(
    db: AsyncSession,
    run: GenerationRun,
) -> CorpusPreparationResult:
    current_view = await load_current_run_corpus_view(db, run.id)
    if current_view is None:
        return await prepare_v2_corpus(db, run)
    if _explicit_source_selection_changed(run, current_view):
        return await prepare_v2_corpus(db, run)
    if await _current_triage_scope_requires_new_attempt(db, run, current_view):
        return await prepare_v2_corpus(db, run)
    return await load_current_v2_corpus(db, run)


async def _current_triage_scope_requires_new_attempt(
    db: AsyncSession,
    run: GenerationRun,
    run_view: RunCorpusView,
) -> bool:
    if not await current_attempt_has_triage_history(db, run_view):
        return False
    workspace = await load_source_triage_workspace(db, run.id)
    if not workspace:
        return True
    same_scope = (
        int(workspace.get("corpus_revision") or 1) == int(run_view.revision or 1)
        and int(workspace.get("attempt") or 1) == int(run_view.attempt or 1)
    )
    return not (
        same_scope
        and str(workspace.get("schema_version") or "") == WORKSPACE_SCHEMA_VERSION
    )


async def _load_show_bible_context(db: AsyncSession, run: GenerationRun) -> ShowBibleContext:
    if not run.show_bible_id:
        return ShowBibleContext()
    result = await db.execute(select(ShowBible).where(ShowBible.id == run.show_bible_id))
    bible = result.scalar_one_or_none()
    if bible is None:
        return ShowBibleContext()
    return ShowBibleContext(
        show_style=bible.show_style,
        tone_and_mood=bible.tone_and_mood,
        hosts=bible.hosts or [],
        forbidden_phrases=bible.forbidden_phrases or [],
        rigor_policy=bible.rigor_policy,
        adjacency_policy=bible.adjacency_policy,
        explanation_policy=bible.explanation_policy,
        format_rules=bible.format_rules or {},
    )
