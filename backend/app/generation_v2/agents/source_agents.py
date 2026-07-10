from __future__ import annotations

from app.generation_v2.contracts import V2Phase
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.agents.source_acquisition_agent import build_source_acquisition_agent
from app.generation_v2.agents.fallbacks import allow_runtime_fallback as _allow_runtime_fallback
from app.generation_v2.agents.tool_sets import (
    TRIAGE_FINALIZER_TOOL_NAMES,
    TRIAGE_PLANNER_TOOL_NAMES,
    TRIAGE_SET_TOOL_NAMES,
    phase_tools,
)
from app.generation_v2.prompting import (
    SOURCE_TRIAGE_FINALIZER_PROMPT,
    SOURCE_TRIAGE_PLANNER_PROMPT,
    SOURCE_TRIAGE_SET_PROMPT,
    SOURCE_TRIAGE_SYSTEM,
)
from app.generation_v2.research_workspace import source_map_research_summary
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import (
    SourceMap,
    SourceTriageCompletion,
)
from app.models.generation_run import GenerationRun


async def build_source_triage_planner_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    workspace_summary: dict,
    workspace_tools: list[AgentTool] | None = None,
) -> SourceTriageCompletion:
    prompt = SOURCE_TRIAGE_PLANNER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        workspace_json=_json(workspace_summary),
    )
    agent_name = "source_triage_planner_agent"
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_TRIAGE,
            agent_name=agent_name,
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=phase_tools(workspace_tools, TRIAGE_PLANNER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=6000,
            max_tool_rounds=settings.generation_v2_triage_max_tool_rounds or 80,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_triage", agent_name, exc)
        return SourceTriageCompletion(
            ready=False,
            summary="Runtime fallback was used; source triage planning did not run.",
            notes=[],
        )


async def build_source_triage_set_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_set_payload: dict,
    set_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    hosted_file_search_handler=None,
) -> SourceTriageCompletion:
    prompt = SOURCE_TRIAGE_SET_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        source_set_json=_json(source_set_payload),
    )
    set_id = ((source_set_payload or {}).get("set") or {}).get("set_id") or "unknown"
    agent_name = f"source_triage_set_agent_{str(set_id)[:48]}"
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_TRIAGE,
            agent_name=agent_name,
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_SYSTEM,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=phase_tools(set_tools, TRIAGE_SET_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            hosted_file_search_handler=hosted_file_search_handler,
            max_output_tokens=8000,
            max_tool_rounds=settings.generation_v2_triage_max_tool_rounds or 120,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_triage", agent_name, exc)
        return SourceTriageCompletion(
            ready=False,
            summary=f"Runtime fallback was used; source set {set_id} was not mapped.",
            notes=[],
        )


async def build_source_triage_finalizer_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    workspace_summary: dict,
    source_map: SourceMap,
    source_inventory: SourceMap | None = None,
    finalizer_tools: list[AgentTool] | None = None,
) -> SourceTriageCompletion:
    compact_source_map = source_map_research_summary(
        source_map,
        source_inventory or source_map,
        include_evidence_ids=False,
    )
    prompt = SOURCE_TRIAGE_FINALIZER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        workspace_json=_json(workspace_summary),
        source_map_json=_json(compact_source_map),
    )
    agent_name = "source_triage_finalizer_agent"
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_TRIAGE,
            agent_name=agent_name,
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=phase_tools(finalizer_tools, TRIAGE_FINALIZER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=6000,
            max_tool_rounds=24,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_triage", agent_name, exc)
        return SourceTriageCompletion(
            ready=False,
            summary="Runtime fallback was used; source triage finalizer did not run.",
            notes=[],
        )


def _json(value: object) -> str:
    if isinstance(value, list):
        return json.dumps(
            [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value],
            ensure_ascii=False,
        )
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)

__all__ = [
    "build_source_acquisition_agent",
]
