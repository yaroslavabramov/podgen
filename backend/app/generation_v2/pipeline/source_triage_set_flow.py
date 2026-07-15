from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_triage_set_agent
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion
from app.generation_v2.source_corpus import CorpusPreparationResult
from app.generation_v2.source_triage_workspace import (
    build_workspace_tools,
    load_source_triage_workspace,
    save_source_triage_workspace,
    source_set_prompt_payload,
    start_triage_source_set,
)
from app.generation_v2.tools.evidence import EvidenceToolset
from app.models.generation_run import GenerationRun
from app.pipeline.step_control import assert_step_not_stopped


async def run_source_triage_set(
    *,
    run: GenerationRun,
    db: AsyncSession,
    runtime: Any,
    corpus: CorpusPreparationResult,
    source_inventory: SourceMap,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> SourceTriageCompletion:
    set_id = str(source_set.get("set_id") or "")
    if not set_id:
        return SourceTriageCompletion(ready=False, summary="Source triage set is missing set_id.", notes=[])
    workspace = await start_triage_source_set(
        db,
        run=run,
        workspace=workspace,
        set_id=set_id,
        inventory=source_inventory,
        evidence_toolset=EvidenceToolset(
            db=db,
            run=run,
            run_view=corpus.run_view,
            provider_index=corpus.provider_index,
        ),
    )
    current_set = next((item for item in workspace.get("source_sets") or [] if item.get("set_id") == set_id), source_set)
    allowed_refs = {str(ref) for ref in current_set.get("source_refs") or [] if str(ref).strip()}
    repair_write_refs = (
        {str(ref) for ref in current_set.get("repair_source_refs") or [] if str(ref).strip()}
        if current_set.get("repair_targeted")
        else None
    )
    scoped_toolset = EvidenceToolset(
        db=db,
        run=run,
        run_view=corpus.run_view,
        provider_index=corpus.provider_index,
        triage_allowed_source_refs=allowed_refs,
        triage_write_allowed_source_refs=repair_write_refs,
        triage_current_set_id=set_id,
    )
    set_tools = [
        *scoped_toolset.agent_tools(),
        *build_workspace_tools(
            db=db,
            run=run,
            workspace=workspace,
            inventory=source_inventory,
            evidence_toolset=scoped_toolset,
            mode="set",
            current_set_id=set_id,
        ),
    ]
    completion = await build_source_triage_set_agent(
        db,
        run,
        runtime,
        source_set_payload=source_set_prompt_payload(workspace, set_id),
        set_tools=set_tools,
        vector_store_id=corpus.vector_store_id,
        hosted_file_search_handler=scoped_toolset.persist_hosted_file_search_calls,
    )
    latest = await load_source_triage_workspace(db, run.id) or workspace
    finished_set = next((item for item in latest.get("source_sets") or [] if item.get("set_id") == set_id), None)
    if finished_set is None or finished_set.get("status") == "in_progress":
        await mark_source_triage_set_unfinished(
            db=db,
            run=run,
            workspace=latest,
            set_id=set_id,
            reason="Set agent returned before calling finish_triage_source_set.",
        )
        return SourceTriageCompletion(
            ready=False,
            summary=f"Source triage set {set_id} was not finished by the agent.",
            notes=[completion.summary] if completion.summary else [],
        )
    return completion


async def mark_source_triage_set_unfinished(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    set_id: str,
    reason: str,
) -> None:
    updated_sets: list[dict[str, Any]] = []
    for item in workspace.get("source_sets") or []:
        source_set = dict(item)
        if source_set.get("set_id") == set_id:
            source_set["status"] = "needs_repair"
            source_set["repair"] = True
            source_set["deferred_reason"] = reason
            source_set["findings_summary"] = source_set.get("findings_summary") or reason
        updated_sets.append(source_set)
    workspace["source_sets"] = updated_sets
    workspace["current_set"] = None
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    from app.generation_v2.artifacts import save_tool_trace

    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_TRIAGE,
        agent_name=V2AgentName.SOURCE_TRIAGE_SET_WORKFLOW,
        tool_name=V2ToolName.SOURCE_SET_UNFINISHED,
        input_json={"set_id": set_id},
        output_json={"accepted": False, "error": reason},
    )



__all__ = [
    "assert_step_not_stopped",
]
