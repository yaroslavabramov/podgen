from __future__ import annotations

from dataclasses import replace
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
    SOURCE_TRIAGE_FINALIZER_SYSTEM,
    SOURCE_TRIAGE_PLANNER_PROMPT,
    SOURCE_TRIAGE_PLANNER_SYSTEM,
    SOURCE_TRIAGE_SET_PROMPT,
    SOURCE_TRIAGE_SET_SYSTEM,
)
from app.generation_v2.contracts import V2Phase, V2ToolName
from app.generation_v2.research_workspace import source_map_research_summary
from app.generation_v2.runtime import (
    AgentCheckpointPolicy,
    AgentTool,
    ToolPolicy,
    V2Runtime,
    V2RuntimeUnavailable,
)
from app.generation_v2.runtime_checkpoint_context import (
    TRIAGE_FINALIZER_RESUME_CONTRACT,
    TRIAGE_PLANNER_RESUME_CONTRACT,
    TRIAGE_SET_RESUME_CONTRACT,
    build_checkpoint_resume_context,
)
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
    source_catalog: dict,
    workspace_tools: list[AgentTool] | None = None,
) -> SourceTriageCompletion:
    output_language = _output_language(run, workspace_summary)
    prompt = SOURCE_TRIAGE_PLANNER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        output_language=_json(output_language),
        workspace_json=_json(workspace_summary),
        source_catalog_json=_json(source_catalog),
    )
    agent_name = "source_triage_planner_agent"
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_TRIAGE,
            agent_name=agent_name,
            schema_model=SourceTriageCompletion,
            system=SOURCE_TRIAGE_PLANNER_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_triage_phase_tools(workspace_tools, TRIAGE_PLANNER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=None,
            max_tool_rounds=0,
            server_compaction_threshold=settings.generation_v2_triage_compaction_threshold,
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            checkpoint_resume_context=build_checkpoint_resume_context(
                run_config=_run_config_with_language(run, output_language),
                workflow_contract=TRIAGE_PLANNER_RESUME_CONTRACT,
            ),
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
    output_language: str | None = None,
    terminal_progress_loader=None,
) -> SourceTriageCompletion:
    resolved_output_language = _output_language(run, explicit=output_language)
    prompt = SOURCE_TRIAGE_SET_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        output_language=_json(resolved_output_language),
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
            system=SOURCE_TRIAGE_SET_SYSTEM,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=_triage_phase_tools(set_tools, TRIAGE_SET_TOOL_NAMES),
            tool_policy=ToolPolicy(
                file_search=False,
                web_search=False,
                local_tools=True,
                parallel_local_tool_calls=True,
            ),
            hosted_file_search_handler=hosted_file_search_handler,
            max_output_tokens=None,
            max_tool_rounds=0,
            server_compaction_threshold=settings.generation_v2_triage_compaction_threshold,
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            checkpoint_resume_context=build_checkpoint_resume_context(
                run_config=_run_config_with_language(run, resolved_output_language),
                workflow_contract=TRIAGE_SET_RESUME_CONTRACT,
            ),
            terminal_progress_loader=terminal_progress_loader,
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
    output_language = _output_language(run, workspace_summary)
    compact_source_map = source_map_research_summary(
        source_map,
        source_inventory or source_map,
        include_evidence_ids=False,
    )
    # Keep the general finalizer preview compact, but expose exact evidence
    # handles for relationships because conflict adjudication may need to read
    # those saved passages rather than trust a summary.
    for payload, relationship in zip(
        compact_source_map.get("relationships") or [],
        source_map.source_triage.source_relationships,
        strict=False,
    ):
        payload["evidence_ids"] = list(relationship.evidence_ids)
        payload["evidence_id_omitted_count"] = 0
    for payload, cluster in zip(
        compact_source_map.get("clusters") or [],
        source_map.source_triage.source_clusters,
        strict=False,
    ):
        payload["evidence_ids"] = list(cluster.evidence_ids)
        payload["evidence_id_omitted_count"] = 0
    for payload, gap in zip(
        compact_source_map.get("gap_records") or [],
        source_map.gap_records,
        strict=False,
    ):
        payload["evidence_ids"] = list(gap.evidence_ids)
        payload["evidence_id_omitted_count"] = 0
    prompt = SOURCE_TRIAGE_FINALIZER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        output_language=_json(output_language),
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
            system=SOURCE_TRIAGE_FINALIZER_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_triage_phase_tools(finalizer_tools, TRIAGE_FINALIZER_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=None,
            max_tool_rounds=0,
            server_compaction_threshold=settings.generation_v2_triage_compaction_threshold,
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            checkpoint_resume_context=build_checkpoint_resume_context(
                run_config=_run_config_with_language(run, output_language),
                workflow_contract=TRIAGE_FINALIZER_RESUME_CONTRACT,
            ),
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


def _triage_phase_tools(
    tools: list[AgentTool] | None,
    allowed_names: set[str],
) -> list[AgentTool]:
    """Ensure every model-visible triage rejection names a valid recovery action."""

    guided: list[AgentTool] = []
    for tool in phase_tools(tools, allowed_names):
        original_handler = tool.handler

        async def handler(args: dict, *, _handler=original_handler, _name=tool.name) -> dict:
            output = await _handler(args)
            if not isinstance(output, dict) or not output.get("error") or output.get("next_action"):
                return output
            return {
                **output,
                "next_action": _triage_error_next_action(
                    _name,
                    available_tool_names=allowed_names,
                ),
            }

        guided.append(replace(tool, handler=handler))
    return guided


def _triage_error_next_action(
    tool_name: str,
    *,
    available_tool_names: set[str],
) -> str:
    if tool_name in {
        "list_source_capsules",
        "list_triage_source_sets",
    }:
        return f"Correct the returned ref, section, or cursor and retry {tool_name}."
    if tool_name in {
        V2ToolName.READ_SOURCE,
        V2ToolName.SEARCH_SOURCE,
        V2ToolName.OPEN_SOURCE_FILE,
        V2ToolName.READ_EVIDENCE,
        V2ToolName.READ_SOURCE_WINDOW,
    }:
        if V2ToolName.SAVE_SOURCE_ANALYSIS in available_tool_names:
            return (
                f"Correct the returned source handle, cursor, or locator and retry {tool_name}; if the checked "
                "representation is genuinely unavailable, preserve that exact limitation with save_source_analysis."
            )
        return (
            f"Correct the returned evidence handle or locator and retry {tool_name}; if exact material remains "
            "unavailable, preserve that limitation in the affected finding, outcome, relationship, or gap."
        )
    if tool_name == V2ToolName.COMPARE_SOURCE_EVIDENCE:
        return (
            "Correct the source refs or comparison question and retry compare_source_evidence, then update only "
            "the finding links, relations, outcomes, relationships, clusters, or gaps supported by the result."
        )
    if tool_name == "finish_triage_source_set":
        return "Complete the listed source analyses or outcomes, then retry finish_triage_source_set."
    if tool_name == "finalize_triage_workspace_plan":
        return "Correct the listed questions or source sets, then retry finalize_triage_workspace_plan."
    if tool_name == "finalize_source_map":
        return "Correct the listed finding links, outcomes, syntheses, or map records, then retry finalize_source_map."
    return f"Correct the returned defect and retry {tool_name}."


def _output_language(
    run: GenerationRun,
    durable_context: dict | None = None,
    *,
    explicit: str | None = None,
) -> str:
    durable_language = str(((durable_context or {}).get("intent") or {}).get("language") or "").strip()
    return str(explicit or durable_language or (run.config or {}).get("language") or "en").strip() or "en"


def _run_config_with_language(run: GenerationRun, language: str) -> dict:
    return {**(run.config or {}), "language": language}

__all__ = [
    "build_source_acquisition_agent",
]
