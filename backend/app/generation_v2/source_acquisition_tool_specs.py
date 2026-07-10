from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool


def source_acquisition_agent_tools(owner: Any) -> list[AgentTool]:
    return [
        AgentTool(
            name="ingest_source_candidate",
            description=(
                "Add one vetted URL to the project corpus for the current gap report. "
                "Use only after web_search discovers a URL worth keeping. The backend validates the URL, dedupes it, creates a Source, runs native-first ingest, syncs source versions/provider files, and returns source/version ids. "
                "web_search content is discovery only; final evidence must come from the ingested source in later file_search/source-triage phases."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "gap_ids": {"type": "array", "items": {"type": "string"}},
                    "expected_editorial_function": {"type": "string"},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "source_type_hint": {"type": "string"},
                    "authority_hint": {"type": "string"},
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
                    "url": {"type": "string"},
                    "gap_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
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
                    "summary": {"type": "string"},
                    "unresolved_gap_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "unresolved_gap_ids"],
            },
            handler=owner.finalize_source_acquisition_round_tool,
        ),
    ]


__all__ = ["source_acquisition_agent_tools"]
