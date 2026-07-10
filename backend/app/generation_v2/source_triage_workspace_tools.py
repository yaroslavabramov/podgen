from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as _workspace
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_tool_handlers import SourceTriageWorkspaceToolHandlers
from app.generation_v2.source_triage_workspace_tool_specs import (
    source_triage_finalizer_tools,
    source_triage_planner_tools,
    source_triage_set_tools,
)
from app.models.generation_run import GenerationRun


async def load_source_triage_workspace(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.load_source_triage_workspace(*args, **kwargs)


async def save_source_triage_workspace(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.save_source_triage_workspace(*args, **kwargs)


async def ensure_capsules_for_refs(*args: Any, **kwargs: Any) -> Any:
    return await _workspace.ensure_capsules_for_refs(*args, **kwargs)


def _metadata_only_capsule(*args: Any, **kwargs: Any) -> Any:
    return _workspace._metadata_only_capsule(*args, **kwargs)


def _capsule_page_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspace._capsule_page_payload(*args, **kwargs)


def _dedupe(*args: Any, **kwargs: Any) -> Any:
    return _workspace._dedupe(*args, **kwargs)


def _stable_set_id(*args: Any, **kwargs: Any) -> Any:
    return _workspace._stable_set_id(*args, **kwargs)


def _merge_deferred(*args: Any, **kwargs: Any) -> Any:
    return _workspace._merge_deferred(*args, **kwargs)


def _workspace_set(*args: Any, **kwargs: Any) -> Any:
    return _workspace._workspace_set(*args, **kwargs)


def _replace_workspace_set(*args: Any, **kwargs: Any) -> Any:
    return _workspace._replace_workspace_set(*args, **kwargs)


def _now(*args: Any, **kwargs: Any) -> Any:
    return _workspace._now(*args, **kwargs)


def build_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    inventory: SourceMap,
    evidence_toolset: Any | None = None,
    mode: str,
    current_set_id: str | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = []
    handlers = SourceTriageWorkspaceToolHandlers(
        db=db,
        run=run,
        workspace=workspace,
        inventory=inventory,
        evidence_toolset=evidence_toolset,
        current_set_id=current_set_id,
    )

    if mode == "planner":
        tools.extend(
            source_triage_planner_tools(
                handlers.list_source_capsules_tool,
                handlers.upsert_triage_source_set_tool,
                handlers.finalize_triage_workspace_plan_tool,
            )
        )
    if mode == "set":
        tools.extend(source_triage_set_tools(handlers.record_triage_observation_tool, handlers.finish_triage_source_set_tool))
    if mode == "finalizer":
        tools.extend(source_triage_finalizer_tools(handlers.finish_triage_source_set_tool))
    return tools
