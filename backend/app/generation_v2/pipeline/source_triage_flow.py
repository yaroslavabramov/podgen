from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.pipeline.source_acquisition_flow import (
    run_source_acquisition_until_triage_ready as _run_source_acquisition_until_triage_ready_impl,
)
from app.generation_v2.pipeline.source_triage_stage import SourceTriageStageRunner
from app.generation_v2.pipeline.state import TriageState
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    EpisodeCritique,
    ResearchAudit,
)
from app.models.generation_run import GenerationRun


async def _prepare_and_triage(run: GenerationRun, db: AsyncSession, runtime: V2Runtime) -> TriageState:
    return await SourceTriageStageRunner(run, db, runtime).run_stage()


async def _triage_prepared_corpus(
    run: GenerationRun,
    db: AsyncSession,
    runtime: V2Runtime,
    *,
    corpus,
) -> TriageState:
    return await SourceTriageStageRunner(run, db, runtime, prepared_corpus=corpus).run_stage()


async def _run_source_acquisition_until_triage_ready(
    run: GenerationRun,
    db: AsyncSession,
    runtime: V2Runtime,
    *,
    triage_state: TriageState,
    trigger_phase: str,
    acquisition_rounds_used: int,
    research_audit: ResearchAudit | None = None,
    treatment_gate: EpisodeCritique | None = None,
) -> tuple[TriageState, int]:
    return await _run_source_acquisition_until_triage_ready_impl(
        run,
        db,
        runtime,
        triage_state=triage_state,
        trigger_phase=trigger_phase,
        acquisition_rounds_used=acquisition_rounds_used,
        research_audit=research_audit,
        treatment_gate=treatment_gate,
        prepare_and_triage=_prepare_and_triage,
    )
