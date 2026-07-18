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
                "Read real contiguous text from one source for set-agent reasoning without creating an EvidenceItem. "
                "Start with cursor=''. The first result includes bounded structure metadata: full marker counts, a "
                "representative heading/timestamp index, and explicit omitted_count, never content previews. Short sources "
                "are returned whole; long sources return one exact range ending near a completed text block. Continue "
                "with next_cursor; ranges are contiguous and eof marks the end. text is the complete returned range, "
                "not a preview and not silently shortened. receipt_ref records that exact range in durable set memory; "
                "the visible tool result has delivery_status=delivered and is usable immediately."
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
                    },
                },
                "required": ["source_ref", "cursor", "max_chars"],
                "additionalProperties": False,
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.read_source_tool, args),
        ),
        AgentTool(
            name="search_source",
            description=(
                "Search inside exactly one source for navigation without creating EvidenceItems. Returns real exact "
                "context ranges, not snippets or previews. Each result includes its full text, offsets, durable "
                "receipt_ref, and a stable window_ref that can be passed directly to read_source_window for evidence "
                "the visible result has delivery_status=delivered and is usable immediately."
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
                "Inspect one source as a contiguous page of stable readable windows. Start with cursor=''; follow "
                "next_cursor when you need later areas or complete coverage. This makes every character of a long "
                "single-section source reachable without truncation or sampling gaps. Returned previews are "
                "orientation only; exact-read a "
                "window_ref before using its text as durable support. Each preview reports preview_chars and "
                "preview_omitted_chars explicitly. "
                "Pass source_ref; the backend selects the readable version from the pinned run corpus. "
                "Use this when you need locators before exact reads; it does not create evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 24},
                    "cursor": {"type": "string"},
                },
                "required": ["source_ref", "max_items", "cursor"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.inspect_source_tool, args),
        ),
        AgentTool(
            name="read_source_window",
            description=(
                "Read one concrete local text window from a selected source and persist that window as an EvidenceItem. "
                "This does not read the whole source. Prefer a durable receipt_ref returned by read_source or "
                "search_source; it remains valid after worker restart. If that receipt covers more than max_chars, "
                "select one exact quote or an absolute start_offset/end_offset inside its returned range. Alternatively "
                "pass a window_ref returned by inspect_source/skim_source, or a concrete locator such as page, heading, "
                "timestamp, beginning, or exact phrase. Exactly one of receipt_ref, window_ref, or locator must be "
                "non-empty. quote and offsets are valid only with receipt_ref; choose quote or both offsets, not both. "
                "Timestamps are locators only for transcript/audio/video sources. An unresolved locator returns "
                "locator_not_found and never substitutes the beginning of the source. "
                "Pass source_ref; the backend selects the readable version from the pinned run corpus. "
                "For PDF/document sources this uses extracted/page-marked text and returns a durable evidence_id."
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
