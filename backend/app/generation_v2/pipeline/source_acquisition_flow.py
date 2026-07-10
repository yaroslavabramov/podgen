from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_acquisition_agent
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.contracts.pipeline import V2Phase
from app.generation_v2.pipeline.state import TriageState
from app.generation_v2.pipeline.status import start_phase
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import EpisodeCritique, ResearchAudit
from app.generation_v2.source_acquisition import (
    SourceAcquisitionToolset,
    acquisition_enabled,
    build_corpus_gap_report,
    remaining_acquisition_rounds,
    source_acquisition_report_from_rounds,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType

PrepareAndTriage = Callable[[GenerationRun, AsyncSession, V2Runtime], Awaitable[TriageState]]


async def run_source_acquisition_until_triage_ready(
    run: GenerationRun,
    db: AsyncSession,
    runtime: V2Runtime,
    *,
    triage_state: TriageState,
    trigger_phase: str,
    acquisition_rounds_used: int,
    prepare_and_triage: PrepareAndTriage,
    research_audit: ResearchAudit | None = None,
    treatment_gate: EpisodeCritique | None = None,
) -> tuple[TriageState, int]:
    if not acquisition_enabled(run):
        return triage_state, acquisition_rounds_used
    rounds = []
    while remaining_acquisition_rounds(acquisition_rounds_used) > 0:
        round_number = acquisition_rounds_used + 1
        gap_report = build_corpus_gap_report(
            run,
            trigger_phase=trigger_phase,
            round_number=round_number,
            source_map=triage_state.source_map,
            triage_completion_summary=triage_state.triage_completion.summary,
            triage_ready=triage_state.triage_completion.ready,
            triage_gate=triage_state.triage_gate,
            research_audit=research_audit,
            treatment_gate=treatment_gate,
        )
        gap_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.CORPUS_GAP_REPORT,
            artifact_key=f"{trigger_phase}:round-{round_number}",
            payload=gap_report,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=triage_state.source_workspace_artifact_ids,
        )
        await start_phase(run, db, V2Phase.SOURCE_ACQUISITION, f"Acquiring sources for {trigger_phase} gaps", 10)
        toolset = SourceAcquisitionToolset(
            db=db,
            run=run,
            gap_report=gap_report,
            round_number=round_number,
        )
        completion = await build_source_acquisition_agent(
            db,
            run,
            runtime,
            gap_report=gap_report,
            source_inventory=triage_state.source_inventory,
            acquisition_tools=toolset.agent_tools(),
            round_number=round_number,
        )
        round_payload = toolset.round_artifact_payload(
            trigger_phase=trigger_phase,
            gap_report_id=str(gap_artifact.id),
        )
        if completion.summary:
            round_payload.summary = completion.summary
        acquisition_agent_name = f"source_acquisition_round_{round_number}"
        acquisition_model = runtime.model_for("v2_source_acquisition", acquisition_agent_name)
        round_artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_ACQUISITION_ROUND,
            artifact_key=f"{trigger_phase}:round-{round_number}",
            payload=round_payload,
            provider="openai",
            model=acquisition_model,
            status=V2ArtifactStatus.PASSED if round_payload.accepted_count else V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(gap_artifact.id)],
            prompt_hash=runtime.prompt_hash_for("v2_source_acquisition", acquisition_agent_name),
            metadata=runtime.fallback_metadata_for("v2_source_acquisition", acquisition_agent_name),
        )
        rounds.append(round_payload)
        acquisition_rounds_used = round_number
        report = source_acquisition_report_from_rounds(
            enabled=True,
            exhausted=remaining_acquisition_rounds(acquisition_rounds_used) == 0,
            rounds=rounds,
        )
        await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_ACQUISITION_REPORT,
            artifact_key=trigger_phase,
            payload=report,
            provider="openai",
            model=acquisition_model,
            status=V2ArtifactStatus.PASSED if report.total_accepted else V2ArtifactStatus.FAILED,
            input_artifact_ids=[str(gap_artifact.id), str(round_artifact.id)],
        )
        await db.commit()
        if round_payload.accepted_count <= 0:
            break
        triage_state = await prepare_and_triage(run, db, runtime)
        if triage_state.triage_completion.ready and triage_state.triage_gate.passed:
            break
    return triage_state, acquisition_rounds_used
