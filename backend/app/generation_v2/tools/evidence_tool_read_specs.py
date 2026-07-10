from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.tools.evidence_tool_source_read_specs import evidence_source_read_tools


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
                "This is a discovery preview tool, not a final evidence reader: use it to find leads for a specific investigation question, source comparison, quote, relation, or citation-sensitive claim. "
                "It is not a substitute for skimming a source when you need to understand that source's overall role. "
                "Use scope=all_sources for broad discovery, or scope=selected_sources with source_refs from the current prompt or prior tool results to target known sources. "
                "The returned short_snippet is a preview with an evidence_id handle; call read_evidence, skim_source, inspect_source, or read_source_window when the preview is not enough before writing a durable conclusion."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 3},
                    "purpose": {"type": "string"},
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
                "Read one already-saved EvidenceItem by evidence_id. Use this to inspect selected evidence before writing, rewriting, or linking a claim. "
                "The result is a readable detail payload with source_ref/title, locator, snippet, and model-read support notes when available. "
                "Do not use read_evidence to discover new material; call search_corpus first if no evidence_id is known."
            ),
            parameters={
                "type": "object",
                "properties": {"evidence_id": {"type": "string"}},
                "required": ["evidence_id"],
            },
            handler=lambda args: owner._call_with_research_commit_advisory(owner.read_evidence_tool, args),
        ),
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
                "Retrieve comparable evidence previews from 2-5 selected sources for one source-map question: duplication, interpretive relationship, update chain, contradiction, coverage overlap, or quality gap. "
                "The tool searches each source with the same question and returns evidence_id-backed inputs grouped by source_ref. "
                "Use this when a cluster or relationship needs multiple sources read together; write the resulting cluster/relationship/gap afterward."
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
