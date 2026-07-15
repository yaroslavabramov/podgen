from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_acquisition_agent
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.corpus_brief_actions import (
    triage_state_acquisition_requests,
    triage_state_ready_for_research,
)
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
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType

PrepareAndTriage = Callable[[GenerationRun, AsyncSession, V2Runtime], Awaitable[TriageState]]


async def persisted_source_acquisition_rounds_used(
    db: AsyncSession,
    generation_run_id: uuid.UUID,
) -> int:
    """Consume the repair budget only after a durable terminal round result."""
    result = await db.execute(
        select(V2Artifact.id)
        .where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.SOURCE_ACQUISITION_ROUND,
            V2Artifact.status.in_([V2ArtifactStatus.PASSED, V2ArtifactStatus.FAILED]),
        )
        .limit(1)
    )
    return 1 if result.scalar_one_or_none() is not None else 0


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
        _raise_if_explicit_acquisition_remains(
            triage_state,
            reason="source acquisition is disabled for this run",
        )
        return triage_state, acquisition_rounds_used
    if remaining_acquisition_rounds(acquisition_rounds_used) <= 0:
        _raise_if_explicit_acquisition_remains(
            triage_state,
            reason="the source-acquisition repair budget is exhausted",
        )
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
            corpus_brief=getattr(triage_state, "corpus_brief", None),
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
        # Candidate rows are committed before ingest. If the previous worker
        # died mid-call, restore the same three-candidate budget and repeat the
        # unfinished ingest before asking the agent for more candidates.
        await toolset.restore_unfinished_round()
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
        _attach_acquired_sources_to_explicit_selection(
            run,
            round_payload.ingested_source_ids,
        )
        await db.commit()
        if round_payload.accepted_count <= 0:
            break
        triage_state = await prepare_and_triage(run, db, runtime)
        if triage_state_ready_for_research(triage_state):
            break
    _raise_if_explicit_acquisition_remains(
        triage_state,
        reason="the source-acquisition round did not resolve the requested CorpusBrief action",
    )
    return triage_state, acquisition_rounds_used


def _raise_if_explicit_acquisition_remains(
    triage_state: TriageState,
    *,
    reason: str,
) -> None:
    requests = triage_state_acquisition_requests(triage_state)
    if not requests:
        return
    questions = "; ".join(f"{item.question_id}: {item.question}" for item in requests)
    raise RuntimeError(
        "V2 CorpusBrief still requires source acquisition before research, but "
        f"{reason}. Pending questions: {questions}"
    )


def _attach_acquired_sources_to_explicit_selection(
    run: GenerationRun,
    acquired_source_ids: list[str],
) -> None:
    """Keep an explicitly pinned run selection open only for its own repairs.

    Runs without ``source_ids`` intentionally follow all ready project sources.
    Runs with an explicit selection would otherwise promote an acquired source
    successfully and then omit it from the replacement corpus revision.
    """
    config = dict(run.config or {})
    if "source_ids" not in config:
        return
    selected = [str(value) for value in config.get("source_ids") or [] if str(value).strip()]
    for source_id in acquired_source_ids:
        value = str(source_id).strip()
        if value and value not in selected:
            selected.append(value)
    config["source_ids"] = selected
    run.config = config


__all__ = [
    "_attach_acquired_sources_to_explicit_selection",
    "persisted_source_acquisition_rounds_used",
    "run_source_acquisition_until_triage_ready",
]
