from __future__ import annotations

from app.generation_v2.research_kinds import ALLOWED_RESEARCH_ATOM_KINDS
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_ANCHOR_REF_LIMIT,
    RESEARCH_ATOM_STORY_WEIGHTS,
    RESEARCH_GAP_CATEGORIES,
    RESEARCH_GAP_SEVERITIES,
    RESEARCH_GAP_STATUSES,
    RESEARCH_GAP_TREATMENT_RELEVANCE,
    RESEARCH_PENDING_QUESTION_LIMIT,
    RESEARCH_QUOTE_REF_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_tool_spec_types import ToolHandler
from app.generation_v2.runtime import AgentTool


def research_job_tools(
    record_research_observation_handler: ToolHandler,
    upsert_editorial_atom_handler: ToolHandler,
    finish_research_job_handler: ToolHandler,
    record_research_risk_handler: ToolHandler,
    record_research_gap_handler: ToolHandler,
) -> list[AgentTool]:
    return [
        AgentTool(
            name="record_research_observation",
            description="Persist an intermediate research finding for the current job before reading more or crossing a memory boundary.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                    "pending_questions": {
                        "type": "array",
                        "maxItems": RESEARCH_PENDING_QUESTION_LIMIT,
                        "items": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["pending_question"]},
                    },
                },
                "required": ["job_id", "source_refs", "evidence_ids", "note", "pending_questions"],
            },
            handler=record_research_observation_handler,
        ),
        AgentTool(
            name="upsert_editorial_atom",
            description="Persist one source-grounded editorial atom draft. Included evidence_ids are selected automatically for research lineage.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "atom_id": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(ALLOWED_RESEARCH_ATOM_KINDS)},
                    "title": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["title"]},
                    "claim": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["claim"]},
                    "explanation": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["explanation"]},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "quote_refs": {
                        "type": "array",
                        "maxItems": RESEARCH_QUOTE_REF_LIMIT,
                        "items": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["quote_ref"]},
                    },
                    "story_value": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["story_value"]},
                    "anchor_refs": {
                        "type": "array",
                        "maxItems": RESEARCH_ANCHOR_REF_LIMIT,
                        "items": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["anchor_ref"]},
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "risk": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["risk"]},
                    "source_coverage_status": {"type": "string", "enum": ["ready", "partial", "weak"]},
                    "story_weight": {"type": "string", "enum": sorted(RESEARCH_ATOM_STORY_WEIGHTS)},
                    "treatment_role_hint": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["treatment_role_hint"]},
                },
                "required": [
                    "job_id",
                    "kind",
                    "title",
                    "claim",
                    "explanation",
                    "source_refs",
                    "evidence_ids",
                    "quote_refs",
                    "story_value",
                    "anchor_refs",
                    "confidence",
                    "risk",
                    "source_coverage_status",
                    "story_weight",
                    "treatment_role_hint",
                ],
            },
            handler=upsert_editorial_atom_handler,
        ),
        AgentTool(
            name="finish_research_job",
            description="Finish the current investigation job after persisting observations or atom drafts.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["mapped", "deferred"]},
                    "summary": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["job_summary"]},
                    "deferred_reason": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["deferred_reason"]},
                },
                "required": ["job_id", "status", "summary", "deferred_reason"],
            },
            handler=finish_research_job_handler,
        ),
        AgentTool(
            name="record_research_risk",
            description="Persist a non-blocking source-quality, corroboration, contradiction, or uncertainty risk. Use this instead of an editorial atom for source-quality warnings.",
            parameters={
                "type": "object",
                "properties": {
                    "risk": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["risk"]},
                    "severity": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["risk", "severity", "source_refs", "evidence_ids"],
            },
            handler=record_research_risk_handler,
        ),
        AgentTool(
            name="record_research_gap",
            description="Persist a structured research gap. Use severity=blocking only when treatment should not proceed without repair.",
            parameters={
                "type": "object",
                "properties": {
                    "gap_id": {"type": "string"},
                    "question": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["question"]},
                    "category": {"type": "string", "enum": sorted(RESEARCH_GAP_CATEGORIES)},
                    "severity": {"type": "string", "enum": sorted(RESEARCH_GAP_SEVERITIES)},
                    "treatment_relevance": {"type": "string", "enum": sorted(RESEARCH_GAP_TREATMENT_RELEVANCE)},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": sorted(RESEARCH_GAP_STATUSES)},
                    "resolution_note": {"type": "string", "maxLength": SEMANTIC_FIELD_LIMITS["resolution_note"]},
                },
                "required": [
                    "question",
                    "category",
                    "severity",
                    "treatment_relevance",
                    "source_refs",
                    "evidence_ids",
                    "status",
                    "resolution_note",
                ],
            },
            handler=record_research_gap_handler,
        ),
    ]


__all__ = ["research_job_tools"]
