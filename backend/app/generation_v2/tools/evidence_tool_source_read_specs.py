from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool


def evidence_source_read_tools(owner: Any) -> list[AgentTool]:
    return [
        AgentTool(
            name="inspect_source",
            description=(
                "Inspect one source's readable structure: headings, chunk labels, page/window hints, and timestamp/section-like locators. "
                "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface. "
                "Use this when you need locators before exact reads; it does not create evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "version_ref": {"type": "string"},
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 40},
                },
                "required": ["source_ref", "max_items"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.inspect_source_tool, args),
        ),
        AgentTool(
            name="read_source_window",
            description=(
                "Read one concrete local text window from a selected source and persist that window as an EvidenceItem. "
                "This does not read the whole source; it opens an exact page, page range, heading, timestamp, section, beginning, or around-phrase window when local text/chunks are available. "
                "If skim_source is available and the source is unfamiliar, skim first; use read_source_window as a follow-up for exact support, quote seeds, relationship evidence, or an editorial atom. "
                "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface. "
                "For PDF/document sources this uses extracted/page-marked text; use read_source_with_model when an important claim needs direct file-level support. "
                "Use the returned evidence_id in durable write actions after you verify the readable detail."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "version_ref": {"type": "string"},
                    "locator": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 200, "maximum": 3000},
                },
                "required": ["source_ref", "locator", "max_chars"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.read_source_window_tool, args),
        ),
        AgentTool(
            name="read_source_with_model",
            description=(
                "Deep-read an uploaded source file with OpenAI input_file and persist the model-read result as an EvidenceItem. "
                "Use this when an important PDF/document/source claim requires reading the actual file rather than relying on local parser chunks or search previews. "
                "Provide source_ref, a focused question/claim, and optional locator_hint; version_ref is optional and should usually be omitted so the tool chooses the best provider-backed readable file. "
                "The tool asks the model for strongest support, quote/snippet, support_summary, uncertainty, and optional locator, then returns an evidence_id. "
                "If direct file reading is unavailable, the tool returns a human-readable coverage_warning. "
                "Research atoms select included evidence automatically through upsert_editorial_atom. Citation linking may still select evidence explicitly. "
                "Page numbers are useful when reported, but not required."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "version_ref": {"type": "string"},
                    "focus_question": {"type": "string"},
                    "claim_text": {"type": "string"},
                    "purpose": {"type": "string"},
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
                "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "version_ref": {"type": "string"},
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
                "Default first-read tool for one selected source. Skim representative windows and persist them as EvidenceItems. "
                "Use this before judging a source's role, scope, structure, duplicate status, editorial usefulness, or whether it can support a research atom. "
                "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface. "
                "For normal triage or research orientation, request 3-5 windows, then use read_source_window or read_source_with_model for exact follow-up."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "version_ref": {"type": "string"},
                    "max_windows": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["source_ref", "max_windows"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.skim_source_tool, args),
        ),
    ]


__all__ = ["evidence_source_read_tools"]
