from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.research_workspace_tool_specs import (
    research_finalizer_tools,
    research_job_tools,
    research_planner_tools,
)
from app.generation_v2.research_workspace_atom_tools import upsert_editorial_atom_handler
from app.generation_v2.research_workspace_finalizer_tools import research_finalizer_tool_handlers
from app.generation_v2.research_workspace_gap_tools import record_research_gap_handler
from app.generation_v2.research_workspace_job_tools import (
    finish_research_job_handler,
    record_research_observation_handler,
)
from app.generation_v2.research_workspace_planner_tools import research_planner_tool_handlers
from app.generation_v2.research_workspace_risk_tools import record_research_risk_handler
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceMap
from app.models.generation_run import GenerationRun


def build_research_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    source_inventory: SourceMap,
    evidence_toolset: Any | None = None,
    mode: str,
    current_job_id: str | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = []

    if mode == "planner":
        upsert_research_job_tool, finalize_research_plan_tool = research_planner_tool_handlers(
            db=db,
            run=run,
            workspace=workspace,
        )
        tools.extend(research_planner_tools(upsert_research_job_tool, finalize_research_plan_tool))
    record_research_risk_tool = record_research_risk_handler(
        db=db,
        run=run,
        workspace=workspace,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    record_research_gap_tool = record_research_gap_handler(
        db=db,
        run=run,
        workspace=workspace,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    finish_research_job_tool = finish_research_job_handler(
        db=db,
        run=run,
        workspace=workspace,
        current_job_id=current_job_id,
    )
    record_research_observation_tool = record_research_observation_handler(
        db=db,
        run=run,
        workspace=workspace,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    upsert_editorial_atom_tool = upsert_editorial_atom_handler(
        db=db,
        run=run,
        workspace=workspace,
        source_inventory=source_inventory,
        evidence_toolset=evidence_toolset,
        current_job_id=current_job_id,
    )
    merge_editorial_atoms_tool, finalize_research_memo_tool = research_finalizer_tool_handlers(
        db=db,
        run=run,
        workspace=workspace,
    )
    if mode == "job":
        tools.extend(
            research_job_tools(
                record_research_observation_tool,
                upsert_editorial_atom_tool,
                finish_research_job_tool,
                record_research_risk_tool,
                record_research_gap_tool,
            )
        )
    if mode == "finalizer":
        tools.extend(
            research_finalizer_tools(
                merge_editorial_atoms_tool,
                record_research_risk_tool,
                record_research_gap_tool,
                finalize_research_memo_tool,
            )
        )
    return tools
