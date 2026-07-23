from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS


def semantic_review_self_check_tool(
    handler: Callable[[dict[str, Any]], Any],
    *,
    output_language: str = "",
) -> AgentTool:
    language = str(output_language or "").strip()
    return AgentTool(
        name="acknowledge_semantic_review",
        description=(
            "Confirm that every page of the current semantic_review snapshot was read after you made any needed "
            "semantic repairs. The backend checks only snapshot identity and complete delivery; it does not repeat "
            "your judgments. "
            + (
                f"Write readiness_explanation in output_language={language!r}; exact quotations remain in the source language."
                if language
                else "Write readiness_explanation in the workflow output language."
            )
        ),
        parameters={
            "type": "object",
            "properties": {
                "set_id": {"type": "string"},
                "review_snapshot_id": {"type": "string"},
                "readiness_explanation": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["semantic_review_conclusion"],
                },
            },
            "required": ["set_id", "review_snapshot_id", "readiness_explanation"],
            "additionalProperties": False,
        },
        handler=handler,
    )


__all__ = ["semantic_review_self_check_tool"]
