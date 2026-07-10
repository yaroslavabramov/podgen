from __future__ import annotations

from app.generation_v2.research_workspace_contracts import (
    RESEARCH_ATOM_STORY_WEIGHTS,
    RESEARCH_GAP_CATEGORIES,
    RESEARCH_GAP_SEVERITIES,
    RESEARCH_GAP_STATUSES,
    RESEARCH_GAP_TREATMENT_RELEVANCE,
    RESEARCH_MEMO_RISK_REGISTER_LIMIT,
    RESEARCH_MEMO_SOURCE_COVERAGE_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_tool_spec_types import ToolHandler
from app.generation_v2.runtime import AgentTool


def research_finalizer_tools(
    merge_editorial_atoms_handler: ToolHandler,
    record_research_risk_handler: ToolHandler,
    record_research_gap_handler: ToolHandler,
    finalize_research_memo_handler: ToolHandler,
) -> list[AgentTool]:
    return [
        AgentTool(
            name="merge_editorial_atoms",
            description=(
                "Mandatory finalizer duplicate-audit tool. Merge same-kind atom drafts that repeat "
                "the same research finding before final memo assembly."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "primary_atom_id": {"type": "string"},
                    "duplicate_atom_ids": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["title"]},
                    "claim": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["claim"]},
                    "explanation": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["explanation"]},
                    "story_value": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["story_value"]},
                    "risk": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["risk"]},
                    "source_coverage_status": {"type": "string", "enum": ["ready", "partial", "weak"]},
                    "story_weight": {"type": "string", "enum": sorted(RESEARCH_ATOM_STORY_WEIGHTS)},
                    "treatment_role_hint": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["treatment_role_hint"]},
                },
                "required": ["primary_atom_id", "duplicate_atom_ids"],
            },
            handler=merge_editorial_atoms_handler,
        ),
        AgentTool(
            name="record_research_risk",
            description="Persist a source-coverage, contradiction, or uncertainty risk that should survive memo finalization.",
            parameters={
                "type": "object",
                "properties": {
                    "risk": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["risk"]},
                    "severity": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "atom_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["risk", "severity", "source_refs"],
            },
            handler=record_research_risk_handler,
        ),
        AgentTool(
            name="record_research_gap",
            description="Persist or update a structured research gap before memo finalization. Blocking open/deferred gaps will block the research gate.",
            parameters={
                "type": "object",
                "properties": {
                    "gap_id": {"type": "string"},
                    "question": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["question"]},
                    "category": {"type": "string", "enum": sorted(RESEARCH_GAP_CATEGORIES)},
                    "severity": {"type": "string", "enum": sorted(RESEARCH_GAP_SEVERITIES)},
                    "treatment_relevance": {"type": "string", "enum": sorted(RESEARCH_GAP_TREATMENT_RELEVANCE)},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "atom_refs": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": sorted(RESEARCH_GAP_STATUSES)},
                    "resolution_note": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["resolution_note"]},
                },
                "required": [
                    "question",
                    "category",
                    "severity",
                    "treatment_relevance",
                    "source_refs",
                    "status",
                    "resolution_note",
                ],
            },
            handler=record_research_gap_handler,
        ),
        AgentTool(
            name="finalize_research_memo",
            description=(
                "Persist internal research digest fields. The backend assembles ResearchMemo atoms and "
                "structured risks from durable workspace drafts; thesis is a short supported answer, "
                "research_synthesis is audit/debug context, and risk_register is optional extra cautions only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["title"]},
                    "thesis": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["thesis"]},
                    "research_synthesis": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["research_synthesis"]},
                    "source_coverage": {
                        "type": "array",
                        "maxItems": RESEARCH_MEMO_SOURCE_COVERAGE_LIMIT,
                        "items": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["source_coverage_item"]},
                    },
                    "risk_register": {
                        "type": "array",
                        "maxItems": RESEARCH_MEMO_RISK_REGISTER_LIMIT,
                        "items": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["risk_register_item"]},
                    },
                },
                "required": [
                    "title",
                    "thesis",
                    "research_synthesis",
                    "source_coverage",
                ],
            },
            handler=finalize_research_memo_handler,
        ),
    ]


__all__ = ["research_finalizer_tools"]
