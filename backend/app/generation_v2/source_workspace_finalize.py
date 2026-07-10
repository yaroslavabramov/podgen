from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from app.generation_v2.artifacts import save_tool_trace
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import CorpusWorkspace, V2RecordStatus


async def fail_source_workspace(
    db,
    *,
    run: GenerationRun,
    workspace: CorpusWorkspace,
    source_count: int,
    ready_source_count: int,
    failed_source_count: int,
    ready_representation_count: int,
    failed_representation_count: int,
    workspace_warnings: list[str],
) -> None:
    workspace.status = V2RecordStatus.FAILED
    workspace.config = _workspace_count_config(
        workspace,
        ready_source_count=ready_source_count,
        failed_source_count=failed_source_count,
        ready_representation_count=ready_representation_count,
        failed_representation_count=failed_representation_count,
        workspace_warnings=workspace_warnings,
    )
    await db.flush()
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_WORKSPACE,
        agent_name=V2AgentName.SOURCE_WORKSPACE,
        tool_name=V2ToolName.BUILD_SOURCE_WORKSPACE,
        input_json={"source_count": source_count},
        output_json={
            "workspace_id": str(workspace.id),
            "status": "failed",
            "ready_source_count": ready_source_count,
            "failed_source_count": failed_source_count,
            "ready_representation_count": ready_representation_count,
            "failed_representation_count": failed_representation_count,
            "workspace_warnings": workspace_warnings,
        },
    )


async def mark_source_workspace_ready(
    db,
    *,
    run: GenerationRun,
    workspace: CorpusWorkspace,
    source_count: int,
    source_card_count: int,
    quote_card_count: int,
    vector_store_id: str | None,
    ready_source_count: int,
    failed_source_count: int,
    ready_representation_count: int,
    failed_representation_count: int,
    workspace_warnings: list[str],
) -> None:
    workspace.status = V2RecordStatus.READY
    workspace.config = _workspace_count_config(
        workspace,
        ready_source_count=ready_source_count,
        failed_source_count=failed_source_count,
        ready_representation_count=ready_representation_count,
        failed_representation_count=failed_representation_count,
        workspace_warnings=workspace_warnings,
    )
    await db.flush()
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_WORKSPACE,
        agent_name=V2AgentName.SOURCE_WORKSPACE,
        tool_name=V2ToolName.BUILD_SOURCE_WORKSPACE,
        input_json={"source_count": source_count},
        output_json={
            "workspace_id": str(workspace.id),
            "source_cards": source_card_count,
            "quote_cards": quote_card_count,
            "vector_store_id": vector_store_id,
            "ready_source_count": ready_source_count,
            "failed_source_count": failed_source_count,
            "ready_representation_count": ready_representation_count,
            "failed_representation_count": failed_representation_count,
            "workspace_warnings": workspace_warnings,
        },
    )


def _workspace_count_config(
    workspace: CorpusWorkspace,
    *,
    ready_source_count: int,
    failed_source_count: int,
    ready_representation_count: int,
    failed_representation_count: int,
    workspace_warnings: list[str],
) -> dict:
    return {
        **(workspace.config or {}),
        "workspace_warnings": workspace_warnings,
        "ready_source_count": ready_source_count,
        "failed_source_count": failed_source_count,
        "ready_representation_count": ready_representation_count,
        "failed_representation_count": failed_representation_count,
    }


__all__ = ["fail_source_workspace", "mark_source_workspace_ready"]
