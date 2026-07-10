from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.schemas import SourceTriageCompletion
from app.generation_v2.source_triage_workspace import (
    load_source_triage_workspace,
    save_source_triage_workspace,
    unresolved_source_set_summary,
    unresolved_source_sets,
)
from app.models.generation_run import GenerationRun


async def source_triage_unresolved_completion(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
) -> SourceTriageCompletion:
    unresolved = unresolved_source_set_summary(workspace)
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_TRIAGE,
        agent_name=V2AgentName.SOURCE_TRIAGE_WORKFLOW,
        tool_name=V2ToolName.SOURCE_SETS_UNRESOLVED,
        input_json={"unresolved_source_sets": unresolved},
        output_json={"accepted": False, "unresolved_set_count": len(unresolved_source_sets(workspace))},
    )
    return SourceTriageCompletion(
        ready=False,
        summary="Source triage workflow has unresolved source sets; finalization is blocked.",
        notes=[f"{item['set_id']}:{item['status']}:{','.join(item.get('source_refs') or [])}" for item in unresolved],
    )


async def materialize_workspace_deferrals_as_gap_traces(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    materialized = {str(item) for item in workspace.get("materialized_deferred_source_refs") or []}
    deferred = [
        dict(item)
        for item in workspace.get("deferred_sources") or []
        if item.get("source_ref") and str(item.get("source_ref")) not in materialized
    ]
    if not deferred:
        return workspace
    for item in deferred:
        source_ref = str(item.get("source_ref"))
        reason = str(item.get("reason") or "Deferred by source triage workflow.")
        await save_tool_trace(
            db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_TRIAGE,
            agent_name=V2AgentName.SOURCE_TRIAGE_WORKSPACE,
            tool_name=V2ToolName.RECORD_GAP,
            input_json={
                "kind": "coverage",
                "description": f"Source {source_ref} deferred during source triage: {reason}",
                "source_refs": [source_ref],
                "evidence_ids": [],
            },
            output_json={"accepted": True, "materialized_workspace_deferral": True},
        )
        materialized.add(source_ref)
    workspace["materialized_deferred_source_refs"] = sorted(materialized)
    return await save_and_reload_workspace(db=db, run=run, workspace=workspace)


async def save_and_reload_workspace(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    return await load_source_triage_workspace(db, run.id) or workspace


def combined_triage_completion(
    planner_completion: SourceTriageCompletion,
    set_completions: list[SourceTriageCompletion],
    finalizer_completion: SourceTriageCompletion,
) -> SourceTriageCompletion:
    notes: list[str] = []
    if planner_completion.summary:
        notes.append(f"Planner: {planner_completion.summary}")
    for idx, completion in enumerate(set_completions, start=1):
        if completion.summary:
            notes.append(f"Set {idx}: {completion.summary}")
    notes.extend(finalizer_completion.notes)
    return SourceTriageCompletion(
        ready=finalizer_completion.ready,
        summary=finalizer_completion.summary,
        notes=notes[:60],
    )


__all__ = [
    "combined_triage_completion",
    "materialize_workspace_deferrals_as_gap_traces",
    "save_and_reload_workspace",
    "source_triage_unresolved_completion",
]
