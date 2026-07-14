from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.source_acquisition_validation import (
    SOURCE_ACQUISITION_LIST_LIMITS,
    SOURCE_ACQUISITION_TEXT_LIMITS,
)


def _text(tool_name: str, field: str) -> dict[str, Any]:
    return {"type": "string", "maxLength": SOURCE_ACQUISITION_TEXT_LIMITS[tool_name][field]}


def _strings(tool_name: str, field: str) -> dict[str, Any]:
    max_items, max_length = SOURCE_ACQUISITION_LIST_LIMITS[tool_name][field]
    return {
        "type": "array",
        "maxItems": max_items,
        "items": {"type": "string", "maxLength": max_length},
    }


def source_acquisition_agent_tools(owner: Any) -> list[AgentTool]:
    return [
        AgentTool(
            name="ingest_source_candidate",
            description=(
                "Add one vetted URL to the project corpus for the current gap report. "
                "Use only after web_search discovers a URL worth keeping. The backend validates the URL, dedupes it, stages and ingests the source, then returns only an acceptance status. "
                "web_search content is discovery only; final evidence must come from the ingested source in later file_search/source-triage phases."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": _text("ingest_source_candidate", "url"),
                    "gap_ids": _strings("ingest_source_candidate", "gap_ids"),
                    "expected_editorial_function": _text("ingest_source_candidate", "expected_editorial_function"),
                    "rationale": _text("ingest_source_candidate", "rationale"),
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "source_type_hint": _text("ingest_source_candidate", "source_type_hint"),
                    "authority_hint": _text("ingest_source_candidate", "authority_hint"),
                },
                "required": [
                    "url",
                    "gap_ids",
                    "expected_editorial_function",
                    "rationale",
                    "confidence",
                    "source_type_hint",
                    "authority_hint",
                ],
            },
            handler=owner.ingest_source_candidate_tool,
        ),
        AgentTool(
            name="reject_source_candidate",
            description=(
                "Record an explicit rejection for a URL found during web_search. Use this for duplicates, weak relevance, unsupported file types, search pages, social posts, SEO summaries, or low-confidence candidates."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": _text("reject_source_candidate", "url"),
                    "gap_ids": _strings("reject_source_candidate", "gap_ids"),
                    "reason": _text("reject_source_candidate", "reason"),
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["url", "gap_ids", "reason", "confidence"],
            },
            handler=owner.reject_source_candidate_tool,
        ),
        AgentTool(
            name="finalize_source_acquisition_round",
            description=(
                "Finish this acquisition round after candidate discovery and ingest calls are complete. Include unresolved gap ids so the backend can decide whether to retry or fail closed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": _text("finalize_source_acquisition_round", "summary"),
                    "unresolved_gap_ids": _strings("finalize_source_acquisition_round", "unresolved_gap_ids"),
                },
                "required": ["summary", "unresolved_gap_ids"],
            },
            handler=owner.finalize_source_acquisition_round_tool,
        ),
    ]


__all__ = ["source_acquisition_agent_tools"]
