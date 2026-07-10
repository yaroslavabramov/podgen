from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.contracts.pipeline import V2PipelineStep
from app.generation_v2.pipeline import orchestrator_compat as _orchestrator_compat
from app.generation_v2.pipeline.source_triage_flow import (
    _prepare_and_triage,
    _run_source_acquisition_until_triage_ready,
)
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
from app.models.show_bible import ShowBible


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
    acquisition_rounds_used = 0

    triage_state = await _prepare_and_triage(run, db, runtime)
    if not triage_state.triage_completion.ready or not triage_state.triage_gate.passed:
        triage_state, acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
            run,
            db,
            runtime,
            triage_state=triage_state,
            trigger_phase="source_triage",
            acquisition_rounds_used=acquisition_rounds_used,
        )
    if not triage_state.triage_completion.ready or not triage_state.triage_gate.passed:
        await db.commit()
        raise RuntimeError(
            f"V2 source triage gate failed: {triage_state.triage_completion.summary} {triage_state.triage_gate.summary}"
        )
    await commit_step(run, db, V2PipelineStep.CORPUS_PREPARE, 12, next_step=V2PipelineStep.EDITORIAL_RESEARCH)

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
