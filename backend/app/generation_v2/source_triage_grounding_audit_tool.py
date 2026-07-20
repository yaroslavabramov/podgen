from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)


ToolHandler = Callable[[dict[str, Any]], Any]


def source_finding_grounding_audit_tool(handler: ToolHandler) -> AgentTool:
    return AgentTool(
        name="audit_source_finding",
        description=(
            "Compare one complete finding statement with every attached exact evidence item. Mark it supported, "
            "narrow it to what the evidence actually establishes, or remove it. A truncated evidence preview must "
            "first be read completely with read_evidence. This tool records a semantic judgment; the backend only "
            "checks evidence lineage and that every current finding received a current audit."
        ),
        parameters={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string", "minLength": 1},
                "verdict": {"type": "string", "enum": ["supported", "narrowed", "removed"]},
                "rationale": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_support_explanation"],
                },
                "revised_statement": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_statement"],
                    "description": "Required for narrowed; otherwise empty.",
                },
                "revised_support_explanation": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_support_explanation"],
                    "description": "Required for narrowed; otherwise empty.",
                },
                "revised_evidence_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Complete replacement evidence set. Required for narrowed. For supported, use it when a "
                        "specific reread found better exact support; otherwise empty. Must remain empty for removed."
                    ),
                },
                "revised_scope": {
                    "type": "object",
                    "properties": {
                        "actor": {"type": "string"},
                        "period": {"type": "string"},
                        "geography": {"type": "string"},
                        "qualifiers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["actor", "period", "geography", "qualifiers"],
                    "additionalProperties": False,
                    "description": "Complete replacement scope for narrowed; empty fields otherwise.",
                },
                "revised_limitations": {
                    "type": "array",
                    "maxItems": TRIAGE_LIMITATION_LIMIT,
                    "items": {
                        "type": "string",
                        "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
                    },
                    "description": "Complete replacement limitations for narrowed; otherwise empty.",
                },
                "source_no_findings_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["no_findings_reason"],
                    "description": "Required only when removal leaves the source with no findings.",
                },
            },
            "required": [
                "finding_id",
                "verdict",
                "rationale",
                "revised_statement",
                "revised_support_explanation",
                "revised_evidence_ids",
                "revised_scope",
                "revised_limitations",
                "source_no_findings_reason",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


__all__ = ["source_finding_grounding_audit_tool"]
