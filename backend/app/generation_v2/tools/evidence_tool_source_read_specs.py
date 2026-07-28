from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.tools.evidence_source_tools import (
    SOURCE_NAVIGATION_READ_MAX_CHARS,
    SOURCE_NAVIGATION_READ_MIN_CHARS,
    SOURCE_NAVIGATION_QUERY_MAX_CHARS,
    SOURCE_NAVIGATION_SEARCH_MAX_CHARS,
    SOURCE_NAVIGATION_SEARCH_MAX_RESULTS,
    SOURCE_NAVIGATION_SEARCH_MIN_CHARS,
)
from app.generation_v2.tools.evidence_types import EVIDENCE_PURPOSE_MAX_CHARS


def evidence_source_read_tools(owner: Any) -> list[AgentTool]:
    return [
        AgentTool(
            name="read_source",
            description=(
                "Read real contiguous text from one source as lossless stable passages. "
                "Start with cursor=''. The first result includes the first bounded structure page: full marker counts, "
                "total_count, explicit omitted_count and next_structure_cursor, never content previews. Pass only "
                "structure.next_structure_cursor to inspect_source.cursor. The top-level next_cursor belongs only to "
                "the next read_source call. Short sources "
                "are returned whole; long sources return one exact range ending near a completed text block. Continue "
                "with next_cursor; ranges are contiguous and eof marks the end. passages concatenate to the complete "
                "returned range. Each passage includes its full text and passage_ref; none is a preview or silently shortened. "
                "receipt_ref records that exact range in durable set memory; "
                "the visible tool result has delivery_status=delivered and is usable immediately. If the result's "
                "before_opening_different_source.required is true, save_reading_note or save_source_analysis for "
                "that source before calling any read tool for a different source. For normal sequential reading, "
                "omit max_chars; the backend uses the largest stable chunk. Use smaller max_chars only for a "
                "deliberately narrow reread. Read source_analysis_guidance "
                "before the passages; it identifies remaining sequential work or a verified exact duplicate."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string", "minLength": 1},
                    "cursor": {"type": "string"},
                    "max_chars": {
                        "type": "integer",
                        "minimum": SOURCE_NAVIGATION_READ_MIN_CHARS,
                        "maximum": SOURCE_NAVIGATION_READ_MAX_CHARS,
                        "description": (
                            "Optional. Omit for ordinary sequential reading; default is the maximum stable chunk."
                        ),
                    },
                },
                "required": ["source_ref", "cursor"],
                "additionalProperties": False,
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.read_source_tool, args),
        ),
        AgentTool(
            name="search_source",
            description=(
                "Search inside exactly one source for navigation. Returns real lossless match_text and durable "
                "receipt_refs, but deliberately returns no passage_ref because a search match cannot support a "
                "persisted finding. Each result also contains a stable context window of up to 3000 characters. "
                "Copy its exact_read_call unchanged into read_source_window; do not invent quotes or extend receipt "
                "offsets. Use the returned evidence_id. Search does not "
                "establish that unmatched regions are irrelevant."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string", "minLength": 1},
                    "query": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": SOURCE_NAVIGATION_QUERY_MAX_CHARS,
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": SOURCE_NAVIGATION_SEARCH_MAX_RESULTS,
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": SOURCE_NAVIGATION_SEARCH_MIN_CHARS,
                        "maximum": SOURCE_NAVIGATION_SEARCH_MAX_CHARS,
                    },
                },
                "required": ["source_ref", "query", "max_results", "max_chars"],
                "additionalProperties": False,
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.search_source_tool, args),
        ),
        AgentTool(
            name="inspect_source",
            description=(
                "Inspect document structure without reading its content. Returns headings, pages, timestamps, or "
                "coarse ranges with offsets and stable window_ref handles. Start with cursor='' and follow "
                "next_cursor. In triage, delivered pages remain available in source_memory after continuation. Use "
                "read_source for actual reading; this tool creates no evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 24},
                    "cursor": {"type": "string"},
                },
                "required": ["source_ref", "max_items", "cursor"],
                "additionalProperties": False,
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.inspect_source_tool, args),
        ),
        AgentTool(
            name="read_source_window",
            description=(
                "Read one concrete local text window from a selected source and persist that window as an EvidenceItem. "
                "This does not read the whole source. Prefer a durable receipt_ref returned by read_source, or copy "
                "search_source.exact_read_call unchanged to read the stable expanded context around a match. "
                "Receipts remain valid after worker restart. If a receipt covers more than max_chars, "
                "select one exact quote or an absolute start_offset/end_offset inside its returned range. Alternatively "
                "pass a window_ref returned by inspect_source/skim_source, or a concrete locator such as page, heading, "
                "timestamp, beginning, or exact phrase. Exactly one of receipt_ref, window_ref, or locator must be "
                "non-empty; leave unused selectors empty. quote and offsets are valid only with receipt_ref; choose quote or both offsets, not both. "
                "Timestamps are locators only for transcript/audio/video sources. An unresolved locator returns "
                "locator_not_found and never substitutes the beginning of the source. "
                "Pass source_ref; the backend selects the readable version from the pinned run corpus. "
                "For PDF/document sources this uses extracted/page-marked text and returns a durable evidence_id. "
                "In triage, this is the required bridge from a search result to exact finding evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string", "minLength": 1},
                    "receipt_ref": {"type": "string"},
                    "window_ref": {"type": "string"},
                    "locator": {"type": "string"},
                    "quote": {"type": "string"},
                    "start_offset": {"type": "integer", "minimum": 0},
                    "end_offset": {"type": "integer", "minimum": 1},
                    "max_chars": {"type": "integer", "minimum": 200, "maximum": 3000},
                },
                "required": ["source_ref", "window_ref", "locator", "max_chars"],
                "additionalProperties": False,
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.read_source_window_tool, args),
        ),
        AgentTool(
            name="read_source_with_model",
            description=(
                "Deep-read an uploaded source file with OpenAI input_file and persist the model-read result as an EvidenceItem. "
                "Use this when an important PDF/document/source claim requires reading the actual file rather than relying on local parser chunks or search previews. "
                "Provide source_ref, a focused question/claim, and locator_hint; the backend chooses the provider-backed file pinned to this run. "
                "The tool asks the model for strongest support, quote/snippet, support_summary, uncertainty, and optional locator, then returns an evidence_id. "
                "If direct file reading is unavailable, the tool returns a human-readable coverage_warning. "
                "Research atoms select included evidence automatically through upsert_editorial_atom. Citation linking may still select evidence explicitly. "
                "Page numbers are useful when reported, but not required."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "focus_question": {"type": "string"},
                    "claim_text": {"type": "string"},
                    "purpose": {"type": "string", "maxLength": EVIDENCE_PURPOSE_MAX_CHARS},
                    "locator_hint": {"type": "string"},
                },
                "required": ["source_ref", "focus_question", "claim_text", "purpose", "locator_hint"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.read_source_with_model_tool, args),
        ),
        AgentTool(
            name="search_within_source",
            description=(
                "Search for a phrase/topic inside exactly one selected source, persist matching windows as observed EvidenceItems, and return evidence ids. "
                "Use this for targeted follow-up after skim_source, inspect_source, or corpus search finds a promising source. "
                "Pass source_ref; the backend selects the readable version from the pinned run corpus."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["source_ref", "query", "limit"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.search_within_source_tool, args),
        ),
        AgentTool(
            name="skim_source",
            description=(
                "Default first-read tool for one selected source. Skim representative windows without creating or selecting EvidenceItems. "
                "Use this to understand the source's content, scope, structure, and possible overlap before making "
                "source-level judgments. "
                "Pass source_ref; the backend selects the readable version from the pinned run corpus. "
                "Each result has an opaque window_ref; pass it to read_source_window to persist exact follow-up evidence. "
                "The handle is intentionally short: copy it exactly and never edit or synthesize it. "
                "For normal triage or research orientation, request 3-5 windows, then use read_source_window or read_source_with_model for exact follow-up."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "max_windows": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["source_ref", "max_windows"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.skim_source_tool, args),
        ),
    ]


__all__ = ["evidence_source_read_tools"]
