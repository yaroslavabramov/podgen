from __future__ import annotations

from app.generation_v2.research_kinds import ALLOWED_RESEARCH_ATOM_KINDS
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_QUESTION_LIMIT,
    RESEARCH_REQUIRED_ATOM_KIND_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_tool_spec_types import ToolHandler
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import ResearchWorkflowCompletion


def research_planner_tools(
    upsert_research_job_handler: ToolHandler,
    finalize_research_plan_handler: ToolHandler,
    *,
    terminal_resume_loader=None,
) -> list[AgentTool]:
    return [
        AgentTool(
            name="upsert_research_job",
            description="Persist one investigation job. The planner uses this to split SourceMap questions into durable research work.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "label": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["label"]},
                    "story_job": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["story_job"]},
                    "purpose": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["purpose"]},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "questions": {
                        "type": "array",
                        "maxItems": RESEARCH_QUESTION_LIMIT,
                        "items": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["question"]},
                    },
                    "required_atom_kinds": {
                        "type": "array",
                        "maxItems": RESEARCH_REQUIRED_ATOM_KIND_LIMIT,
                        "items": {"type": "string", "enum": sorted(ALLOWED_RESEARCH_ATOM_KINDS)},
                    },
                    "priority": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["priority"]},
                    "allow_broad_search": {"type": "boolean"},
                },
                "required": [
                    "label",
                    "story_job",
                    "purpose",
                    "source_refs",
                    "questions",
                    "required_atom_kinds",
                    "priority",
                    "allow_broad_search",
                ],
            },
            handler=upsert_research_job_handler,
        ),
        AgentTool(
            name="finalize_research_plan",
            description="Mark the durable research plan complete after investigation jobs have been written.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["plan_summary"]},
                },
                "required": ["summary"],
            },
            handler=finalize_research_plan_handler,
            terminal=True,
            terminal_result_builder=lambda arguments, _output: ResearchWorkflowCompletion(
                ready=True,
                summary=str(arguments.get("summary") or "Research plan persisted."),
                notes=[],
            ),
            terminal_resume_loader=terminal_resume_loader,
        ),
    ]


__all__ = ["research_planner_tools"]
