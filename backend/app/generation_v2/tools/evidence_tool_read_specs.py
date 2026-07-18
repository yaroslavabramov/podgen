from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.tools.evidence_tool_source_file_specs import evidence_source_file_tools
from app.generation_v2.tools.evidence_tool_source_read_specs import evidence_source_read_tools
from app.generation_v2.tools.evidence_types import EVIDENCE_PURPOSE_MAX_CHARS


def evidence_read_tools(owner: Any) -> list[AgentTool]:
    return [
        AgentTool(
            name="list_sources",
            description=(
                "List neutral metadata for sources selected in this run: source_ref, title/domain, type, date, "
                "canonical kind, coverage status, and a short brief_hint. "
                "Use this at the start of source triage to plan which sources to skim/read. It does not create evidence. "
                "Request up to 25 sources per page; large corpora should be paged deliberately rather than loaded from the prompt."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                    "cursor": {"type": "string"},
                },
                "required": ["limit", "cursor"],
            },
            handler=owner.list_sources_tool,
        ),
        AgentTool(
            name="search_corpus",
            description=(
                "Search the selected run corpus and persist returned passage matches as observed EvidenceItems. "
                "This is a discovery preview, not a final evidence reader. Search for one concrete question; use "
                "scope=all_sources for the allowed corpus scope, or scope=selected_sources with exact source_refs to "
                "target known sources. The returned short_snippet is a lead with an evidence_id handle. Use "
                "read_evidence, skim_source, inspect_source, or read_source_window when exact context is needed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 3},
                    "purpose": {"type": "string", "maxLength": EVIDENCE_PURPOSE_MAX_CHARS},
                    "scope": {"type": "string", "enum": ["all_sources", "selected_sources"]},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query", "purpose", "scope", "source_refs", "limit"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.search_corpus_tool, args),
        ),
        AgentTool(
            name="read_evidence",
            description=(
                "Exact-read one already-saved EvidenceItem by evidence_id before using it to ground a durable claim. "
                "Pass page_ref='' for the first read. If the full item is too long, the result contains one lossless "
                "exact page and a stable next_page_ref; keep copying next_page_ref until it is empty and "
                "exact_read_complete=true. Invalid handles return a working available_page_ref. The result includes "
                "source_ref/title, locator, snippet, and model-read support notes when available. "
                "Do not use read_evidence to discover new material; call search_corpus first if no evidence_id is known."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "page_ref": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 400, "maximum": 3000},
                },
                "required": ["evidence_id", "page_ref", "max_chars"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.read_evidence_tool, args),
        ),
        *evidence_source_file_tools(owner),
        *evidence_source_read_tools(owner),
        AgentTool(
            name="find_quotes",
            description=(
                "Find quote-like snippets about a specific topic and persist them as EvidenceItems. "
                "Use when a direct quote or quote-like support is needed for a durable entry, research atom, or citation link. "
                "This is not a generic source search tool; for general evidence use search_corpus. "
                "Quote refs should be evidence_id values or source_ref:locator refs from returned evidence, never raw quote text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["topic", "source_refs", "limit"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.find_quotes_tool, args),
        ),
        AgentTool(
            name="search_sources",
            description=(
                "Search for source-level candidates and evidence-backed snippets for corpus/source triage only. "
                "Use this to rank sources, detect source clusters, identify primary/secondary or duplicate relationships, and find gaps. "
                "Do not use search_sources in treatment, writing, rewriting, critique, or final citation linking."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query", "limit"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.search_sources_tool, args),
        ),
        AgentTool(
            name="compare_source_evidence",
            description=(
                "Retrieve comparable evidence previews from 2-5 selected sources for one question about duplication, "
                "an update chain, contradiction, coverage overlap, or another concrete relation. The tool searches "
                "each source with the same question and returns evidence_id-backed inputs grouped by source_ref. "
                "Treat those inputs as material to inspect, not as an automatic conclusion about the sources."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 5,
                    },
                    "question": {"type": "string"},
                    "limit_per_source": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["source_refs", "question", "limit_per_source"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.compare_source_evidence_tool, args),
        ),
        AgentTool(
            name="compare_evidence_for_question",
            description=(
                "Retrieve comparable evidence previews from 2-5 selected sources for one research question inside the current investigation job. "
                "Use this when an editorial atom needs several sources read together. After comparing, persist a research observation or an editorial atom draft."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 5,
                    },
                    "question": {"type": "string"},
                    "limit_per_source": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["source_refs", "question", "limit_per_source"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(
                owner.compare_evidence_for_question_tool,
                args,
            ),
        ),
    ]


__all__ = ["evidence_read_tools"]
