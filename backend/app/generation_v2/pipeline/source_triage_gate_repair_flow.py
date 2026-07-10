from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_triage_finalizer_agent
from app.generation_v2.artifacts import save_artifact, save_tool_trace
from app.generation_v2.pipeline.source_triage_set_flow import run_source_triage_set as _run_source_triage_set
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion, SourceTriageGate
from app.generation_v2.source_triage_workspace import (
    create_repair_sets_from_gate,
    load_source_triage_workspace,
    save_source_triage_workspace,
    unprocessed_source_sets,
    unresolved_source_sets,
    workspace_summary_for_prompt,
)
from app.generation_v2.tools.evidence import EvidenceToolset, build_source_map_from_write_actions, validate_source_map_gate
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType
from app.pipeline.step_control import assert_step_not_stopped


async def run_source_triage_gate_repair(
    *,
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    corpus: Any,
    evidence_toolset: EvidenceToolset,
    source_inventory: SourceMap,
    source_map: SourceMap,
    workspace: dict[str, Any],
    triage_gate: SourceTriageGate,
    triage_completion: SourceTriageCompletion,
    triage_completion_artifact_id: str,
    finalizer_tools: list[Any],
    unresolved_completion_handler: Any,
    materialize_deferrals_handler: Any,
) -> tuple[dict[str, Any], SourceMap, SourceTriageGate, SourceTriageCompletion]:
    await _save_pre_repair_gate_artifacts(
        db=db,
        run=run,
        source_map=source_map,
        triage_gate=triage_gate,
        triage_completion_artifact_id=triage_completion_artifact_id,
    )
    workspace = await load_source_triage_workspace(db, run.id) or workspace
    workspace = create_repair_sets_from_gate(workspace, gate=triage_gate, inventory=source_inventory, source_map=source_map)
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    repair_completions: list[SourceTriageCompletion] = []
    for repair_set in [item for item in unprocessed_source_sets(workspace) if item.get("repair")][:4]:
        await assert_step_not_stopped()
        repair_completion = await _run_source_triage_set(
            run=run,
            db=db,
            runtime=runtime,
            corpus=corpus,
            source_inventory=source_inventory,
            workspace=workspace,
            source_set=repair_set,
        )
        repair_completions.append(repair_completion)
        workspace = await load_source_triage_workspace(db, run.id) or workspace
    workspace = await materialize_deferrals_handler(db=db, run=run, workspace=workspace)
    source_map = await build_source_map_from_write_actions(
        db,
        generation_run_id=run.id,
        inventory=source_inventory,
    )
    unresolved_sets = unresolved_source_sets(workspace)
    if unresolved_sets:
        finalizer_completion = await unresolved_completion_handler(db=db, run=run, workspace=workspace)
    else:
        finalizer_completion = await build_source_triage_finalizer_agent(
            db,
            run,
            runtime,
            workspace_summary=workspace_summary_for_prompt(workspace),
            source_map=source_map,
            source_inventory=source_inventory,
            finalizer_tools=finalizer_tools,
        )
    source_map = await build_source_map_from_write_actions(
        db,
        generation_run_id=run.id,
        inventory=source_inventory,
    )
    triage_gate = await validate_source_map_gate(
        db,
        generation_run_id=run.id,
        source_map=source_map,
        run_view=corpus.run_view,
        triage_completion=finalizer_completion,
    )
    triage_completion = SourceTriageCompletion(
        ready=finalizer_completion.ready,
        summary=f"{triage_completion.summary}\nTargeted repair pass: {finalizer_completion.summary}",
        notes=[
            *triage_completion.notes,
            "Targeted source triage repair sets executed after gate failure.",
            *[completion.summary for completion in repair_completions if completion.summary],
            *finalizer_completion.notes,
        ],
    )
    return workspace, source_map, triage_gate, triage_completion


async def _save_pre_repair_gate_artifacts(
    *,
    db: AsyncSession,
    run: GenerationRun,
    source_map: SourceMap,
    triage_gate: SourceTriageGate,
    triage_completion_artifact_id: str,
) -> None:
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="gate_pre_repair",
        payload=triage_gate,
        status=V2ArtifactStatus.FAILED,
        input_artifact_ids=[triage_completion_artifact_id],
    )
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_TRIAGE,
        agent_name=V2AgentName.SOURCE_TRIAGE_GATE,
        tool_name=V2ToolName.TRIAGE_TARGETED_REPAIR_STARTED,
        input_json={
            "gate": triage_gate.model_dump(mode="json"),
            "source_card_count": len(source_map.source_cards),
            "quote_card_count": len(source_map.quote_cards),
            "cluster_count": len(source_map.source_triage.source_clusters),
            "relationship_count": len(source_map.source_triage.source_relationships),
        },
        output_json={"targeted_repair": True},
    )


__all__ = ["run_source_triage_gate_repair"]
