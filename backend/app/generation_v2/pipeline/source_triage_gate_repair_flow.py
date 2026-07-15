from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_triage_finalizer_agent
from app.generation_v2.artifacts import save_artifact, save_tool_trace
from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from app.generation_v2.pipeline.source_triage_set_flow import run_source_triage_set as _run_source_triage_set
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion, SourceTriageGate
from app.generation_v2.source_triage_workspace import (
    create_repair_sets_from_gate,
    load_source_triage_workspace,
    save_source_triage_workspace,
    unprocessed_source_sets,
    unresolved_source_sets,
    unresolved_source_refs,
    workspace_summary_for_prompt,
)
from app.generation_v2.source_triage_repair_state import (
    TRIAGE_REPAIR_COMPLETED,
    complete_triage_repair,
    pending_triage_repair,
    prepare_pending_triage_repair,
    reserve_triage_repair,
    start_triage_repair,
    triage_repair_operation,
)
from app.generation_v2.source_triage_canonical import materialize_source_map_v2
from app.generation_v2.tools.evidence import (
    EvidenceToolset,
    build_source_map_from_write_actions,
    validate_source_map_gate,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType
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
    workspace = await load_source_triage_workspace(db, run.id) or workspace
    operation = pending_triage_repair(workspace)
    if operation is None:
        existing_operation = triage_repair_operation(workspace)
        if existing_operation is not None and existing_operation.get("status") == TRIAGE_REPAIR_COMPLETED:
            raise RuntimeError("Source-triage repair pass was already completed.")
        workspace = create_repair_sets_from_gate(
            workspace,
            gate=triage_gate,
            inventory=source_inventory,
            source_map=source_map,
        )
        unaddressable = [
            item for item in workspace.get("repair_unaddressable_violations") or [] if isinstance(item, dict)
        ]
        if unaddressable:
            codes = ", ".join(sorted({str(item.get("code") or "unknown") for item in unaddressable}))
            raise RuntimeError(
                "Source-triage validation requires replanning or a source-map rewrite; "
                f"an existing-set repair cannot safely resolve: {codes}."
            )
        is_v2_workspace = str(workspace.get("schema_version") or "") == "source_triage_workspace.v2"
        repair_sets = [
            dict(item)
            for item in workspace.get("source_sets") or []
            if isinstance(item, dict) and item.get("repair") and (not is_v2_workspace or item.get("repair_targeted"))
        ]
        operation = reserve_triage_repair(
            workspace,
            gate_payload=triage_gate.model_dump(mode="json"),
            completion_payload=triage_completion.model_dump(mode="json"),
            repair_sets=repair_sets,
        )
        if operation is None:
            raise RuntimeError("Source-triage repair pass could not be reserved.")
        await _save_pre_repair_gate_artifacts(
            db=db,
            run=run,
            source_map=source_map,
            triage_gate=triage_gate,
            triage_completion_artifact_id=triage_completion_artifact_id,
        )
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
        # The reservation, its repair sets, and the marker become durable together.
        await db.commit()
    else:
        payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
        stored_completion = payload.get("completion")
        if isinstance(stored_completion, dict):
            triage_completion = SourceTriageCompletion.model_validate(stored_completion)
    workspace = prepare_pending_triage_repair(workspace)
    operation = start_triage_repair(workspace) or operation
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    await db.commit()
    repair_completions: list[SourceTriageCompletion] = []
    operation_payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    repair_set_ids = {str(item) for item in operation_payload.get("repair_set_ids") or [] if str(item).strip()}
    for repair_set in [
        item for item in unprocessed_source_sets(workspace) if str(item.get("set_id") or "") in repair_set_ids
    ]:
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
    workspace = await load_source_triage_workspace(db, run.id) or workspace
    source_map = await build_source_map_from_write_actions(
        db,
        generation_run_id=run.id,
        inventory=source_inventory,
        run_view=corpus.run_view,
    )
    source_map = materialize_source_map_v2(
        source_map,
        workspace=workspace,
        inventory=source_inventory,
    )
    unresolved_sets = unresolved_source_sets(workspace)
    if unresolved_sets or unresolved_source_refs(workspace):
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
    workspace = await load_source_triage_workspace(db, run.id) or workspace
    source_map = await build_source_map_from_write_actions(
        db,
        generation_run_id=run.id,
        inventory=source_inventory,
        run_view=corpus.run_view,
    )
    source_map = materialize_source_map_v2(
        source_map,
        workspace=workspace,
        inventory=source_inventory,
    )
    triage_gate = await validate_source_map_gate(
        db,
        generation_run_id=run.id,
        source_map=source_map,
        run_view=corpus.run_view,
        triage_completion=finalizer_completion,
        triage_workspace=workspace,
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
    complete_triage_repair(workspace)
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    await db.commit()
    return workspace, source_map, triage_gate, triage_completion


async def source_triage_repair_already_used(db: AsyncSession, generation_run_id: uuid.UUID) -> bool:
    workspace = await load_source_triage_workspace(db, generation_run_id)
    operation = triage_repair_operation(workspace or {})
    if operation is not None:
        return operation.get("status") == TRIAGE_REPAIR_COMPLETED
    result = await db.execute(
        select(V2Artifact.id)
        .where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.SOURCE_TRIAGE,
            V2Artifact.artifact_key == "gate_pre_repair",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


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


__all__ = ["run_source_triage_gate_repair", "source_triage_repair_already_used"]
