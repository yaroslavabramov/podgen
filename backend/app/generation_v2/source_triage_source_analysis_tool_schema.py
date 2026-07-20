from __future__ import annotations

from typing import Any

from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS


def reading_basis_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": (
            "The model-authored stopping judgment. The backend binds delivered reading receipts and derives "
            "range coverage without asking the model to copy transport identifiers."
        ),
        "properties": {
            "coverage_summary": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["reading_basis"],
            },
            "unread_regions": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["reading_basis"],
            },
            "unread_risk_assessment": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["reading_basis"],
                "description": (
                    "Explain why unread material cannot reasonably change the analysis, or state that no material "
                    "region remains unread. This is a semantic judgment, not a percentage threshold."
                ),
            },
            "representation_limits": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["reading_basis"],
            },
            "stopping_reason": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["reading_basis"],
                "description": "Why further reading is unlikely to change findings, scope, or question links.",
            },
        },
        "required": [
            "coverage_summary",
            "unread_regions",
            "unread_risk_assessment",
            "representation_limits",
            "stopping_reason",
        ],
        "additionalProperties": False,
    }


__all__ = ["reading_basis_schema"]
