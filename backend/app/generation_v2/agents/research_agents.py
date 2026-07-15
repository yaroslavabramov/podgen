from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agent_output_policy import semantic_agent_output_limit
from app.generation_v2.agents.json_helpers import json_for_prompt as _json
from app.generation_v2.agents.tool_sets import (
    RESEARCH_FINALIZER_TOOL_NAMES,
    RESEARCH_JOB_TOOL_NAMES,
    RESEARCH_PLANNER_TOOL_NAMES,
    phase_tools,
)
from app.generation_v2.prompting import (
    RESEARCH_FINALIZER_PROMPT,
    RESEARCH_FINALIZER_SYSTEM,
    RESEARCH_JOB_PROMPT,
    RESEARCH_JOB_SYSTEM,
    RESEARCH_PLANNER_PROMPT,
    RESEARCH_PLANNER_SYSTEM,
)
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime
from app.generation_v2.runtime_checkpoint_context import (
    RESEARCH_FINALIZER_RESUME_CONTRACT,
    RESEARCH_JOB_RESUME_CONTRACT,
    RESEARCH_PLANNER_RESUME_CONTRACT,
    build_checkpoint_resume_context,
)
from app.generation_v2.schemas import ResearchWorkflowCompletion, ShowBibleContext
from app.models.generation_run import GenerationRun


def noop_research_workspace_tools() -> list[AgentTool]:
    async def _handler(_args):
        return {"accepted": True}

    terminal_names = {
        "finalize_research_plan",
        "finish_research_job",
        "finalize_research_memo",
    }
    return [
        AgentTool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            terminal=name in terminal_names,
            terminal_result_builder=(
                lambda _arguments, _output: (
                    ResearchWorkflowCompletion(
                        ready=True,
                        summary="Ephemeral research step persisted.",
                        notes=[],
                    )
                    if name in terminal_names
                    else None
                )
            ),
        )
        for name in [
            "merge_editorial_atoms",
            "upsert_research_job",
            "finalize_research_plan",
            "record_research_observation",
            "upsert_editorial_atom",
            "finish_research_job",
            "record_research_risk",
            "record_research_gap",
            "finalize_research_memo",
        ]
    ]


async def build_research_planner_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    workspace_summary: dict,
    source_map_summary: dict,
    workspace_tools: list[AgentTool] | None = None,
    show_bible: ShowBibleContext | None = None,
) -> ResearchWorkflowCompletion:
    prompt = RESEARCH_PLANNER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        source_map_summary_json=_json(source_map_summary),
        workspace_json=_json(workspace_summary),
    )
    return await runtime.call_structured(
        db=db,
        generation_run_id=run.id,
        phase=V2Phase.EDITORIAL_RESEARCH,
        agent_name=V2AgentName.RESEARCH_PLANNER,
        schema_model=ResearchWorkflowCompletion,
        system=RESEARCH_PLANNER_SYSTEM,
        prompt=prompt,
        vector_store_id=None,
        local_tools=phase_tools(workspace_tools, RESEARCH_PLANNER_TOOL_NAMES),
        tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
        max_output_tokens=semantic_agent_output_limit(),
        max_tool_rounds=40,
        checkpoint_resume_context=build_checkpoint_resume_context(
            run_config=run.config or {},
            workflow_contract=RESEARCH_PLANNER_RESUME_CONTRACT,
            show_bible=show_bible or ShowBibleContext(),
        ),
    )


async def build_research_job_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    job_payload: dict,
    job_tools: list[AgentTool] | None = None,
    hosted_file_search_handler=None,
) -> ResearchWorkflowCompletion:
    job_id = ((job_payload or {}).get("job") or {}).get("job_id") or "unknown"
    prompt = RESEARCH_JOB_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        job_payload_json=_json(job_payload),
    )
    return await runtime.call_structured(
        db=db,
        generation_run_id=run.id,
        phase=V2Phase.EDITORIAL_RESEARCH,
        agent_name=f"research_job_agent_{str(job_id)[:48]}",
        schema_model=ResearchWorkflowCompletion,
        system=RESEARCH_JOB_SYSTEM,
        prompt=prompt,
        vector_store_id=None,
        local_tools=phase_tools(job_tools, RESEARCH_JOB_TOOL_NAMES),
        tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
        hosted_file_search_handler=hosted_file_search_handler,
        max_output_tokens=semantic_agent_output_limit(),
        max_tool_rounds=80,
        checkpoint_resume_context=build_checkpoint_resume_context(
            run_config=run.config or {},
            workflow_contract=RESEARCH_JOB_RESUME_CONTRACT,
        ),
    )


async def build_research_finalizer_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    workspace_summary: dict,
    finalizer_tools: list[AgentTool] | None = None,
    show_bible: ShowBibleContext | None = None,
) -> ResearchWorkflowCompletion:
    prompt = RESEARCH_FINALIZER_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        workspace_json=_json(workspace_summary),
    )
    return await runtime.call_structured(
        db=db,
        generation_run_id=run.id,
        phase=V2Phase.EDITORIAL_RESEARCH,
        agent_name=V2AgentName.RESEARCH_FINALIZER,
        schema_model=ResearchWorkflowCompletion,
        system=RESEARCH_FINALIZER_SYSTEM,
        prompt=prompt,
        vector_store_id=None,
        local_tools=phase_tools(finalizer_tools, RESEARCH_FINALIZER_TOOL_NAMES),
        tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
        max_output_tokens=semantic_agent_output_limit(),
        max_tool_rounds=24,
        checkpoint_resume_context=build_checkpoint_resume_context(
            run_config=run.config or {},
            workflow_contract=RESEARCH_FINALIZER_RESUME_CONTRACT,
            show_bible=show_bible or ShowBibleContext(),
        ),
    )
